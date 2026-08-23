"""Regression pin for ``sf_object_sync/web_ui/app.py`` auth gate.

Behaviour pinned on 2026-07-25 per ADR-0003 (see
``sapsf/_shared/docs/ADR-0003-deprecation-window.md``):

  * ``X-Auth-Token`` header is the primary channel - 200 on correct value.
  * ``?token=...`` query strings are permanently rejected so credentials do
    not leak through browser history, referrers, access logs, or copied URLs.
  * Wrong or missing tokens always return 401 with body ``Unauthorized``.
  * When ``WEB_UI_TOKEN`` is unset, the gate stays open (local single-user
    tool); this test always sets it.

The former deprecation window is closed. Reintroducing a query-string token is
a security regression.
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

import pytest

# Single secret used everywhere - hoist avoids typo drift across test bodies.
AUTH_SECRET = "auth-secret-xyz-001"


@pytest.fixture()
def web_app(monkeypatch):
    """Reload ``web_ui.app`` with ``WEB_UI_TOKEN`` set and a stable secret."""
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret-stable-pytest")
    monkeypatch.setenv("WEB_UI_TOKEN", AUTH_SECRET)

    # Make the repo root importable so ``from src.sync_engine import ...`` works.
    # ``monkeypatch.syspath_prepend`` auto-cleans on teardown -- prevents the
    # legacy ``sys.path.insert`` pattern from leaking into sibling tests.
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(repo_root))

    if "web_ui.app" in sys.modules:
        del sys.modules["web_ui.app"]
    pkg = importlib.import_module("web_ui.app")
    importlib.reload(pkg)
    yield pkg.app


def test_auth_header_passes(web_app):
    client = web_app.test_client()
    resp = client.get("/", headers={"X-Auth-Token": AUTH_SECRET})
    assert resp.status_code == 200


def test_auth_wrong_token_returns_401(web_app):
    client = web_app.test_client()
    assert client.get("/", headers={"X-Auth-Token": "wrong"}).status_code == 401
    assert client.get("/").status_code == 401


def test_query_token_is_rejected(web_app, caplog):
    """ADR-0003: credentials in URLs never authenticate a request."""
    client = web_app.test_client()
    with caplog.at_level(logging.WARNING, logger="web_ui.app"):
        resp = client.get("/?token=" + AUTH_SECRET)
    assert resp.status_code == 401
    assert resp.data == b"Unauthorized"
    assert not caplog.records


def test_missing_token_returns_401_without_warning(web_app, caplog):
    """No token at all is a hard 401 with no deprecation log emitted."""
    client = web_app.test_client()
    with caplog.at_level(logging.WARNING, logger="web_ui.app"):
        resp = client.get("/")
    assert resp.status_code == 401
    assert not [r for r in caplog.records if "deprecated" in r.message]
