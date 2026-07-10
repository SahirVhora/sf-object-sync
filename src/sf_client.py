"""Compatibility adapter for the shared SAP SuccessFactors SDK client.

The rest of sf-object-sync imports ``src.sf_client.SFClient`` directly, so this
module preserves that local interface while delegating OData HTTP behaviour to
``sapsf_shared.SFClient``.
"""

from __future__ import annotations

import json
from typing import Any

from sapsf_shared import AuthConfig
from sapsf_shared import SFClient as SharedSFClient
from sapsf_shared.exceptions import AmbiguousWriteError, SFClientError
from sapsf_shared.utils import odata_escape

__all__ = ["AmbiguousWriteError", "SFClient", "SFClientError", "_odata_escape"]


def _odata_escape(value: str) -> str:
    """Backward-compatible alias for the shared OData literal escaper."""
    return odata_escape(value)


class SFClient(SharedSFClient):
    """sf-object-sync facade over ``sapsf_shared.SFClient``.

    The constructor and ``post_entity`` method match the tool's original local
    client so callers and tests do not need to change during SDK adoption.
    """

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        timeout_sec: int = 30,
        auth: Any | None = None,
        cert: Any | None = None,
    ) -> None:
        config = AuthConfig(
            base_url=base_url,
            username=username or "__auth_override__",
            password=password or "__auth_override__",
            timeout_sec=timeout_sec,
        )
        super().__init__(config, default_top=100)
        self.timeout = timeout_sec

        if auth is not None:
            self._session.auth = auth
        if cert is not None:
            self._session.cert = cert

    def get_entity_by_code(
        self,
        entity_set: str,
        external_code: str,
        extra_params: dict[str, str] | None = None,
        expand: str | None = None,
    ) -> list[dict[str, Any]]:
        return super().get_entity_by_code(
            entity_set,
            external_code,
            expand=expand,
            extra_params=extra_params,
        )

    def _paginate_get(
        self,
        url: str,
        params: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        return super()._paginate(url, params)

    def entity_exists(
        self,
        entity_set: str,
        external_code: str,
    ) -> tuple[bool, dict[str, Any] | None]:
        records = self.get_entity_by_code(entity_set, external_code)
        if not records:
            return False, None
        return True, records  # type: ignore[return-value]

    def post_entity(
        self,
        entity_set: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        """POST an entity while preserving legacy non-raising 4xx/5xx handling."""
        url = self._url(entity_set)
        resp = self._request_with_retry("POST", url, json=payload)
        try:
            body = resp.json()
        except json.JSONDecodeError:
            body = {"raw": resp.text[:2000]}
        return resp.status_code, body
