"""
Tests for sf_client._odata_escape.

Verifies the OData v2 $filter escaping is correct (prevents injection) AND
byte-identical for normal SuccessFactors codes (so live behaviour is unchanged).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.sf_client import _odata_escape


class TestOdataEscape:
    def test_normal_code_unchanged(self):
        # Real SF externalCodes are alphanumeric - output must equal input.
        for code in ["IT", "DEPT_001", "1000123", "FO-Dept.A", "a_b-c"]:
            assert _odata_escape(code) == code

    def test_single_quote_doubled(self):
        assert _odata_escape("O'Brien") == "O''Brien"

    def test_injection_attempt_neutralised(self):
        raw = "x' or externalCode ne 'y"
        literal = f"externalCode eq '{_odata_escape(raw)}'"
        # The crafted quote can no longer terminate the literal early.
        assert literal == "externalCode eq 'x'' or externalCode ne ''y'"

    def test_multiple_quotes(self):
        assert _odata_escape("a'b'c") == "a''b''c"

    def test_empty_string(self):
        assert _odata_escape("") == ""

    def test_non_string_coerced(self):
        assert _odata_escape(123) == "123"
