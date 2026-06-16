"""
Tests for Phase 1 input validation logic.
"""

import os
import sys
import tempfile

import openpyxl
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.audit_logger import AuditLogger, VALIDATION_FAILED
from sf_object_sync import phase1_validate_input, _normalise_object_type


def _make_xlsx(rows, headers=("Object", "Code")):
    """Create a temporary xlsx file with given rows and return its path."""
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp.close()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(headers))
    for row in rows:
        ws.append(list(row))
    wb.save(tmp.name)
    return tmp.name


@pytest.fixture
def audit(tmp_path):
    with AuditLogger(str(tmp_path), dry_run=True) as al:
        yield al


class TestNormaliseObjectType:
    def test_exact_match_sub_department(self):
        assert _normalise_object_type("Sub Department") == "Sub Department"

    def test_exact_match_department(self):
        assert _normalise_object_type("Department") == "Department"

    def test_case_insensitive(self):
        assert _normalise_object_type("SUB DEPARTMENT") == "Sub Department"
        assert _normalise_object_type("department") == "Department"
        assert _normalise_object_type("sub department") == "Sub Department"

    def test_leading_trailing_whitespace(self):
        assert _normalise_object_type("  Sub Department  ") == "Sub Department"

    def test_invalid_type(self):
        assert _normalise_object_type("Division") is None
        assert _normalise_object_type("") is None
        assert _normalise_object_type("SomethingElse") is None


class TestPhase1ValidateInput:
    def test_valid_rows(self, audit, tmp_path):
        path = _make_xlsx([
            ("Sub Department", "10000073"),
            ("Department", "10016236"),
        ])
        errors = []
        result = phase1_validate_input(path, audit, errors)
        assert len(result) == 2
        assert result[0] == {"object_type": "Sub Department", "code": "10000073"}
        assert result[1] == {"object_type": "Department", "code": "10016236"}
        assert errors == []
        os.unlink(path)

    def test_case_insensitive_object(self, audit, tmp_path):
        path = _make_xlsx([("sub department", "10000073")])
        errors = []
        result = phase1_validate_input(path, audit, errors)
        assert len(result) == 1
        assert result[0]["object_type"] == "Sub Department"
        os.unlink(path)

    def test_invalid_object_type_rejected(self, audit, tmp_path):
        path = _make_xlsx([
            ("Division", "10012042"),
            ("Department", "10016236"),
        ])
        errors = []
        result = phase1_validate_input(path, audit, errors)
        assert len(result) == 1
        assert result[0]["object_type"] == "Department"
        assert len(errors) == 1
        assert errors[0]["input_object"] == "Division"
        os.unlink(path)

    def test_empty_code_rejected(self, audit, tmp_path):
        path = _make_xlsx([("Sub Department", "")])
        errors = []
        with pytest.raises(SystemExit):
            phase1_validate_input(path, audit, errors)
        assert len(errors) == 1
        assert "empty" in errors[0]["reason"].lower()
        os.unlink(path)

    def test_blank_rows_skipped(self, audit, tmp_path):
        path = _make_xlsx([
            ("Sub Department", "10000073"),
            ("", ""),
            ("Department", "10016236"),
        ])
        errors = []
        result = phase1_validate_input(path, audit, errors)
        assert len(result) == 2
        os.unlink(path)

    def test_case_insensitive_headers(self, audit, tmp_path):
        path = _make_xlsx(
            [("Sub Department", "10000073")],
            headers=("OBJECT", "CODE"),
        )
        errors = []
        result = phase1_validate_input(path, audit, errors)
        assert len(result) == 1
        os.unlink(path)

    def test_alphanumeric_codes_with_hyphen(self, audit, tmp_path):
        # Hyphen is accepted (common in SF codes)
        path = _make_xlsx([("Department", "GF-00006")])
        errors = []
        result = phase1_validate_input(path, audit, errors)
        assert len(result) == 1
        os.unlink(path)

    def test_audit_log_validation_failed(self, audit, tmp_path):
        path = _make_xlsx([("Legal Entity", "LE001")])
        errors = []
        with pytest.raises(SystemExit):
            phase1_validate_input(path, audit, errors)
        failed = [r for r in audit.all_records() if r["status"] == VALIDATION_FAILED]
        assert len(failed) == 1
        os.unlink(path)
