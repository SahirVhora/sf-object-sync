"""
Tests for HierarchyResolver.

All tests mock SFClient — no live API calls.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.hierarchy_resolver import HierarchyResolver, HierarchyBrokenError, EntityNotFoundError
from src.sf_client import SFClient


def _load_mock(filename: str) -> dict:
    """Load a mock response JSON file."""
    path = os.path.join(os.path.dirname(__file__), "mock_responses", filename)
    with open(path) as fh:
        return json.load(fh)


def _results(filename: str) -> list:
    return _load_mock(filename)["d"]["results"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_prd():
    """
    Return a MagicMock SFClient configured with mock PRD responses.

    FODepartment, FODivision, FOBusinessUnit records already contain the
    expanded parent nav property inline (as the real API returns with $expand).
    """
    client = MagicMock(spec=SFClient)

    subdept_records = _results("cust_SubDepartment_sample.json")
    # These include embedded parent nav data (cust_Division / cust_BusinessUnit /
    # cust_legalEntity) to mirror a real $expand=<nav> response.
    dept_records    = _results("FODepartment_sample.json")
    div_records     = _results("FODivision_sample.json")
    bu_records      = _results("FOBusinessUnit_sample.json")
    le_records      = _results("FOCompany_sample.json")

    def side_effect(entity_set: str, code: str, **kwargs):
        mapping = {
            ("cust_SubDepartment", "10000073"): subdept_records,
            ("FODepartment",       "10016236"): dept_records,
            ("FODivision",         "10012042"): div_records,
            ("FOBusinessUnit",     "GF00006"):  bu_records,
            ("FOCompany",          "LE_CORP"):  le_records,
        }
        return mapping.get((entity_set, code), [])

    client.get_entity_by_code.side_effect = side_effect
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHierarchyResolver:
    def test_full_chain_from_sub_department(self, mock_prd):
        resolver = HierarchyResolver(mock_prd)
        chain = resolver.resolve("Sub Department", "10000073")

        assert len(chain) == 5
        types = [c[0] for c in chain]
        codes = [c[1] for c in chain]

        assert types == [
            "Sub Department",
            "Department",
            "Division",
            "Business Unit",
            "Legal Entity",
        ]
        assert codes == ["10000073", "10016236", "10012042", "GF00006", "LE_CORP"]

    def test_chain_from_department(self, mock_prd):
        resolver = HierarchyResolver(mock_prd)
        chain = resolver.resolve("Department", "10016236")

        assert len(chain) == 4
        assert chain[0][0] == "Department"
        assert chain[-1][0] == "Legal Entity"

    def test_chain_from_legal_entity_stops(self, mock_prd):
        resolver = HierarchyResolver(mock_prd)
        chain = resolver.resolve("Legal Entity", "LE_CORP")

        assert len(chain) == 1
        assert chain[0][0] == "Legal Entity"
        assert chain[0][1] == "LE_CORP"

    def test_entity_not_found_raises(self, mock_prd):
        resolver = HierarchyResolver(mock_prd)
        with pytest.raises(EntityNotFoundError) as exc_info:
            resolver.resolve("Sub Department", "NONEXISTENT")
        assert "NONEXISTENT" in str(exc_info.value)

    def test_hierarchy_broken_when_subdept_has_no_department_nav(self):
        """SubDept with empty cust_Department nav should raise HierarchyBrokenError."""
        broken_records = _results("cust_SubDepartment_sample.json")
        broken_records[0]["cust_Department"] = {"results": []}  # nav expanded but empty

        client = MagicMock(spec=SFClient)
        client.get_entity_by_code.side_effect = lambda es, code, **kw: (
            broken_records if (es == "cust_SubDepartment" and code == "10000073") else []
        )

        resolver = HierarchyResolver(client)
        with pytest.raises(HierarchyBrokenError) as exc_info:
            resolver.resolve("Sub Department", "10000073")
        assert "cust_Department" in str(exc_info.value)

    def test_hierarchy_broken_when_subdept_nav_absent(self):
        """SubDept without cust_Department key at all → HierarchyBrokenError."""
        broken_records = _results("cust_SubDepartment_sample.json")
        broken_records[0].pop("cust_Department", None)

        client = MagicMock(spec=SFClient)
        client.get_entity_by_code.side_effect = lambda es, code, **kw: (
            broken_records if (es == "cust_SubDepartment" and code == "10000073") else []
        )

        resolver = HierarchyResolver(client)
        with pytest.raises(HierarchyBrokenError):
            resolver.resolve("Sub Department", "10000073")

    def test_hierarchy_broken_when_dept_has_no_division_nav(self):
        """FODepartment with empty cust_Division nav should raise HierarchyBrokenError."""
        broken_dept = _results("FODepartment_sample.json")
        broken_dept[0]["cust_Division"] = {"results": []}

        client = MagicMock(spec=SFClient)
        client.get_entity_by_code.side_effect = lambda es, code, **kw: (
            broken_dept if (es == "FODepartment" and code == "10016236") else []
        )

        resolver = HierarchyResolver(client)
        with pytest.raises(HierarchyBrokenError):
            resolver.resolve("Department", "10016236")

    def test_caching_avoids_duplicate_api_calls(self, mock_prd):
        resolver = HierarchyResolver(mock_prd)
        resolver.resolve("Sub Department", "10000073")
        call_count_after_first = mock_prd.get_entity_by_code.call_count

        # Second resolve of same sub-dept should use full cache
        resolver.resolve("Sub Department", "10000073")
        assert mock_prd.get_entity_by_code.call_count == call_count_after_first

    def test_prime_cache_prevents_api_call(self):
        client = MagicMock(spec=SFClient)
        le_record = _results("FOCompany_sample.json")[0]

        resolver = HierarchyResolver(client)
        resolver.prime_cache("Legal Entity", "LE_CORP", le_record)
        chain = resolver.resolve("Legal Entity", "LE_CORP")

        assert len(chain) == 1
        client.get_entity_by_code.assert_not_called()

    def test_correct_parent_nav_used_not_self_ref(self, mock_prd):
        """Ensure cust_Division nav (not 'parent' self-ref) is used for FODepartment."""
        resolver = HierarchyResolver(mock_prd)
        chain = resolver.resolve("Department", "10016236")

        # Department → Division via cust_Division nav expand
        parent_entry = chain[1]
        assert parent_entry[0] == "Division"
        assert parent_entry[1] == "10012042"

    def test_correct_bu_nav_used_for_division(self, mock_prd):
        """Ensure cust_BusinessUnit nav (not 'parent') is used for FODivision."""
        resolver = HierarchyResolver(mock_prd)
        chain = resolver.resolve("Division", "10012042")

        bu_entry = chain[1]
        assert bu_entry[0] == "Business Unit"
        assert bu_entry[1] == "GF00006"

    def test_correct_le_nav_used_for_bu(self, mock_prd):
        """Ensure cust_legalEntity nav (not cust_parentBusinessUnit) is used for BU."""
        resolver = HierarchyResolver(mock_prd)
        chain = resolver.resolve("Business Unit", "GF00006")

        le_entry = chain[1]
        assert le_entry[0] == "Legal Entity"
        assert le_entry[1] == "LE_CORP"
