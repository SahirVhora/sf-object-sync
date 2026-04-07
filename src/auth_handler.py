"""
Authentication handler — builds authenticated SFClient instances based on
the AUTH_METHOD environment variable (or explicit argument).

Supported methods:
  basic       — HTTP Basic Auth (username + password)
  oauth       — OAuth 2.0 client credentials (token endpoint)
  certificate — Mutual TLS with a client certificate + private key
"""

import logging
import os
from typing import Any, Dict, Optional

import requests
from requests.auth import AuthBase, HTTPBasicAuth

logger = logging.getLogger(__name__)

# Imported lazily to avoid hard dependency when basic auth is the only method used
_CERT_AUTH_AVAILABLE = True


# ── Custom OAuth bearer-token Auth class ─────────────────────────────────────

class _BearerAuth(AuthBase):
    """Attaches an OAuth 2.0 Bearer token to every request."""

    def __init__(self, token: str) -> None:
        self._token = token

    def __call__(self, r: requests.PreparedRequest) -> requests.PreparedRequest:
        r.headers["Authorization"] = f"Bearer {self._token}"
        return r


# ── Token fetcher ─────────────────────────────────────────────────────────────

def _fetch_oauth_token(
    token_url: str,
    client_id: str,
    client_secret: str,
    timeout: int = 30,
) -> str:
    """
    Fetch an OAuth 2.0 access token via client credentials grant.

    Raises AuthError on failure.
    """
    try:
        resp = requests.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        token = resp.json().get("access_token")
        if not token:
            raise AuthError("OAuth response missing access_token field")
        logger.debug("OAuth token obtained from %s", token_url)
        return token
    except requests.exceptions.RequestException as exc:
        raise AuthError(f"OAuth token request failed: {exc}") from exc


# ── Public exception ──────────────────────────────────────────────────────────

class AuthError(Exception):
    """Raised when authentication cannot be established."""


# ── Main public function ──────────────────────────────────────────────────────

def build_sf_client(
    env: str,
    base_url: str,
    auth_method: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout_sec: int = 30,
) -> "SFClient":  # noqa: F821  (imported below to avoid circular import)
    """
    Build and return an authenticated SFClient for *base_url*.

    Args:
        env         : "source" or "target" — used to look up env-specific vars
        base_url    : OData v2 base URL
        auth_method : override AUTH_METHOD env var ("basic", "oauth", "certificate")
        username    : override username env var (used for basic auth)
        password    : override password env var (used for basic auth)
        timeout_sec : per-request timeout in seconds

    The function reads from environment variables as fallback for any param
    not explicitly passed.
    """
    from .sf_client import SFClient

    method = (auth_method or os.getenv("AUTH_METHOD", "basic")).lower()
    env_prefix = f"SF_{env.upper()}"

    if method == "basic":
        user = username or os.getenv(f"{env_prefix}_USER") or os.getenv(f"{env_prefix}_USERNAME", "")
        pwd = password or os.getenv(f"{env_prefix}_PASSWORD", "")
        if not user or not pwd:
            raise AuthError(
                f"Basic auth requires {env_prefix}_USER and {env_prefix}_PASSWORD"
            )
        logger.debug("Building SFClient[%s] with Basic Auth (user=%s)", env, user)
        return SFClient(
            base_url=base_url,
            username=user,
            password=pwd,
            timeout_sec=timeout_sec,
            auth=HTTPBasicAuth(user, pwd),
        )

    elif method == "oauth":
        client_id = os.getenv(f"{env_prefix}_CLIENT_ID", "")
        client_secret = os.getenv(f"{env_prefix}_CLIENT_SECRET", "")
        token_url = os.getenv(f"{env_prefix}_TOKEN_URL", "")
        if not client_id or not client_secret or not token_url:
            raise AuthError(
                f"OAuth auth requires {env_prefix}_CLIENT_ID, "
                f"{env_prefix}_CLIENT_SECRET, {env_prefix}_TOKEN_URL"
            )
        token = _fetch_oauth_token(token_url, client_id, client_secret, timeout=timeout_sec)
        logger.debug("Building SFClient[%s] with OAuth Bearer", env)
        return SFClient(
            base_url=base_url,
            username="",
            password="",
            timeout_sec=timeout_sec,
            auth=_BearerAuth(token),
        )

    elif method == "certificate":
        cert_path = os.getenv(f"{env_prefix}_CERT_PATH", "")
        key_path = os.getenv(f"{env_prefix}_KEY_PATH", "")
        if not cert_path or not key_path:
            raise AuthError(
                f"Certificate auth requires {env_prefix}_CERT_PATH and "
                f"{env_prefix}_KEY_PATH"
            )
        if not os.path.isfile(cert_path):
            raise AuthError(f"Certificate file not found: {cert_path}")
        if not os.path.isfile(key_path):
            raise AuthError(f"Key file not found: {key_path}")
        logger.debug(
            "Building SFClient[%s] with Certificate Auth (cert=%s)", env, cert_path
        )
        return SFClient(
            base_url=base_url,
            username="",
            password="",
            timeout_sec=timeout_sec,
            cert=(cert_path, key_path),
        )

    else:
        raise AuthError(
            f"Unknown AUTH_METHOD '{method}'. Must be one of: basic, oauth, certificate"
        )


def build_clients_from_env(timeout_sec: int = 30) -> Dict[str, Any]:
    """
    Build PRD (source) and Dev (target) SFClient instances from environment variables.

    Returns:
        {"prd_client": SFClient, "dev_client": SFClient,
         "prd_url": str, "dev_url": str}
    """
    from .sf_client import SFClient

    source_url = os.getenv("SF_SOURCE_URL", "")
    target_url = os.getenv("SF_TARGET_URL", "")

    if not source_url:
        raise AuthError("SF_SOURCE_URL environment variable is not set")
    if not target_url:
        raise AuthError("SF_TARGET_URL environment variable is not set")

    prd_client = build_sf_client(
        env="source",
        base_url=source_url,
        timeout_sec=timeout_sec,
    )
    dev_client = build_sf_client(
        env="target",
        base_url=target_url,
        timeout_sec=timeout_sec,
    )

    return {
        "prd_client": prd_client,
        "dev_client": dev_client,
        "prd_url": source_url,
        "dev_url": target_url,
    }
