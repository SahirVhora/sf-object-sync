"""
Configuration loader - reads config.yaml, validates required fields,
and returns a typed dict for use across all modules.
"""

import os
import logging
from typing import Any, Dict

import yaml

logger = logging.getLogger(__name__)

REQUIRED_KEYS = {
    "prd": ["base_url", "username", "password"],
    "dev": ["base_url", "username", "password"],
}

DEFAULTS: Dict[str, Any] = {
    "options": {
        "dry_run": True,
        "log_level": "INFO",
        "output_dir": "./output",
        "request_timeout_sec": 30,
        "locales_to_sync": ["defaultValue", "en_US", "en_GB", "pl_PL"],
    }
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base (base is mutated in place)."""
    for key, val in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val
    return base


def load_config(config_path: str) -> Dict[str, Any]:
    """
    Load and validate configuration from *config_path*.

    Returns a dict with keys: prd, dev, options.
    Raises ValueError on validation failures.
    """
    if not os.path.isfile(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as fh:
        raw: Dict[str, Any] = yaml.safe_load(fh) or {}

    # Apply defaults for missing option keys
    merged = _deep_merge(dict(DEFAULTS), raw)

    # Validate required top-level sections
    for section, keys in REQUIRED_KEYS.items():
        if section not in merged:
            raise ValueError(f"Config missing required section: [{section}]")
        for k in keys:
            if k not in merged[section]:
                raise ValueError(
                    f"Config missing required field: [{section}].{k}"
                )

    # Warn if credentials appear to be placeholders
    for env in ("prd", "dev"):
        url = merged[env].get("base_url", "")
        if "<" in url or ">" in url:
            logger.warning(
                "Config [%s].base_url looks like a placeholder: %s", env, url
            )
        if not merged[env].get("username"):
            logger.warning("Config [%s].username is empty.", env)
        if not merged[env].get("password"):
            logger.warning("Config [%s].password is empty.", env)

    # Normalise output_dir
    merged["options"]["output_dir"] = os.path.abspath(
        merged["options"].get("output_dir", "./output")
    )
    os.makedirs(merged["options"]["output_dir"], exist_ok=True)

    # Ensure defaultValue is always included in locales
    locales: list = merged["options"].get("locales_to_sync", ["defaultValue"])
    if "defaultValue" not in locales:
        locales.insert(0, "defaultValue")
    merged["options"]["locales_to_sync"] = locales

    logger.debug("Configuration loaded from %s", config_path)
    return merged
