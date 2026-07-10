"""Tests for safe reconciliation of ambiguous SuccessFactors writes."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.audit_logger import UPLOAD_FAILED, UPLOAD_SUCCESS, VERIFICATION_OK
from src.sf_client import AmbiguousWriteError
from src.uploader import Uploader


def _uploader(*, reconciled_record):
    client = MagicMock()
    client.base_url = "https://api.example.com/odata/v2"
    client.post_entity.side_effect = AmbiguousWriteError(
        "POST outcome is unknown after a network error",
        method="POST",
        url="https://api.example.com/odata/v2/FODepartment",
    )
    client.get_entity_by_code.return_value = reconciled_record
    audit = MagicMock()
    uploader = Uploader(
        dev_client=client,
        prd_records={("Department", "DEPT-1"): {"externalCode": "DEPT-1"}},
        gap_results={("Department", "DEPT-1"): SimpleNamespace(status="DEV_MISSING")},
        audit=audit,
        dry_run=False,
    )
    return uploader, client, audit


@patch("src.uploader.build_payload", return_value={"externalCode": "DEPT-1"})
@patch("src.uploader._select_active_record", return_value={"externalCode": "DEPT-1"})
def test_ambiguous_post_is_reconciled_without_replay(_select, _build):
    uploader, client, audit = _uploader(reconciled_record=[{"externalCode": "DEPT-1"}])

    uploader._upload_one("Department", "DEPT-1", [])

    client.post_entity.assert_called_once()
    client.get_entity_by_code.assert_called_once_with("FODepartment", "DEPT-1")
    statuses = [call.kwargs["status"] for call in audit.log.call_args_list]
    assert statuses == [UPLOAD_SUCCESS, VERIFICATION_OK]
    assert audit.log.call_args_list[0].kwargs["response_received"] == {"reconciled": True}
    assert uploader._failed_parents == set()


@patch("src.uploader.build_payload", return_value={"externalCode": "DEPT-1"})
def test_unresolved_ambiguous_post_fails_without_replay(_build):
    uploader, client, audit = _uploader(reconciled_record=[])

    uploader._upload_one("Department", "DEPT-1", [])

    client.post_entity.assert_called_once()
    client.get_entity_by_code.assert_called_once_with("FODepartment", "DEPT-1")
    statuses = [call.kwargs["status"] for call in audit.log.call_args_list]
    assert statuses == [UPLOAD_FAILED]
    assert ("Department", "DEPT-1") in uploader._failed_parents
