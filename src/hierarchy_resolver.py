"""
Hierarchy resolver - walks the parent chain from a starting object up to
Legal Entity using ENTITY_CONFIG.

Parent lookup strategy (per entity type):
  cust_SubDepartment → plain FK field "cust_parentDepartment" in GET response
  FODepartment       → $expand=cust_Division      → results[0].externalCode
  FODivision         → $expand=cust_BusinessUnit  → results[0].externalCode
  FOBusinessUnit     → $expand=cust_legalEntity   → results[0].externalCode

All fetched records are cached by (entity_type, externalCode).
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from .entity_config import ENTITY_CONFIG, get_config
from .sf_client import SFClient, SFClientError

logger = logging.getLogger(__name__)


class HierarchyBrokenError(Exception):
    """Raised when a required parent reference is null/empty in PRD."""

    def __init__(self, entity_type: str, code: str, ref: str):
        self.entity_type = entity_type
        self.code = code
        self.parent_field = ref
        super().__init__(
            f"HIERARCHY_BROKEN: {entity_type} '{code}' has no resolvable parent "
            f"(checked: '{ref}')"
        )


class EntityNotFoundError(Exception):
    """Raised when a required entity is not found in PRD."""

    def __init__(self, entity_type: str, code: str):
        self.entity_type = entity_type
        self.code = code
        super().__init__(f"PRD_NOT_FOUND: {entity_type} '{code}' not found in PRD")


def _select_active_record(
    records: List[Dict[str, Any]],
    entity_type: str,
    code: str,
) -> Optional[Dict[str, Any]]:
    """
    From a list of records, pick the active/open-ended one.

    Matches end_date_field == 9999-12-31 in any /Date()/ encoding.
    Falls back to the record with the highest (latest) date field value.
    """
    cfg = get_config(entity_type)
    end_field = cfg["end_date_field"]

    # All known representations of 9999-12-31 / infinity
    infinity_patterns = {
        "/Date(253370764800000)/",
        "/Date(253370764800000+0000)/",
        "/Date(253402214400000)/",      # 9999-12-31T00:00:00 in some tenants
        "/Date(253402214400000+0000)/",
        "9999-12-31T00:00:00",
        "9999-12-31",
    }

    active = [r for r in records if r.get(end_field) in infinity_patterns]

    if not active:
        # Fallback: latest start date
        date_field = cfg["date_field"]
        active = sorted(records, key=lambda r: r.get(date_field, ""), reverse=True)[:1]

    if not active:
        return None

    if len(active) > 1:
        logger.debug("%s '%s': %d active records, using first", entity_type, code, len(active))
    return active[0]


def _extract_parent_code_from_nav(
    record: Dict[str, Any],
    parent_nav: str,
) -> Optional[str]:
    """
    Extract the parent externalCode from an already-expanded navigation property.

    The $expand response embeds the nav property as:
      "<parent_nav>": {"results": [{"externalCode": "...", ...}]}
    or (single-value nav):
      "<parent_nav>": {"externalCode": "...", ...}

    Returns None if the nav property is absent, deferred, or has no results.
    """
    nav_data = record.get(parent_nav)
    if not nav_data or isinstance(nav_data, str):
        return None

    # Still deferred - expand was not honoured
    if "__deferred" in nav_data:
        return None

    # Collection nav: {"results": [...]}
    if "results" in nav_data:
        results = nav_data["results"]
        if results:
            return str(results[0].get("externalCode", "")).strip() or None
        return None

    # Single-entity nav: {"externalCode": "...", ...}
    code = nav_data.get("externalCode")
    return str(code).strip() if code else None


class HierarchyResolver:
    """
    Resolves the full parent chain for a starting entity in PRD.

    Returns an ordered list of (entity_type, externalCode, record_dict) tuples,
    from the starting object up to (and including) Legal Entity.
    """

    def __init__(self, prd_client: SFClient) -> None:
        self._client = prd_client
        # cache: (entity_type, externalCode) → record dict (may include expanded nav)
        self._cache: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def _fetch(self, entity_type: str, code: str) -> Dict[str, Any]:
        """
        Fetch entity from PRD with cache.

        For entities using parent_nav, fetches with $expand so the parent
        record is embedded inline - avoiding a second round-trip.
        """
        key = (entity_type, code)
        if key in self._cache:
            logger.debug("Cache hit: %s '%s'", entity_type, code)
            return self._cache[key]

        cfg = get_config(entity_type)
        entity_set = cfg["entity_set"]
        parent_nav = cfg.get("parent_nav")

        logger.debug(
            "Fetching %s '%s' from PRD (%s)%s",
            entity_type, code, entity_set,
            f" [$expand={parent_nav}]" if parent_nav else "",
        )
        try:
            records = self._client.get_entity_by_code(
                entity_set, code, expand=parent_nav
            )
        except SFClientError as exc:
            raise EntityNotFoundError(entity_type, code) from exc

        if not records:
            raise EntityNotFoundError(entity_type, code)

        record = _select_active_record(records, entity_type, code)
        if record is None:
            raise EntityNotFoundError(entity_type, code)

        self._cache[key] = record
        return record

    def _get_parent_code(
        self,
        entity_type: str,
        record: Dict[str, Any],
    ) -> Optional[str]:
        """
        Extract the parent externalCode from a record.

        Uses parent_field (direct string) or parent_nav (expanded inline data)
        depending on ENTITY_CONFIG for this entity type.
        """
        cfg = get_config(entity_type)
        parent_field = cfg.get("parent_field")
        parent_nav   = cfg.get("parent_nav")

        if parent_field:
            val = record.get(parent_field)
            if val and str(val).strip() not in ("", "null", "None"):
                return str(val).strip()
            return None

        if parent_nav:
            return _extract_parent_code_from_nav(record, parent_nav)

        return None  # Legal Entity - no parent

    def resolve(
        self,
        start_entity_type: str,
        start_code: str,
    ) -> List[Tuple[str, str, Dict[str, Any]]]:
        """
        Walk the hierarchy from *start_entity_type* up to Legal Entity.

        Returns list of (entity_type, externalCode, record) in traversal order
        (starting object first, Legal Entity last).

        Raises:
          EntityNotFoundError  - if any entity is missing in PRD
          HierarchyBrokenError - if a parent reference cannot be resolved
        """
        chain: List[Tuple[str, str, Dict[str, Any]]] = []
        current_type = start_entity_type
        current_code = start_code
        visited: set = set()

        while current_type is not None:
            if (current_type, current_code) in visited:
                logger.error("Cycle detected: %s '%s'", current_type, current_code)
                break
            visited.add((current_type, current_code))

            record = self._fetch(current_type, current_code)
            chain.append((current_type, current_code, record))

            cfg = get_config(current_type)
            parent_entity_type = cfg["parent_entity"]

            if parent_entity_type is None:
                # Reached the top (Legal Entity)
                break

            parent_code = self._get_parent_code(current_type, record)
            ref_name = cfg.get("parent_field") or cfg.get("parent_nav") or "?"

            if not parent_code:
                raise HierarchyBrokenError(current_type, current_code, ref_name)

            logger.debug(
                "%s '%s' → %s '%s' (via '%s')",
                current_type, current_code,
                parent_entity_type, parent_code, ref_name,
            )
            current_type = parent_entity_type
            current_code = parent_code

        return chain

    def get_cache(self) -> Dict[Tuple[str, str], Dict[str, Any]]:
        return dict(self._cache)

    def prime_cache(self, entity_type: str, code: str, record: Dict[str, Any]) -> None:
        """Pre-populate cache (used in tests / when record already fetched)."""
        self._cache[(entity_type, code)] = record
