import os
import sys
import time
from io import BytesIO
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Import after sys.path bootstrap so 'web_ui.app' is resolvable.
from web_ui.app import app  # noqa: E402


def _client():
    client = app.test_client()
    with client.session_transaction() as session:
        session["csrf_token"] = "test-csrf"
    client.environ_base["HTTP_X_CSRF_TOKEN"] = "test-csrf"
    return client


def test_connection_api_reports_source_and_target_success():
    client = _client()
    payload = {
        "auth_method": "basic",
        "source_url": "https://prd.example.com/odata/v2",
        "source_user": "source_user",
        "source_password": "source_pass",
        "target_url": "https://dev.example.com/odata/v2",
        "target_user": "target_user",
        "target_password": "target_pass",
    }

    with patch(
        "web_ui.app._probe_connection", return_value=(True, "Connection successful")
    ):
        response = client.post("/api/test_connection", json=payload)

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["source"]["ok"] is True
    assert body["target"]["ok"] is True


def test_connection_api_normalises_tenant_host_urls():
    client = _client()
    payload = {
        "auth_method": "basic",
        "source_url": "https://api55.sapsf.eu",
        "source_user": "source_user",
        "source_password": "source_pass",
        "target_url": "https://api55preview.sapsf.eu/",
        "target_user": "target_user",
        "target_password": "target_pass",
    }
    probed_urls = []

    def fake_probe(sf_client):
        probed_urls.append(sf_client.base_url)
        return True, "Connection successful"

    with patch("web_ui.app._probe_connection", fake_probe):
        response = client.post("/api/test_connection", json=payload)

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert probed_urls == [
        "https://api55.sapsf.eu/odata/v2",
        "https://api55preview.sapsf.eu/odata/v2",
    ]


def test_connection_api_validates_required_urls():
    client = _client()

    response = client.post("/api/test_connection", json={"auth_method": "basic"})

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is False
    assert body["source"]["ok"] is False
    assert body["target"]["ok"] is False


def test_settings_script_does_not_persist_sensitive_fields():
    script = open(
        os.path.join(_ROOT, "web_ui", "static", "script.js"), encoding="utf-8"
    ).read()

    assert "const SENSITIVE_FIELDS" in script
    assert "!SENSITIVE_FIELDS.has(id)" in script
    assert "delete settings[id]" in script
    assert "hidden.value = modal.value" in script


def test_upload_size_limit_returns_413():
    client = _client()
    original_limit = app.config["MAX_CONTENT_LENGTH"]
    app.config["MAX_CONTENT_LENGTH"] = 8
    try:
        response = client.post(
            "/process",
            data={"input_file": (BytesIO(b"x" * 32), "input.xlsx")},
            content_type="multipart/form-data",
        )
    finally:
        app.config["MAX_CONTENT_LENGTH"] = original_limit

    assert response.status_code == 413
    assert b"Uploaded file is too large" in response.data


def test_background_sync_exception_marks_run_failed():
    client = _client()

    def boom(**_kwargs):
        raise RuntimeError("simulated failure")

    with patch("web_ui.app.sync_objects", boom):
        response = client.post(
            "/process",
            data={
                "source_url": "https://prd.example.com/odata/v2",
                "target_url": "https://dev.example.com/odata/v2",
                "dry_run": "on",
                "input_file": (BytesIO(b"not a real workbook"), "input.xlsx"),
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )

    assert response.status_code == 302
    run_id = response.headers["Location"].rstrip("/").split("/")[-1]

    for _ in range(30):
        status = client.get(f"/api/status/{run_id}").get_json()
        if status["status"] == "failed":
            break
        time.sleep(0.05)
    else:
        raise AssertionError("background run did not fail")

    assert status["percent"] == 100
    assert "Sync failed" in status["message"]


def test_cross_origin_sync_cannot_use_saved_credentials(monkeypatch):
    monkeypatch.setenv("SF_SOURCE_PASSWORD", "synthetic")
    with patch("web_ui.app.sync_objects") as sync:
        response = app.test_client().post(
            "/process",
            headers={"Origin": "https://untrusted.example"},
            data={"input_file": (BytesIO(b"x"), "input.csv")},
        )
    assert response.status_code == 403
    sync.assert_not_called()


def test_missing_csrf_token_is_rejected():
    response = app.test_client().post("/api/test_connection", json={})
    assert response.status_code == 403


def test_write_mode_requires_explicit_confirmation(tmp_path):
    import importlib

    module = importlib.import_module("web_ui.app")

    class InlineThread:
        def __init__(self, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    for confirmation, expected_dry_run in [(None, True), ("yes", False)]:
        data = {
            "source_url": "https://source.example",
            "target_url": "https://target.example",
            "input_file": (BytesIO(b"synthetic"), "input.csv"),
        }
        if confirmation:
            data["confirm_write"] = confirmation
        with (
            patch.object(module, "UPLOAD_FOLDER", str(tmp_path)),
            patch.object(module.threading, "Thread", InlineThread),
            patch.object(module, "sync_objects", return_value={}) as sync,
        ):
            response = _client().post("/process", data=data)
        assert response.status_code == 302
        assert sync.call_args.kwargs["dry_run"] is expected_dry_run
