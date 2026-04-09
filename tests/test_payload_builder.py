"""
Tests for payload_builder.py.

Verifies correct field selection, excluded fields, date format, and
parent code injection per entity type.
"""

import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.payload_builder import (
    build_payload,
    build_cust_SubDepartment,
    build_FODepartment,
    build_FODivision,
    build_FOBusinessUnit,
    build_FOCompany,
    extract_parent_codes,
    INFINITY_DATE_STR,
)

DATE_PATTERN = re.compile(r"^/Date\(\d+\)/$")


def _assert_date(val: str, msg: str = "") -> None:
    assert DATE_PATTERN.match(val), f"Expected /Date(<ms>)/ format, got: {val!r}  {msg}"


# ---------------------------------------------------------------------------
# Sample records mirroring mock_responses/
# ---------------------------------------------------------------------------

SUBDEPT_RECORD = {
    "externalCode": "10000073",
    "effectiveStartDate": "/Date(1577836800000)/",
    "mdfSystemStatus": "A",
    "mdfSystemEffectiveEndDate": "/Date(253370764800000)/",
    "externalName_defaultValue": "Test Sub Dept",
    "externalName_en_US": "Test Sub Dept US",
    "externalName_en_GB": "Test Sub Dept GB",
    "externalName_en_DEBUG": "[D]Test Sub Dept",
    "externalName_pl_PL": "Testowy Poddzial",
    "cust_description_defaultValue": "A test sub dept",
    "cust_description_en_US": "Test US desc",
    "cust_description_en_GB": None,
    "cust_Department": "10016236",
    "cust_headOfUnit": None,
    "cust_costCenter": None,
    "createdBy": "sfadmin",
    "entityUUID": "aabb-1234",
    "name_localized": "Test",
}

DEPT_RECORD = {
    "externalCode": "10016236",
    "startDate": "/Date(1577836800000)/",
    "endDate": "/Date(253370764800000)/",
    "status": "A",
    "name_defaultValue": "Finance Dept",
    "name_en_US": "Finance Dept US",
    "name_en_GB": "Finance Dept GB",
    "name_en_DEBUG": "[D]Finance Dept",
    "name_pl_PL": "Dzial Finansowy",
    "description_defaultValue": "Finance",
    "description_en_US": "Finance US",
    "description_en_GB": None,
    "cust_DivisionProp": "10012042",
    "parent": None,
    "headOfUnit": None,
    "createdBy": "sfadmin",
    "entityUUID": "bbcc-2345",
}

DIV_RECORD = {
    "externalCode": "10012042",
    "startDate": "/Date(1577836800000)/",
    "endDate": "/Date(253370764800000)/",
    "status": "A",
    "name_defaultValue": "European Div",
    "name_en_US": "European Div US",
    "name_en_GB": "European Div GB",
    "name_en_DEBUG": "[D]European Div",
    "name_pl_PL": "Europejski Dzial",
    "description_defaultValue": "European Ops",
    "cust_BusinessUnitProp": "GF00006",
    "parent": None,
    "headOfUnit": None,
    "createdBy": "sfadmin",
    "entityUUID": "ccdd-3456",
}

BU_RECORD = {
    "externalCode": "GF00006",
    "startDate": "/Date(1577836800000)/",
    "endDate": "/Date(253370764800000)/",
    "status": "A",
    "name_defaultValue": "Global Finance BU",
    "name_en_US": "Global Finance BU US",
    "name_en_GB": "Global Finance BU GB",
    "name_en_DEBUG": "[D]Global Finance BU",
    "name_pl_PL": "Globalny Dzial Finansowy",
    "cust_legalEntityProp": "LE_CORP",
    "cust_parentBusinessUnit": None,
    "cust_businessUnitType": "FINANCIAL",
    "headOfUnit": None,
    "createdBy": "sfadmin",
    "entityUUID": "ddee-4567",
}

LE_RECORD = {
    "externalCode": "LE_CORP",
    "startDate": "/Date(1577836800000)/",
    "endDate": "/Date(253370764800000)/",
    "status": "A",
    "country": "GBR",
    "currency": "GBP",
    "name_defaultValue": "Corp LE",
    "name_en_US": "Corp LE US",
    "name_en_GB": "Corp LE GB",
    "name_pl_PL": "Podmiot Corp",
    "description_defaultValue": "Main Corp LE",
    "createdBy": "sfadmin",
    "entityUUID": "eeff-5678",
}

FULL_CHAIN = [
    ("Sub Department", "10000073", SUBDEPT_RECORD),
    ("Department", "10016236", DEPT_RECORD),
    ("Division", "10012042", DIV_RECORD),
    ("Business Unit", "GF00006", BU_RECORD),
    ("Legal Entity", "LE_CORP", LE_RECORD),
]

PARENT_CODES = extract_parent_codes(FULL_CHAIN)


# ---------------------------------------------------------------------------
# cust_SubDepartment
# ---------------------------------------------------------------------------

class TestBuildSubDepartment:
    def test_required_fields_present(self):
        payload = build_cust_SubDepartment(SUBDEPT_RECORD, "10016236")
        assert "externalCode" in payload
        assert "effectiveStartDate" in payload
        assert "mdfSystemStatus" in payload
        assert "cust_Department" in payload

    def test_status_is_A(self):
        payload = build_cust_SubDepartment(SUBDEPT_RECORD, "10016236")
        assert payload["mdfSystemStatus"] == "A"

    def test_parent_department_set_correctly(self):
        payload = build_cust_SubDepartment(SUBDEPT_RECORD, "10016236")
        # Nav property must be an inline object (SF MDF OData v2 requirement)
        assert payload["cust_Department"] == {"externalCode": "10016236"}

    def test_parent_department_uri_with_pre_1970_start_date(self):
        base_url = "https://example.com/odata/v2"
        parent_codes = {
            "Department": "10016236",
            "_Department_startDate": "/Date(-2208960000000)/",
            "_base_url": base_url,
        }
        payload = build_payload("Sub Department", SUBDEPT_RECORD, parent_codes)
        assert payload["cust_Department"]["__metadata"]["uri"] == (
            "https://example.com/odata/v2/FODepartment("
            "externalCode='10016236',"
            "startDate=datetime'1900-01-01T08:00:00')"
        )

    def test_effectiveStartDate_copied_from_prd(self):
        payload = build_cust_SubDepartment(SUBDEPT_RECORD, "10016236")
        # Must be the PRD value, not today's date
        assert payload["effectiveStartDate"] == SUBDEPT_RECORD["effectiveStartDate"]
        _assert_date(payload["effectiveStartDate"])

    def test_name_fields_included(self):
        payload = build_cust_SubDepartment(SUBDEPT_RECORD, "10016236")
        assert payload.get("externalName_defaultValue") == "Test Sub Dept"
        assert payload.get("externalName_en_US") == "Test Sub Dept US"
        assert payload.get("externalName_en_GB") == "Test Sub Dept GB"
        assert payload.get("externalName_pl_PL") == "Testowy Poddzial"

    def test_debug_locale_excluded(self):
        payload = build_cust_SubDepartment(SUBDEPT_RECORD, "10016236")
        assert "externalName_en_DEBUG" not in payload

    def test_null_optional_fields_excluded(self):
        payload = build_cust_SubDepartment(SUBDEPT_RECORD, "10016236")
        assert "cust_description_en_GB" not in payload  # was null in record
        assert "cust_headOfUnit" not in payload
        assert "cust_costCenter" not in payload

    def test_system_fields_excluded(self):
        payload = build_cust_SubDepartment(SUBDEPT_RECORD, "10016236")
        assert "createdBy" not in payload
        assert "entityUUID" not in payload
        assert "name_localized" not in payload


# ---------------------------------------------------------------------------
# FODepartment
# ---------------------------------------------------------------------------

class TestBuildDepartment:
    def test_required_fields_present(self):
        payload = build_FODepartment(DEPT_RECORD, "10012042")
        assert "externalCode" in payload
        assert "startDate" in payload
        assert "endDate" not in payload  # not insertable — SF sets automatically
        assert "status" in payload
        assert "cust_DivisionProp" in payload

    def test_division_prop_set(self):
        payload = build_FODepartment(DEPT_RECORD, "10012042")
        assert payload["cust_DivisionProp"] == "10012042"

    def test_parent_self_ref_excluded(self):
        payload = build_FODepartment(DEPT_RECORD, "10012042")
        assert "parent" not in payload

    def test_end_date_not_in_payload(self):
        payload = build_FODepartment(DEPT_RECORD, "10012042")
        assert "endDate" not in payload  # not insertable — SF sets automatically

    def test_start_date_today_format(self):
        payload = build_FODepartment(DEPT_RECORD, "10012042")
        _assert_date(payload["startDate"])

    def test_null_description_excluded(self):
        payload = build_FODepartment(DEPT_RECORD, "10012042")
        assert "description_en_GB" not in payload

    def test_name_locales_included(self):
        payload = build_FODepartment(DEPT_RECORD, "10012042")
        assert payload.get("name_defaultValue") == "Finance Dept"
        assert payload.get("name_en_US") == "Finance Dept US"
        assert payload.get("name_pl_PL") == "Dzial Finansowy"

    def test_debug_locale_excluded(self):
        payload = build_FODepartment(DEPT_RECORD, "10012042")
        assert "name_en_DEBUG" not in payload


# ---------------------------------------------------------------------------
# FODivision
# ---------------------------------------------------------------------------

class TestBuildDivision:
    def test_bu_prop_set(self):
        payload = build_FODivision(DIV_RECORD, "GF00006")
        assert payload["cust_BusinessUnitProp"] == "GF00006"

    def test_parent_self_ref_excluded(self):
        payload = build_FODivision(DIV_RECORD, "GF00006")
        assert "parent" not in payload

    def test_end_date_not_in_payload(self):
        payload = build_FODivision(DIV_RECORD, "GF00006")
        assert "endDate" not in payload  # not insertable — SF sets automatically

    def test_status_A(self):
        payload = build_FODivision(DIV_RECORD, "GF00006")
        assert payload["status"] == "A"

    def test_debug_locale_excluded(self):
        payload = build_FODivision(DIV_RECORD, "GF00006")
        assert "name_en_DEBUG" not in payload


# ---------------------------------------------------------------------------
# FOBusinessUnit
# ---------------------------------------------------------------------------

class TestBuildBusinessUnit:
    def test_legal_entity_prop_set(self):
        payload = build_FOBusinessUnit(BU_RECORD, "LE_CORP")
        assert payload["cust_legalEntityProp"] == "LE_CORP"

    def test_parent_bu_null_excluded(self):
        payload = build_FOBusinessUnit(BU_RECORD, "LE_CORP")
        assert "cust_parentBusinessUnit" not in payload  # was null

    def test_bu_type_included(self):
        payload = build_FOBusinessUnit(BU_RECORD, "LE_CORP")
        assert payload.get("cust_businessUnitType") == "FINANCIAL"

    def test_end_date_not_in_payload(self):
        payload = build_FOBusinessUnit(BU_RECORD, "LE_CORP")
        assert "endDate" not in payload  # not insertable — SF sets automatically


# ---------------------------------------------------------------------------
# FOCompany
# ---------------------------------------------------------------------------

class TestBuildLegalEntity:
    def test_country_required(self):
        payload = build_FOCompany(LE_RECORD)
        assert payload["country"] == "GBR"

    def test_currency_required(self):
        payload = build_FOCompany(LE_RECORD)
        assert payload["currency"] == "GBP"

    def test_raises_if_country_missing(self):
        bad_record = dict(LE_RECORD)
        del bad_record["country"]
        with pytest.raises(KeyError):
            build_FOCompany(bad_record)

    def test_no_parent_field(self):
        payload = build_FOCompany(LE_RECORD)
        assert "cust_legalEntityProp" not in payload
        assert "parent" not in payload

    def test_end_date_not_in_payload(self):
        payload = build_FOCompany(LE_RECORD)
        assert "endDate" not in payload  # not insertable — SF sets automatically


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class TestBuildPayloadDispatcher:
    def test_sub_department_dispatched(self):
        payload = build_payload("Sub Department", SUBDEPT_RECORD, PARENT_CODES)
        assert payload["cust_Department"] == {"externalCode": "10016236"}

    def test_department_dispatched(self):
        payload = build_payload("Department", DEPT_RECORD, PARENT_CODES)
        assert payload["cust_DivisionProp"] == "10012042"

    def test_division_dispatched(self):
        payload = build_payload("Division", DIV_RECORD, PARENT_CODES)
        assert payload["cust_BusinessUnitProp"] == "GF00006"

    def test_business_unit_dispatched(self):
        payload = build_payload("Business Unit", BU_RECORD, PARENT_CODES)
        assert payload["cust_legalEntityProp"] == "LE_CORP"

    def test_legal_entity_dispatched(self):
        payload = build_payload("Legal Entity", LE_RECORD, PARENT_CODES)
        assert payload["country"] == "GBR"

    def test_unknown_entity_raises(self):
        with pytest.raises(ValueError):
            build_payload("Unknown Type", {}, {})


# ---------------------------------------------------------------------------
# extract_parent_codes
# ---------------------------------------------------------------------------

class TestExtractParentCodes:
    def test_extracts_all_levels(self):
        codes = extract_parent_codes(FULL_CHAIN)
        assert codes["Sub Department"] == "10000073"
        assert codes["Department"] == "10016236"
        assert codes["Division"] == "10012042"
        assert codes["Business Unit"] == "GF00006"
        assert codes["Legal Entity"] == "LE_CORP"
