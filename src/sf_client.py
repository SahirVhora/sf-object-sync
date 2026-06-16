"""
OData v2 HTTP client for SAP SuccessFactors.

Features:
  - requests.Session with HTTPBasicAuth
  - Accept: application/json on GET; Content-Type: application/json on POST
  - 3 retries with exponential back-off (1 s / 2 s / 4 s) on 429 and 5xx
  - Automatic OData __next pagination
  - Configurable per-request timeout
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

# HTTP status codes that trigger a retry
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
BACKOFF_SECONDS = [1, 2, 4]  # indexed by attempt number (0-based)


def _odata_escape(value: str) -> str:
    """Escape a string literal for an OData v2 $filter.

    OData v2 single-quoted literals escape an embedded quote by doubling it
    ('' ). Without this, a value containing a single quote breaks the filter
    syntax (OData injection). For normal SuccessFactors codes (alphanumeric,
    no quotes) the output is identical to the input, so behaviour is unchanged.
    """
    return str(value).replace("'", "''")


class SFClientError(Exception):
    """Raised when the API returns an unrecoverable error."""

    def __init__(self, message: str, status_code: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class SFClient:
    """
    Thin OData v2 client bound to ONE SuccessFactors tenant.

    Instantiate once per environment (PRD, Dev).
    """

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        timeout_sec: int = 30,
        auth: Optional[Any] = None,
        cert: Optional[Any] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_sec
        self._session = requests.Session()
        # auth kwarg takes precedence; fall back to Basic when username provided
        if auth is not None:
            self._session.auth = auth
        elif username:
            self._session.auth = HTTPBasicAuth(username, password)
        if cert is not None:
            self._session.cert = cert
        self._session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _url(self, entity_set: str) -> str:
        return f"{self.base_url}/{entity_set}"

    def _request_with_retry(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        """Execute an HTTP request with retry logic on transient errors."""
        last_exc: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = self._session.request(
                    method, url, timeout=self.timeout, **kwargs
                )
                if resp.status_code not in RETRY_STATUS_CODES:
                    return resp
                wait = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
                logger.warning(
                    "HTTP %s from %s (attempt %d/%d) - retrying in %ds",
                    resp.status_code,
                    url,
                    attempt + 1,
                    MAX_RETRIES,
                    wait,
                )
                time.sleep(wait)
                last_exc = None
            except requests.exceptions.RequestException as exc:
                wait = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
                logger.warning(
                    "Request error on %s (attempt %d/%d): %s - retrying in %ds",
                    url,
                    attempt + 1,
                    MAX_RETRIES,
                    exc,
                    wait,
                )
                last_exc = exc
                time.sleep(wait)

        if last_exc:
            raise SFClientError(
                f"Request failed after {MAX_RETRIES} attempts: {last_exc}"
            )
        # Return the last response even if it was a retryable status
        return resp  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_entity_by_code(
        self,
        entity_set: str,
        external_code: str,
        extra_params: Optional[Dict[str, str]] = None,
        expand: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch all records for *entity_set* where externalCode = *external_code*.

        Args:
            entity_set    : OData entity set name (e.g. "FODepartment")
            external_code : value to match on externalCode
            extra_params  : additional OData query params to merge in
            expand        : comma-separated navigation properties to $expand
                            (e.g. "cust_Division" to inline the parent Division)

        Handles OData __next pagination automatically.
        Returns the list of entity dicts from d.results.
        """
        url = self._url(entity_set)
        params: Dict[str, str] = {
            "$filter": f"externalCode eq '{_odata_escape(external_code)}'",
            "$format": "json",
            "$top": "100",
        }
        if expand:
            params["$expand"] = expand
        if extra_params:
            params.update(extra_params)

        return self._paginate_get(url, params)

    def _paginate_get(
        self,
        url: str,
        params: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Issue GET requests following OData __next links until exhausted.
        Returns combined list of result entities.
        """
        results: List[Dict[str, Any]] = []
        next_url: Optional[str] = url
        first_call = True

        while next_url:
            resp = self._request_with_retry(
                "GET",
                next_url,
                params=params if first_call else None,
            )
            first_call = False

            if resp.status_code != 200:
                raise SFClientError(
                    f"GET {next_url} returned HTTP {resp.status_code}",
                    status_code=resp.status_code,
                    body=resp.text[:2000],
                )

            try:
                payload = resp.json()
            except json.JSONDecodeError as exc:
                raise SFClientError(
                    f"Non-JSON response from {next_url}: {exc}",
                    body=resp.text[:500],
                )

            data = payload.get("d", {})
            batch = data.get("results", [])
            results.extend(batch)

            # OData pagination link
            next_url = data.get("__next")
            logger.debug(
                "GET %s → %d records (total so far: %d)%s",
                next_url or url,
                len(batch),
                len(results),
                " [has next]" if next_url else "",
            )

        return results

    def entity_exists(
        self,
        entity_set: str,
        external_code: str,
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Check whether an active (open-ended) record exists in *entity_set*.

        Returns (exists: bool, record: dict | None).
        The caller must apply the end-date filter to select the active record.
        """
        records = self.get_entity_by_code(entity_set, external_code)
        if not records:
            return False, None
        return True, records  # caller picks active record from list

    def post_entity(
        self,
        entity_set: str,
        payload: Dict[str, Any],
    ) -> Tuple[int, Dict[str, Any]]:
        """
        POST *payload* to *entity_set*.

        Returns (http_status_code, response_body_dict).
        Raises SFClientError on network / transient failures only.
        4xx/5xx are returned as-is so the caller can log and decide.
        """
        url = self._url(entity_set)
        logger.debug("POST %s payload=%s", url, json.dumps(payload)[:500])

        resp = self._request_with_retry("POST", url, json=payload)

        try:
            body = resp.json()
        except json.JSONDecodeError:
            body = {"raw": resp.text[:2000]}

        return resp.status_code, body

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "SFClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
