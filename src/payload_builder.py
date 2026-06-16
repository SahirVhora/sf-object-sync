"""
Payload builder - constructs OData v2 POST payloads for each entity type.

Rules:
  - Date fields use /Date(<epoch_ms>)/ format
  - Today's date calculated at runtime via datetime.utcnow()
  - Infinity end date: /Date(253370764800000)/  (= 9999-12-31)
  - Null/empty optional fields are omitted (not posted as null)
  - System-managed fields from EXCLUDED_FROM_POST are always omitted
  - _en_DEBUG locale fields are excluded
"""

import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

from .entity_config import EXCLUDED_FROM_POST, INFINITY_DATE_MS

logger = logging.getLogger(__name__)

INFINITY_DATE_STR = f"/Date({INFINITY_DATE_MS})/"


def _odata_date_to_datetime_key(date_str: str) -> str:
    """Convert /Date(<ms>)/ → OData URI datetime key: 'YYYY-MM-DDTHH:MM:SS'."""
    ms = int(re.search(r"/Date\((-?\d+)", date_str).group(1))
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    dt = epoch + timedelta(milliseconds=ms)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _today_epoch_ms() -> int:
    """Return today's date at midnight UTC as epoch milliseconds."""
    now = datetime.now(timezone.utc)
    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return int(midnight.timestamp() * 1000)


def _today_date_str() -> str:
    """Return today's OData date string: /Date(<ms>)/"""
    return f"/Date({_today_epoch_ms()})/"


def _optional(record: Dict[str, Any], field: str) -> Optional[str]:
    """Return field value if non-null/non-empty, else None."""
    val = record.get(field)
    if val is None or str(val).strip() in ("", "null", "None"):
        return None
    return str(val)


def _copy_if_present(
    payload: Dict[str, Any],
    record: Dict[str, Any],
    field: str,
) -> None:
    """Add *field* from *record* to *payload* only if non-null."""
    val = _optional(record, field)
    if val is not None:
        payload[field] = val


def _is_excluded(field: str) -> bool:
    """Return True if *field* should be excluded from POST payloads."""
    if field in EXCLUDED_FROM_POST:
        return True
    # Exclude any en_DEBUG locale variant
    if "_en_DEBUG" in field:
        return True
    # Exclude OData metadata / navigation link keys
    if field.startswith("__") or field == "__deferred":
        return True
    return False


def _copy_locale_fields(
    payload: Dict[str, Any],
    record: Dict[str, Any],
    prefix: str,
) -> None:
    """
    Copy every non-null locale variant of *prefix* that exists in *record*.

    Scans all keys matching ``<prefix>_*``, skipping excluded ones
    (en_DEBUG, _localized, system fields).  This captures whatever locales
    the PRD instance actually has - no hard-coded locale list required.
    """
#    for key in record:
#        if key.startswith(f"{prefix}_") and not _is_excluded(key):
#            # Skip the aggregated "_localized" pseudo-field (read-only)
#            if key == f"{prefix}_localized":
#                continue
#            _copy_if_present(payload, record, key)


    for field, value in record.items():
        # Match locale-specific fields like name_en_US
        if not field.startswith(f"{prefix}_"):
            continue

        if _is_excluded(field):
            continue

        val = _optional(record, field)
        if val is None:
            continue

        payload[field] = val
# ---------------------------------------------------------------------------
# Per-entity payload builders
# ---------------------------------------------------------------------------

def build_cust_SubDepartment(
    record: Dict[str, Any],
    parent_dept_code: str,
    parent_dept_start_date: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build POST payload for cust_SubDepartment.

    parent_dept_code       : externalCode of the parent FODepartment.
    parent_dept_start_date : startDate of the parent FODepartment (/Date(ms)/ string).
    base_url               : OData v2 base URL (e.g. https://.../odata/v2).
                             When provided together with parent_dept_start_date,
                             uses __metadata.uri to reference the existing entity
                             (avoids deep-insert / COE_GENERAL_BAD_REQUEST duplicate).
    """
    # Build the cust_Department nav-property reference.
    # OData v2 rule: to link to an EXISTING entity without creating it, use __metadata.uri.
    if base_url and parent_dept_start_date:
        dt_key = _odata_date_to_datetime_key(parent_dept_start_date)
        dept_nav: Dict[str, Any] = {
            "__metadata": {
                "uri": (
                    f"{base_url}/FODepartment("
                    f"externalCode='{parent_dept_code}',"
                    f"startDate=datetime'{dt_key}')"
                )
            }
        }
    else:
        dept_nav = {"externalCode": parent_dept_code}

    payload: Dict[str, Any] = {
        "externalCode": record["externalCode"],
        # Use the PRD effectiveStartDate so Dev matches the source system exactly.
        "effectiveStartDate": record["effectiveStartDate"],
        "mdfSystemStatus": record.get("mdfSystemStatus", "A"),
        # SF MDF OData v2: navigation properties must be inline objects, not plain strings.
        # "cust_Department" is a Valid When association.
        "cust_Department": dept_nav,
    }

    # Name and description - all locales present in the PRD record
    _copy_locale_fields(payload, record, "externalName")
    _copy_locale_fields(payload, record, "cust_description")

    # Optional extra fields
    # NOTE: mdfSystemRecordStatus is NOT insertable (COE_PROPERTY_NOT_EDITABLE)
    for field in ("cust_headOfUnit", "cust_costCenter"):
        _copy_if_present(payload, record, field)

    return payload


def build_FODepartment(
    record: Dict[str, Any],
    parent_division_code: str,
) -> Dict[str, Any]:
    """
    Build POST payload for FODepartment.

    parent_division_code: externalCode of the parent FODivision.
    NOTE: 'parent' field in FODepartment is self-referencing - DO NOT use
    as Division link. Division is always cust_DivisionProp.
    """
    payload: Dict[str, Any] = {
        "externalCode": record["externalCode"],
        # Copy startDate from PRD so Dev matches source system exactly (e.g. 1900-01-01).
        "startDate": record["startDate"],
        # endDate is NOT insertable in this tenant (COE_PROPERTY_NOT_EDITABLE).
        # SF sets it to 9999-12-31 automatically on POST.
        "status": "A",
        "cust_DivisionProp": parent_division_code,
    }

    # Name and description - all locales present in the PRD record
    _copy_locale_fields(payload, record, "name")
    _copy_locale_fields(payload, record, "description")

    # Optional
    _copy_if_present(payload, record, "headOfUnit")
    # NOTE: 'parent' (self-ref) is intentionally excluded

    return payload


def build_FODivision(
    record: Dict[str, Any],
    parent_bu_code: str,
) -> Dict[str, Any]:
    """
    Build POST payload for FODivision.

    parent_bu_code: externalCode of the parent FOBusinessUnit.
    NOTE: 'parent' in FODivision is self-referencing - DO NOT use as BU link.
    BU is always cust_BusinessUnitProp (REQUIRED by metadata).
    """
    payload: Dict[str, Any] = {
        "externalCode": record["externalCode"],
        # Copy startDate from PRD so Dev matches source system exactly.
        "startDate": record["startDate"],
        # endDate not insertable - SF sets automatically
        "status": "A",
        "cust_BusinessUnitProp": parent_bu_code,
    }

    # Name and description - all locales present in the PRD record
    _copy_locale_fields(payload, record, "name")
    _copy_locale_fields(payload, record, "description")

    # Optional
    _copy_if_present(payload, record, "headOfUnit")

    return payload


def build_FOBusinessUnit(
    record: Dict[str, Any],
    parent_le_code: str,
) -> Dict[str, Any]:
    """
    Build POST payload for FOBusinessUnit.

    parent_le_code: externalCode of the parent FOCompany (Legal Entity).
    NOTE: cust_parentBusinessUnit is a BU-to-BU self-ref hierarchy field;
    it is separate from the Legal Entity link (cust_legalEntityProp).
    """
    payload: Dict[str, Any] = {
        "externalCode": record["externalCode"],
        # Copy startDate from PRD so Dev matches source system exactly.
        "startDate": record["startDate"],
        # endDate not insertable - SF sets automatically
        "status": "A",
        "cust_legalEntityProp": parent_le_code,
    }

    # Name - all locales present in the PRD record
    _copy_locale_fields(payload, record, "name")

    # Optional fields
    _copy_if_present(payload, record, "cust_parentBusinessUnit")
    _copy_if_present(payload, record, "cust_businessUnitType")
    _copy_if_present(payload, record, "headOfUnit")

    return payload


def build_FOCompany(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build POST payload for FOCompany (Legal Entity).

    country and currency are REQUIRED and must be replicated from PRD.
    No parent - top of hierarchy.
    """
    payload: Dict[str, Any] = {
        "externalCode": record["externalCode"],
        # Copy startDate from PRD so Dev matches source system exactly.
        "startDate": record["startDate"],
        # endDate not insertable - SF sets automatically
        "status": "A",
        "country": record["country"],    # REQUIRED - raise if missing
        "currency": record["currency"],  # REQUIRED - raise if missing
    }

    # Name and description - all locales present in the PRD record
    _copy_locale_fields(payload, record, "name")
    _copy_locale_fields(payload, record, "description")

    return payload


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_BUILDERS = {
    "Sub Department": lambda record, parent_chain: build_cust_SubDepartment(
        record,
        parent_chain["Department"],
        parent_chain.get("_Department_startDate"),
        parent_chain.get("_base_url"),
    ),
    "Department": lambda record, parent_chain: build_FODepartment(
        record,
        parent_chain["Division"],
    ),
    "Division": lambda record, parent_chain: build_FODivision(
        record,
        parent_chain["Business Unit"],
    ),
    "Business Unit": lambda record, parent_chain: build_FOBusinessUnit(
        record,
        parent_chain["Legal Entity"],
    ),
    "Legal Entity": lambda record, parent_chain: build_FOCompany(record),
}


def build_payload(
    entity_type: str,
    prd_record: Dict[str, Any],
    parent_codes: Dict[str, str],
) -> Dict[str, Any]:
    """
    Construct the POST payload for *entity_type*.

    Args:
        entity_type  : canonical type name (e.g. "Department")
        prd_record   : full record dict fetched from PRD
        parent_codes : mapping of entity_type → externalCode for all ancestors
                       e.g. {"Division": "10012042", "Business Unit": "GF00006", ...}

    Returns dict ready to be JSON-serialised and POSTed.
    """
    if entity_type not in _BUILDERS:
        raise ValueError(f"No payload builder defined for entity type: '{entity_type}'")

    payload = _BUILDERS[entity_type](prd_record, parent_codes)
    logger.debug(
        "Built payload for %s '%s': %d fields",
        entity_type,
        prd_record.get("externalCode", "?"),
        len(payload),
    )
    return payload


def extract_parent_codes(
    chain: list,
) -> Dict[str, str]:
    """
    From a resolved hierarchy chain [(entity_type, code, record), ...],
    build a dict of {entity_type: externalCode} for all ancestors.

    Also embeds FO parent startDates under "_<Type>_startDate" keys so that
    builders that post nav-property inline objects (e.g. cust_Department for
    SubDepartment) can include the full FO composite key (externalCode + startDate).
    """
    result: Dict[str, Any] = {}
    for etype, code, record in chain:
        result[etype] = code
        # Embed startDate for FO entities (composite key field needed in nav objects).
        if "startDate" in record:
            result[f"_{etype}_startDate"] = record["startDate"]
    return result
