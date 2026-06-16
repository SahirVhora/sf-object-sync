"""
Gap checker - determines which entities in a resolved hierarchy chain
are missing from the Dev tenant.

Deduplicates: if the same (entity_type, externalCode) appears in multiple
input rows' chains, it is checked exactly once.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from .entity_config import get_config
from .hierarchy_resolver import _select_active_record
from .sf_client import SFClient, SFClientError

logger = logging.getLogger(__name__)

# Status constants
DEV_EXISTS = "DEV_EXISTS"
DEV_MISSING = "DEV_MISSING"


class GapCheckResult:
    """Result of a single entity gap check."""

    __slots__ = ("entity_type", "external_code", "entity_set", "status", "dev_record")

    def __init__(
        self,
        entity_type: str,
        external_code: str,
        entity_set: str,
        status: str,
        dev_record: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.entity_type = entity_type
        self.external_code = external_code
        self.entity_set = entity_set
        self.status = status
        self.dev_record = dev_record

    def __repr__(self) -> str:
        return (
            f"GapCheckResult({self.entity_type!r}, {self.external_code!r}, "
            f"{self.status!r})"
        )


class GapChecker:
    """
    Checks all entities in resolved chains against the Dev tenant.

    Maintains a dedup set so each (entity_type, externalCode) is checked once.
    """

    def __init__(self, dev_client: SFClient) -> None:
        self._client = dev_client
        self._checked: Dict[Tuple[str, str], GapCheckResult] = {}

    def check_chain(
        self,
        chain: List[Tuple[str, str, Dict[str, Any]]],
    ) -> List[GapCheckResult]:
        """
        Check all entities in *chain* against Dev.

        *chain* is the output of HierarchyResolver.resolve():
          [(entity_type, external_code, prd_record), ...]

        Returns list of GapCheckResult in chain order (already-checked entries
        returned from cache without another API call).
        """
        results: List[GapCheckResult] = []
        for entity_type, code, _record in chain:
            key = (entity_type, code)
            if key in self._checked:
                logger.debug("Gap cache hit: %s '%s'", entity_type, code)
                results.append(self._checked[key])
                continue

            result = self._check_one(entity_type, code)
            self._checked[key] = result
            results.append(result)
        return results

    def _check_one(self, entity_type: str, code: str) -> GapCheckResult:
        """Perform a single entity existence check against Dev."""
        cfg = get_config(entity_type)
        entity_set = cfg["entity_set"]

        logger.debug("Checking Dev: %s '%s' (%s)", entity_type, code, entity_set)
        try:
            records = self._client.get_entity_by_code(entity_set, code)
        except SFClientError as exc:
            logger.error(
                "Dev check error for %s '%s': %s - aborting to avoid unsafe create",
                entity_type,
                code,
                exc,
            )
            raise

        if not records:
            return GapCheckResult(entity_type, code, entity_set, DEV_MISSING)

        active = _select_active_record(records, entity_type, code)
        if active is None:
            return GapCheckResult(entity_type, code, entity_set, DEV_MISSING)

        logger.debug("Dev: %s '%s' EXISTS", entity_type, code)
        return GapCheckResult(entity_type, code, entity_set, DEV_EXISTS, active)

    def get_results(self) -> Dict[Tuple[str, str], GapCheckResult]:
        """Return all cached gap-check results."""
        return dict(self._checked)

    def print_gap_report(self) -> None:
        """Print a formatted gap analysis table to the console."""
        missing = [r for r in self._checked.values() if r.status == DEV_MISSING]
        exists = [r for r in self._checked.values() if r.status == DEV_EXISTS]

        print("\n" + "=" * 70)
        print("  GAP ANALYSIS REPORT")
        print(f"  Total entities checked : {len(self._checked)}")
        print(f"  Already in Dev         : {len(exists)}")
        print(f"  Missing from Dev       : {len(missing)}")
        print("=" * 70)

        if missing:
            print("\n  MISSING from Dev (will be created):")
            print(f"  {'Entity Type':<20} {'External Code':<25} {'Entity Set'}")
            print("  " + "-" * 65)
            for r in sorted(missing, key=lambda x: x.entity_type):
                print(f"  {r.entity_type:<20} {r.external_code:<25} {r.entity_set}")

        if exists:
            print("\n  Already in Dev (will be skipped):")
            print(f"  {'Entity Type':<20} {'External Code':<25} {'Entity Set'}")
            print("  " + "-" * 65)
            for r in sorted(exists, key=lambda x: x.entity_type):
                print(f"  {r.entity_type:<20} {r.external_code:<25} {r.entity_set}")

        print()
