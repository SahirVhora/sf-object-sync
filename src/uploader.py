"""
Uploader - POSTs missing entities to Dev in strict hierarchy order.

Guarded by:
  1. dry_run check (refuses to POST if dry_run=True)
  2. CONFIRM prompt (user must type CONFIRM before any write)

Upload order (top → bottom):
  FOCompany → FOBusinessUnit → FODivision → FODepartment → cust_SubDepartment
"""

import json
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from .audit_logger import (
    DEV_EXISTS,
    SKIPPED_DUE_TO_PARENT_FAILURE,
    UPLOAD_FAILED,
    UPLOAD_SUCCESS,
    VERIFICATION_FAILED,
    VERIFICATION_OK,
    AuditLogger,
)
from .entity_config import UPLOAD_ORDER, get_config
from .hierarchy_resolver import _select_active_record
from .payload_builder import build_payload, extract_parent_codes
from .sf_client import AmbiguousWriteError, SFClient, SFClientError

logger = logging.getLogger(__name__)


class Uploader:
    """
    Orchestrates POST of missing entities to Dev.

    Accepts the full set of resolved chains and gap results, then processes
    entities in strict UPLOAD_ORDER (Legal Entity first, Sub Department last).
    Skips dependents of any failed parent.
    """

    def __init__(
        self,
        dev_client: SFClient,
        prd_records: Dict[Tuple[str, str], Dict[str, Any]],
        gap_results: Dict[Tuple[str, str], Any],
        audit: AuditLogger,
        dry_run: bool,
    ) -> None:
        self._dev = dev_client
        self._prd_records = prd_records  # (entity_type, code) → record
        self._gap = gap_results          # (entity_type, code) → GapCheckResult
        self._audit = audit
        self._dry_run = dry_run
        self._failed_parents: Set[Tuple[str, str]] = set()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ancestor_failed(self, chain: List[Tuple[str, str, Dict]]) -> Optional[Tuple[str, str]]:
        """Return first ancestor (entity_type, code) that is in failed_parents, or None."""
        for etype, code, _ in chain:
            if (etype, code) in self._failed_parents:
                return (etype, code)
        return None

    def _verify(self, entity_type: str, code: str) -> bool:
        """Re-query Dev to confirm the entity exists after posting."""
        cfg = get_config(entity_type)
        entity_set = cfg["entity_set"]
        try:
            records = self._dev.get_entity_by_code(entity_set, code)
            active = _select_active_record(records, entity_type, code) if records else None
            return active is not None
        except SFClientError:
            return False

    def _mark_failed(self, entity_type: str, code: str) -> None:
        self._failed_parents.add((entity_type, code))

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def run(
        self,
        chains_by_input: List[List[Tuple[str, str, Dict]]],
    ) -> None:
        """
        Execute uploads for all missing entities across all chains.

        chains_by_input: list of resolved chains from HierarchyResolver.
        Processes in UPLOAD_ORDER so parents always come before children.
        """
        if self._dry_run:
            raise RuntimeError("Uploader.run() called with dry_run=True - programming error")

        # Collect all unique (entity_type, code) pairs across all chains
        all_entities: Dict[Tuple[str, str], List[Tuple[str, str, Dict]]] = {}
        for chain in chains_by_input:
            for etype, code, _record in chain:
                key = (etype, code)
                if key not in all_entities:
                    all_entities[key] = chain  # store chain for ancestor lookup

        # Process in strict top-down order
        for entity_type in UPLOAD_ORDER:
            relevant = [
                (etype, code)
                for (etype, code) in all_entities
                if etype == entity_type
            ]
            for etype, code in relevant:
                self._upload_one(etype, code, all_entities[(etype, code)])

    def _upload_one(
        self,
        entity_type: str,
        code: str,
        chain: List[Tuple[str, str, Dict]],
    ) -> None:
        """Upload a single entity to Dev if it's missing and no ancestor failed."""
        gap_result = self._gap.get((entity_type, code))
        cfg = get_config(entity_type)
        entity_set = cfg["entity_set"]

        # Already in Dev - skip
        if gap_result and gap_result.status == DEV_EXISTS:
            logger.info("SKIP (already exists): %s '%s'", entity_type, code)
            self._audit.log(
                phase="upload",
                status=DEV_EXISTS,
                object_type=entity_type,
                entity_set=entity_set,
                external_code=code,
            )
            return

        # Check for failed ancestor
        failed_ancestor = self._ancestor_failed(chain)
        if failed_ancestor:
            fa_type, fa_code = failed_ancestor
            msg = f"Parent {fa_type} '{fa_code}' failed to upload"
            logger.warning("SKIP %s '%s': %s", entity_type, code, msg)
            self._audit.log(
                phase="upload",
                status=SKIPPED_DUE_TO_PARENT_FAILURE,
                object_type=entity_type,
                entity_set=entity_set,
                external_code=code,
                error_message=msg,
            )
            self._mark_failed(entity_type, code)
            return

        # Build payload
        prd_record = self._prd_records.get((entity_type, code))
        if not prd_record:
            logger.error("No PRD record cached for %s '%s'", entity_type, code)
            return

        parent_codes = extract_parent_codes(chain)
        # Inject Dev base URL so builders can use __metadata.uri for nav properties
        # (avoids deep-insert; links to existing Dev entities instead of creating them).
        parent_codes["_base_url"] = self._dev.base_url
        try:
            payload = build_payload(entity_type, prd_record, parent_codes)
        except Exception as exc:
            msg = f"Payload build error: {exc}"
            logger.error(msg)
            self._audit.log(
                phase="upload",
                status=UPLOAD_FAILED,
                object_type=entity_type,
                entity_set=entity_set,
                external_code=code,
                error_message=msg,
            )
            self._mark_failed(entity_type, code)
            return

        # POST
        try:
            http_status, response = self._dev.post_entity(entity_set, payload)
        except AmbiguousWriteError as exc:
            # The server may have committed the create before its response was
            # lost. Reconcile by the stable external code instead of replaying
            # a potentially duplicate POST.
            if self._verify(entity_type, code):
                msg = f"{exc}; target object found during reconciliation"
                logger.warning("UPLOAD_SUCCESS (reconciled): %s '%s'", entity_type, code)
                self._audit.log(
                    phase="upload",
                    status=UPLOAD_SUCCESS,
                    object_type=entity_type,
                    entity_set=entity_set,
                    external_code=code,
                    http_status=exc.status_code,
                    payload_sent=payload,
                    response_received={"reconciled": True},
                    error_message=msg,
                )
                self._audit.log(
                    phase="verification",
                    status=VERIFICATION_OK,
                    object_type=entity_type,
                    entity_set=entity_set,
                    external_code=code,
                )
                return

            msg = f"{exc}; target object not found during reconciliation"
            logger.error("POST outcome unresolved for %s '%s': %s", entity_type, code, msg)
            self._audit.log(
                phase="upload",
                status=UPLOAD_FAILED,
                object_type=entity_type,
                entity_set=entity_set,
                external_code=code,
                http_status=exc.status_code,
                payload_sent=payload,
                error_message=msg,
            )
            self._mark_failed(entity_type, code)
            return
        except SFClientError as exc:
            msg = str(exc)
            logger.error("POST failed for %s '%s': %s", entity_type, code, msg)
            self._audit.log(
                phase="upload",
                status=UPLOAD_FAILED,
                object_type=entity_type,
                entity_set=entity_set,
                external_code=code,
                payload_sent=payload,
                error_message=msg,
            )
            self._mark_failed(entity_type, code)
            return

        if http_status == 201:
            logger.info("UPLOAD_SUCCESS: %s '%s' → HTTP %d", entity_type, code, http_status)
            self._audit.log(
                phase="upload",
                status=UPLOAD_SUCCESS,
                object_type=entity_type,
                entity_set=entity_set,
                external_code=code,
                http_status=http_status,
                payload_sent=payload,
                response_received=response,
            )
            # Verify
            verified = self._verify(entity_type, code)
            v_status = VERIFICATION_OK if verified else VERIFICATION_FAILED
            logger.info("%s: %s '%s'", v_status, entity_type, code)
            self._audit.log(
                phase="verification",
                status=v_status,
                object_type=entity_type,
                entity_set=entity_set,
                external_code=code,
            )
        else:
            err_msg = json.dumps(response)[:1000]
            logger.error(
                "UPLOAD_FAILED: %s '%s' → HTTP %d | %s",
                entity_type,
                code,
                http_status,
                err_msg,
            )
            self._audit.log(
                phase="upload",
                status=UPLOAD_FAILED,
                object_type=entity_type,
                entity_set=entity_set,
                external_code=code,
                http_status=http_status,
                payload_sent=payload,
                response_received=response,
                error_message=err_msg,
            )
            self._mark_failed(entity_type, code)
