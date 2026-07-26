import os
import sys
import threading
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import sapsf_shared.auth as shared_auth

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.auth_handler import build_sf_client  # noqa: E402
from src.sync_engine import _make_clients  # noqa: E402
from web_ui.app import app  # noqa: E402


def disable_oauth_network(monkeypatch):
    monkeypatch.setattr(
        shared_auth, "build_requests_auth", lambda _config: ("test-auth", None)
    )


def test_positional_timeout_remains_backward_compatible():
    client = build_sf_client(
        "source",
        "https://tenant.example/odata/v2",
        "basic",
        "user",
        "password",
        15,
    )

    assert client.timeout == 15
    client.close()


def test_oauth_explicit_config_overrides_environment(monkeypatch):
    disable_oauth_network(monkeypatch)
    monkeypatch.setenv("SF_SOURCE_CLIENT_ID", "environment-client")
    monkeypatch.setenv("SF_SOURCE_CLIENT_SECRET", "environment-secret")
    monkeypatch.setenv("SF_SOURCE_TOKEN_URL", "https://environment.example/token")
    monkeypatch.setenv("SF_SOURCE_COMPANY_ID", "environment-company")

    client = build_sf_client(
        "source",
        "https://tenant.example/odata/v2",
        auth_method="oauth",
        auth_config={
            "client_id": "request-client",
            "client_secret": "request-secret",
            "token_url": "https://request.example/token",
            "company_id": "request-company",
        },
    )

    assert client.config.client_id == "request-client"
    assert client.config.client_secret == "request-secret"
    assert client.config.token_url == "https://request.example/token"
    assert client.config.company_id == "request-company"
    client.close()


def test_oauth_explicit_config_falls_back_to_environment(monkeypatch):
    disable_oauth_network(monkeypatch)
    monkeypatch.setenv("SF_SOURCE_CLIENT_ID", "environment-client")
    monkeypatch.setenv("SF_SOURCE_CLIENT_SECRET", "environment-secret")
    monkeypatch.setenv("SF_SOURCE_TOKEN_URL", "https://environment.example/token")
    monkeypatch.setenv("SF_SOURCE_COMPANY_ID", "environment-company")

    client = build_sf_client(
        "source",
        "https://tenant.example/odata/v2",
        auth_method="oauth",
        auth_config={},
    )

    assert client.config.client_id == "environment-client"
    assert client.config.client_secret == "environment-secret"
    assert client.config.token_url == "https://environment.example/token"
    assert client.config.company_id == "environment-company"
    client.close()


def test_certificate_explicit_config_overrides_environment(monkeypatch, tmp_path: Path):
    request_cert = tmp_path / "request.pem"
    request_key = tmp_path / "request.key"
    request_cert.write_text("certificate", encoding="utf-8")
    request_key.write_text("key", encoding="utf-8")
    monkeypatch.setenv("SF_SOURCE_CERT_PATH", "/environment/cert.pem")
    monkeypatch.setenv("SF_SOURCE_KEY_PATH", "/environment/key.pem")

    client = build_sf_client(
        "source",
        "https://tenant.example/odata/v2",
        auth_method="certificate",
        auth_config={"cert_path": str(request_cert), "key_path": str(request_key)},
    )

    assert client.config.cert_path == str(request_cert)
    assert client.config.key_path == str(request_key)
    assert client._session.cert == (str(request_cert), str(request_key))
    client.close()


def test_connection_api_passes_oauth_credentials_without_mutating_environment(monkeypatch):
    client = app.test_client()
    monkeypatch.setenv("SF_SOURCE_CLIENT_SECRET", "process-level-secret")
    captured = {}

    def fake_build_client(env_name, base_url, *, auth_method, auth_config, timeout_sec):
        captured.update(
            env_name=env_name,
            base_url=base_url,
            auth_method=auth_method,
            auth_config=auth_config,
            timeout_sec=timeout_sec,
        )
        assert os.environ["SF_SOURCE_CLIENT_SECRET"] == "process-level-secret"
        return MagicMock()

    payload = {
        "auth_method": "oauth",
        "source_url": "https://prd.example.com",
        "source_client_id": "request-client-id",
        "source_client_secret": "request-scoped-secret",
        "source_token_url": "https://login.example.com/token",
        "source_company_id": "request-company",
    }
    with patch("web_ui.app.build_sf_client", fake_build_client), patch(
        "web_ui.app._probe_connection", return_value=(True, "Connection successful")
    ):
        response = client.post("/api/test_connection", json=payload)

    assert response.status_code == 200
    assert captured == {
        "env_name": "source",
        "base_url": "https://prd.example.com/odata/v2",
        "auth_method": "oauth",
        "auth_config": {
            "client_id": "request-client-id",
            "client_secret": "request-scoped-secret",
            "token_url": "https://login.example.com/token",
            "company_id": "request-company",
            "cert_path": "",
            "key_path": "",
        },
        "timeout_sec": 15,
    }
    assert os.environ["SF_SOURCE_CLIENT_SECRET"] == "process-level-secret"


def test_process_passes_request_auth_without_mutating_environment(monkeypatch):
    client = app.test_client()
    sentinel = {
        "AUTH_METHOD": "process-auth",
        "SF_SOURCE_CLIENT_SECRET": "process-source-secret",
        "SF_TARGET_CLIENT_SECRET": "process-target-secret",
    }
    for key, value in sentinel.items():
        monkeypatch.setenv(key, value)
    captured = {}
    completed = threading.Event()

    def fake_sync_objects(**kwargs):
        captured.update(kwargs)
        completed.set()
        return {"report_path": "", "logs": []}

    with patch("web_ui.app.sync_objects", fake_sync_objects):
        response = client.post(
            "/process",
            data={
                "auth_method": "oauth",
                "source_url": "https://prd.example.com",
                "target_url": "https://dev.example.com",
                "source_client_id": "source-client",
                "source_client_secret": "source-request-secret",
                "source_token_url": "https://login.example.com/source",
                "source_company_id": "source-company",
                "target_client_id": "target-client",
                "target_client_secret": "target-request-secret",
                "target_token_url": "https://login.example.com/target",
                "target_company_id": "target-company",
                "input_file": (BytesIO(b"test workbook"), "input.xlsx"),
            },
            content_type="multipart/form-data",
        )

    assert response.status_code == 302
    assert completed.wait(timeout=2)
    assert captured["source_config"] == {
        "base_url": "https://prd.example.com/odata/v2",
        "username": "",
        "password": "",
        "auth_method": "oauth",
        "auth_config": {
            "client_id": "source-client",
            "client_secret": "source-request-secret",
            "token_url": "https://login.example.com/source",
            "company_id": "source-company",
            "cert_path": "",
            "key_path": "",
        },
    }
    assert captured["target_config"]["auth_method"] == "oauth"
    assert captured["target_config"]["auth_config"]["company_id"] == "target-company"
    assert captured["target_config"]["auth_config"]["client_secret"] == "target-request-secret"
    assert {key: os.environ[key] for key in sentinel} == sentinel


def test_make_clients_passes_request_scoped_auth_config(monkeypatch):
    monkeypatch.setenv("AUTH_METHOD", "environment-method")
    calls = []

    def fake_build_client(env, base_url, *, auth_method, auth_config, timeout_sec):
        calls.append((env, base_url, auth_method, auth_config, timeout_sec))
        return MagicMock()

    cfg = {
        "prd": {
            "base_url": "https://prd.example.com/odata/v2",
            "auth_method": "oauth",
            "auth_config": {"client_secret": "source-secret", "company_id": "source-company"},
        },
        "dev": {
            "base_url": "https://dev.example.com/odata/v2",
            "auth_method": "certificate",
            "auth_config": {"cert_path": "/request/cert", "key_path": "/request/key"},
        },
        "options": {"request_timeout_sec": 15},
    }

    with patch("src.auth_handler.build_sf_client", fake_build_client):
        _make_clients(cfg)

    assert calls == [
        (
            "source",
            "https://prd.example.com/odata/v2",
            "oauth",
            {"client_secret": "source-secret", "company_id": "source-company"},
            15,
        ),
        (
            "target",
            "https://dev.example.com/odata/v2",
            "certificate",
            {"cert_path": "/request/cert", "key_path": "/request/key"},
            15,
        ),
    ]
