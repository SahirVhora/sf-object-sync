"""
Audit logger - writes one JSON line per event to output/audit_<timestamp>.jsonl.

Each record captures: phase, object type, status, HTTP details, payload/response,
dry_run flag, and a shared run_id (uuid4) for correlation across the run.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── Status constants ────────────────────────────────────────────────────────
VALIDATION_FAILED = "VALIDATION_FAILED"
PRD_NOT_FOUND = "PRD_NOT_FOUND"
HIERARCHY_BROKEN = "HIERARCHY_BROKEN"
DEV_EXISTS = "DEV_EXISTS"
DEV_MISSING = "DEV_MISSING"
DRY_RUN_OK = "DRY_RUN_OK"
DRY_RUN_INTEGRITY_FAIL = "DRY_RUN_INTEGRITY_FAIL"
UPLOAD_SUCCESS = "UPLOAD_SUCCESS"
UPLOAD_FAILED = "UPLOAD_FAILED"
VERIFICATION_OK = "VERIFICATION_OK"
VERIFICATION_FAILED = "VERIFICATION_FAILED"
SKIPPED_DUE_TO_PARENT_FAILURE = "SKIPPED_DUE_TO_PARENT_FAILURE"

ALL_STATUSES = [
    VALIDATION_FAILED,
    PRD_NOT_FOUND,
    HIERARCHY_BROKEN,
    DEV_EXISTS,
    DEV_MISSING,
    DRY_RUN_OK,
    DRY_RUN_INTEGRITY_FAIL,
    UPLOAD_SUCCESS,
    UPLOAD_FAILED,
    VERIFICATION_OK,
    VERIFICATION_FAILED,
    SKIPPED_DUE_TO_PARENT_FAILURE,
]


class AuditLogger:
    """
    JSONL audit logger.  One instance per run - maintains run_id and file handle.

    Usage:
        with AuditLogger(output_dir, dry_run) as al:
            al.log(phase=..., object_type=..., ...)
    """

    def __init__(self, output_dir: str, dry_run: bool) -> None:
        self.run_id = str(uuid.uuid4())
        self.dry_run = dry_run
        self._records: list = []

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        self._path = os.path.join(output_dir, f"audit_{ts}.jsonl")
        self._fh = open(self._path, "w", encoding="utf-8")
        logger.info("Audit log: %s", self._path)

    @property
    def path(self) -> str:
        return self._path

    def log(
        self,
        phase: str,
        status: str,
        object_type: str = "",
        entity_set: str = "",
        external_code: str = "",
        http_status: Optional[int] = None,
        payload_sent: Optional[Dict[str, Any]] = None,
        response_received: Optional[Dict[str, Any]] = None,
        error_message: str = "",
    ) -> Dict[str, Any]:
        """Write one audit record to the JSONL file and return it."""
        record: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "phase": phase,
            "object_type": object_type,
            "entity_set": entity_set,
            "external_code": external_code,
            "status": status,
            "http_status": http_status,
            "payload_sent": payload_sent,
            "response_received": response_received,
            "error_message": error_message,
            "dry_run_flag": self.dry_run,
        }
        self._records.append(record)
        line = json.dumps(record, default=str)
        self._fh.write(line + "\n")
        self._fh.flush()
        return record

    def all_records(self) -> list:
        """Return a copy of all records logged this run."""
        return list(self._records)

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> "AuditLogger":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
