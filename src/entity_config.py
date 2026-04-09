"""
Entity configuration registry for SAP SuccessFactors OM foundation objects.

ENTITY_CONFIG drives all phases: fetching, parent traversal, gap checking,
and payload construction.  Each key is the canonical object type name used
throughout the tool (matching valid values in the input.xlsx "Object" column).
"""

from typing import Dict, Any, Optional

# ---------------------------------------------------------------------------
# Read-only fields excluded from every POST payload
# (system-managed, navigation properties, debug locales)
# ---------------------------------------------------------------------------
EXCLUDED_FROM_POST: frozenset = frozenset([
    "createdBy",
    "createdDateTime",
    "createdOn",
    "lastModifiedBy",
    "lastModifiedDateTime",
    "lastModifiedOn",
    "entityUUID",
    "entityOID",
    "mdfSystemRecordId",
    "name_localized",
    "externalName_localized",
    "description_localized",
    "cust_description_localized",
    # debug locale variants
    "name_en_DEBUG",
    "externalName_en_DEBUG",
    "description_en_DEBUG",
    "cust_description_en_DEBUG",
    # OData metadata / navigation links
    "__metadata",
])

# ---------------------------------------------------------------------------
# Infinity end-date epoch (9999-12-31T00:00:00Z) in milliseconds
# ---------------------------------------------------------------------------
INFINITY_DATE_MS: int = 253_370_764_800_000

# ---------------------------------------------------------------------------
# Main entity registry
# ---------------------------------------------------------------------------
ENTITY_CONFIG: Dict[str, Dict[str, Any]] = {
    "Sub Department": {
        # OData entity set name
        "entity_set": "cust_SubDepartment",
        # Fields that together form the OData composite key
        "key_fields": ["effectiveStartDate", "externalCode"],
        # The date field used to identify the latest / active record
        "date_field": "effectiveStartDate",
        # Status field and active value
        "status_field": "mdfSystemStatus",
        #"active_status_value": "A",
        "active_status_value": "mdfSystemStatus",
        # The Department link is maintained via the "cust_Department" OData
        # association (One To Many, Valid When type).  The "cust_parentDepartment"
        # string field is null in GET responses — the parent is resolved by
        # $expand=cust_Department, same as the FO entity pattern.
        "parent_field": None,
        "parent_nav": "cust_Department",
        # Canonical name of the parent entity type (must be a key in ENTITY_CONFIG)
        "parent_entity": "Department",
        # Field used to detect the open-ended (active) record: value = 9999-12-31
        "end_date_field": "mdfSystemEffectiveEndDate",
        # Prefix for name localisation fields  (e.g. externalName_en_US)
        "name_field_prefix": "externalName",
        # Prefix for description localisation fields (e.g. cust_description_en_US)
        "desc_field_prefix": "cust_description",
        # Hierarchy depth: 5 = deepest (child), 1 = top (Legal Entity)
        "level": 5,
    },
    "Department": {
        "entity_set": "FODepartment",
        "key_fields": ["externalCode", "startDate"],
        "date_field": "startDate",
        "status_field": "status",
        #"active_status_value": "A",
        "active_status_value": "status",
        # FODepartment has no plain FK field for its Division.
        # The Division is reached via the "cust_Division" OData navigation property.
        # NOTE: "parent" is a self-referencing Dept→Dept field — do NOT use it.
        "parent_field": None,
        "parent_nav": "cust_Division",       # $expand this to get the parent Division record
        "parent_entity": "Division",
        "end_date_field": "endDate",
        "name_field_prefix": "name",
        "desc_field_prefix": "description",
        "level": 4,
    },
    "Division": {
        "entity_set": "FODivision",
        "key_fields": ["externalCode", "startDate"],
        "date_field": "startDate",
        "status_field": "status",
        #"active_status_value": "A",
        "active_status_value": "status",
        # FODivision has no plain FK field for its Business Unit.
        # NOTE: "parent" is a self-referencing Div→Div field — do NOT use it.
        "parent_field": None,
        "parent_nav": "cust_BusinessUnit",   # $expand this to get the parent BU record
        "parent_entity": "Business Unit",
        "end_date_field": "endDate",
        "name_field_prefix": "name",
        "desc_field_prefix": "description",
        "level": 3,
    },
    "Business Unit": {
        "entity_set": "FOBusinessUnit",
        "key_fields": ["externalCode", "startDate"],
        "date_field": "startDate",
        "status_field": "status",
        #"active_status_value": "A",
        "active_status_value": "status",
        # NOTE: cust_parentBusinessUnit is a BU→BU self-ref; do NOT use it for LE.
        "parent_field": None,
        "parent_nav": "cust_legalEntity",    # $expand this to get the parent Legal Entity record
        "parent_entity": "Legal Entity",
        "end_date_field": "endDate",
        "name_field_prefix": "name",
        "desc_field_prefix": "description",
        "level": 2,
    },
    "Legal Entity": {
        "entity_set": "FOCompany",
        "key_fields": ["externalCode", "startDate"],
        "date_field": "startDate",
        "status_field": "status",
        #"active_status_value": "A",
        "active_status_value": "status",
        "parent_field": None,    # top of hierarchy — no parent
        "parent_nav": None,
        "parent_entity": None,
        "end_date_field": "endDate",
        "name_field_prefix": "name",
        "desc_field_prefix": "description",
        "level": 1,
    },
}

# Ordered list from top (Legal Entity) to bottom (Sub Department) for upload sequencing
UPLOAD_ORDER: list = [
    "Legal Entity",
    "Business Unit",
    "Division",
    "Department",
    "Sub Department",
]

# Canonical object types accepted in the input file
INPUT_VALID_TYPES: frozenset = frozenset(["Sub Department", "Department"])


def get_config(entity_type: str) -> Dict[str, Any]:
    """Return ENTITY_CONFIG entry; raises KeyError for unknown types."""
    if entity_type not in ENTITY_CONFIG:
        raise KeyError(f"Unknown entity type: '{entity_type}'")
    return ENTITY_CONFIG[entity_type]
