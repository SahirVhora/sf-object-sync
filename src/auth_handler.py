"""Build authenticated SFClient instances from unified shared auth config."""

import logging
import os
from typing import Any

from sapsf_shared.auth import AuthConfig

logger = logging.getLogger(__name__)


class AuthError(Exception):
    """Raised when authentication cannot be established."""


# ── Main public function ──────────────────────────────────────────────────────


def build_sf_client(
    env: str,
    base_url: str,
    auth_method: str | None = None,
    username: str | None = None,
    password: str | None = None,
    timeout_sec: int = 30,
) -> "SFClient":  # noqa: F821  (imported below to avoid circular import)
    """
    Build and return an authenticated SFClient for *base_url*.

    Args:
        env         : "source" or "target" - used to look up env-specific vars
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
    if method == "oauth":
        method = "oauth2"
    env_prefix = f"SF_{env.upper()}"

    if method == "basic":
        user = (
            username or os.getenv(f"{env_prefix}_USER") or os.getenv(f"{env_prefix}_USERNAME", "")
        )
        pwd = password or os.getenv(f"{env_prefix}_PASSWORD", "")
        if not user or not pwd:
            raise AuthError(f"Basic auth requires {env_prefix}_USER and {env_prefix}_PASSWORD")
        logger.debug("Building SFClient[%s] with Basic Auth (user=%s)", env, user)
        return SFClient(
            base_url=base_url,
            username=user,
            password=pwd,
            timeout_sec=timeout_sec,
        )

    elif method == "oauth2":
        client_id = os.getenv(f"{env_prefix}_CLIENT_ID", "")
        client_secret = os.getenv(f"{env_prefix}_CLIENT_SECRET", "")
        token_url = os.getenv(f"{env_prefix}_TOKEN_URL", "")
        company_id = os.getenv(f"{env_prefix}_COMPANY_ID", os.getenv("SF_COMPANY_ID", ""))
        if not client_id or not client_secret or not token_url or not company_id:
            raise AuthError(
                f"OAuth auth requires {env_prefix}_CLIENT_ID, "
                f"{env_prefix}_CLIENT_SECRET, {env_prefix}_TOKEN_URL, "
                f"and {env_prefix}_COMPANY_ID or SF_COMPANY_ID"
            )
        logger.debug("Building SFClient[%s] with OAuth Bearer", env)
        client = SFClient(
            base_url=base_url,
            timeout_sec=timeout_sec,
        )
        client.config = AuthConfig(
            base_url=base_url,
            company_id=company_id,
            auth_type="oauth2",
            client_id=client_id,
            client_secret=client_secret,
            token_url=token_url,
            timeout_sec=timeout_sec,
        )
        from sapsf_shared.auth import build_requests_auth

        client._session.auth, client._session.cert = build_requests_auth(client.config)
        return client

    elif method == "certificate":
        cert_path = os.getenv(f"{env_prefix}_CERT_PATH", "")
        key_path = os.getenv(f"{env_prefix}_KEY_PATH", "")
        if not cert_path or not key_path:
            raise AuthError(
                f"Certificate auth requires {env_prefix}_CERT_PATH and {env_prefix}_KEY_PATH"
            )
        if not os.path.isfile(cert_path):
            raise AuthError(f"Certificate file not found: {cert_path}")
        if not os.path.isfile(key_path):
            raise AuthError(f"Key file not found: {key_path}")
        logger.debug("Building SFClient[%s] with Certificate Auth (cert=%s)", env, cert_path)
        client = SFClient(base_url=base_url, timeout_sec=timeout_sec)
        client.config = AuthConfig(
            base_url=base_url,
            auth_type="certificate",
            cert_path=cert_path,
            key_path=key_path,
            timeout_sec=timeout_sec,
        )
        client._session.cert = (cert_path, key_path)
        client._session.auth = None
        return client

    else:
        raise AuthError(
            f"Unknown AUTH_METHOD '{method}'. Must be one of: basic, oauth2, certificate"
        )


def build_clients_from_env(timeout_sec: int = 30) -> dict[str, Any]:
    """
    Build PRD (source) and Dev (target) SFClient instances from environment variables.

    Returns:
        {"prd_client": SFClient, "dev_client": SFClient,
         "prd_url": str, "dev_url": str}
    """

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
