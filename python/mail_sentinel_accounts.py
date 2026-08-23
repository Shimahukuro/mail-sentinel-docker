"""Load and apply Mail Sentinel account configuration."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


RESERVED = {"MAIL_SENTINEL_ACCOUNTS_FILE", "MAIL_SENTINEL_ACCOUNT_ID", "STATE_DIR"}


def account_id(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]


def load_accounts(path: Path) -> list[tuple[str, str, dict[str, str]]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    accounts = document.get("accounts") if isinstance(document, dict) else None
    defaults = document.get("defaults", {}) if isinstance(document, dict) else {}
    if not isinstance(defaults, dict) or not all(
        isinstance(key, str) and isinstance(item, (str, int, bool)) for key, item in defaults.items()
    ):
        raise RuntimeError("accounts configuration defaults must be an object of scalar values")
    if any(key in RESERVED for key in defaults):
        raise RuntimeError("accounts configuration defaults override a reserved environment variable")
    if not isinstance(accounts, list) or not accounts:
        raise RuntimeError("accounts configuration must contain a non-empty accounts array")
    result = []
    seen = set()
    for position, value in enumerate(accounts):
        if not isinstance(value, dict):
            raise RuntimeError(f"account at index {position} must be an object")
        name = value.get("name")
        environment = value.get("environment")
        if not isinstance(name, str) or not name or name in seen:
            raise RuntimeError("each account name must be a unique non-empty string")
        if not isinstance(environment, dict) or not environment:
            raise RuntimeError(f"account {name!r} must contain an environment object")
        if any(key in RESERVED for key in environment):
            raise RuntimeError(f"account {name!r} overrides a reserved environment variable")
        if not all(isinstance(key, str) and isinstance(item, (str, int, bool))
                   for key, item in environment.items()):
            raise RuntimeError(f"account {name!r} environment values must be strings, integers, or booleans")
        merged = {**defaults, **environment}
        auth_method = str(merged.get("IMAP_AUTH_METHOD", "password"))
        if auth_method not in ("password", "app_password", "xoauth2"):
            raise RuntimeError(f"account {name!r} has an unsupported IMAP_AUTH_METHOD")
        secret_key = "IMAP_OAUTH_ACCESS_TOKEN_FILE" if auth_method == "xoauth2" else "IMAP_PASSWORD_FILE"
        if secret_key not in merged:
            raise RuntimeError(f"account {name!r} must set {secret_key}")
        seen.add(name)
        normalized = {
            key: str(item).lower() if isinstance(item, bool) else str(item)
            for key, item in merged.items()
        }
        result.append((name, account_id(name), normalized))
    return result


def configured_accounts() -> list[tuple[str, str, dict[str, str]]]:
    path = os.environ.get("MAIL_SENTINEL_ACCOUNTS_FILE")
    if not path:
        raise RuntimeError("MAIL_SENTINEL_ACCOUNTS_FILE is required")
    return load_accounts(Path(path))


def account_environment(identifier: str, configured: dict[str, str]) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("MAIL_SENTINEL_ACCOUNTS_FILE", None)
    environment.update(configured)
    environment["MAIL_SENTINEL_ACCOUNT_ID"] = identifier
    root = Path(os.environ.get("STATE_DIR", "/var/lib/mail-sentinel-state"))
    environment["STATE_DIR"] = str(root / "accounts" / identifier)
    return environment
