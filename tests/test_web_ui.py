import os
import sys
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Import after sys.path bootstrap so 'web_ui.app' is resolvable.
from web_ui.app import app  # noqa: E402


def test_connection_api_reports_source_and_target_success():
    client = app.test_client()
    payload = {
        "auth_method": "basic",
        "source_url": "https://prd.example.com/odata/v2",
        "source_user": "source_user",
        "source_password": "source_pass",
        "target_url": "https://dev.example.com/odata/v2",
        "target_user": "target_user",
        "target_password": "target_pass",
    }

    with patch("web_ui.app._probe_connection", return_value=(True, "Connection successful")):
        response = client.post("/api/test_connection", json=payload)

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["source"]["ok"] is True
    assert body["target"]["ok"] is True


def test_connection_api_normalises_tenant_host_urls():
    client = app.test_client()
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
    client = app.test_client()

    response = client.post("/api/test_connection", json={"auth_method": "basic"})

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is False
    assert body["source"]["ok"] is False
    assert body["target"]["ok"] is False
