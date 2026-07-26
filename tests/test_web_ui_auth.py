"""Regression pin for ``sf_object_sync/web_ui/app.py`` auth gate.

Behaviour pinned on 2026-07-25 per ADR-0003 (see
``sapsf/_shared/docs/ADR-0003-deprecation-window.md``):

  * ``X-Auth-Token`` header is the primary channel - 200 on correct value.
  * ``?token=...`` query string is a backward-compatible fallback that emits
    exactly one ``app.logger.warning`` per request (deprecation signal).
  * Wrong or missing tokens always return 401 with body ``Unauthorized``.
  * When ``WEB_UI_TOKEN`` is unset, the gate stays open (local single-user
    tool); this test always sets it.

Removing the ``?token=`` fallback is scheduled for 2026-10-25. When that
lands, ``test_query_token_falls_back_and_logs_deprecation`` is expected
to flip to a permanent 401-regression pin.
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


def test_query_token_falls_back_and_logs_deprecation(web_app, caplog):
    """ADR-0003 window: ``?token=`` still works but emits one warning.

    The log message must contain ``path=`` and ``remote=`` so operators can
    grep the deprecation signal during probes. We also pin the named logger
    (``web_ui.app``) - Flask's ``app.logger`` propagates by default, but
    binding explicitly documents the contract under test.
    """
    client = web_app.test_client()
    with caplog.at_level(logging.WARNING, logger="web_ui.app"):
        resp = client.get("/?token=" + AUTH_SECRET)
    assert resp.status_code == 200
    deprecations = [r for r in caplog.records if "deprecated" in r.message]
    assert len(deprecations) == 1, (
        f"expected exactly 1 deprecation warning, got: "
        f"{[r.message for r in caplog.records]}"
    )
    msg = deprecations[0].message
    # Pin the operator-grep surface.
    assert "X-Auth-Token" in msg
    assert "path=" in msg
    assert "remote=" in msg


def test_missing_token_returns_401_without_warning(web_app, caplog):
    """No token at all is a hard 401 with no deprecation log emitted."""
    client = web_app.test_client()
    with caplog.at_level(logging.WARNING, logger="web_ui.app"):
        resp = client.get("/")
    assert resp.status_code == 401
    assert not [r for r in caplog.records if "deprecated" in r.message]
