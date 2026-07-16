"""
admin/config.py
----------------
Configuration for the ME-HAAT Fashion AI Bot Admin Dashboard (additive module).

All settings are read once from environment variables and exposed as a typed,
immutable object, mirroring the pattern already used by the project's root
``config.py``. Every setting has a safe default so the dashboard module never
breaks application startup, and none of these variables affect the existing
WhatsApp / Shopify / Gemini configuration.

Required for login to be usable in production:
    ADMIN_USERNAME       Admin login username.
    ADMIN_PASSWORD       Admin login password (plaintext) OR set ADMIN_PASSWORD_HASH.

Recommended:
    ADMIN_SECRET_KEY     Stable Flask session-signing secret (see note below).

Optional:
    ADMIN_PASSWORD_HASH  Werkzeug pbkdf2/scrypt hash; takes precedence over
                         ADMIN_PASSWORD when set.
    ADMIN_DB_PATH        Path to the dashboard SQLite database. Defaults to a
                         file next to TOKEN_STORE_PATH (Render's mounted disk).
    ADMIN_SESSION_TIMEOUT_MIN   Idle-session timeout in minutes (default 60).
    ADMIN_LOGIN_MAX_ATTEMPTS    Failed logins per window before throttling (5).
    ADMIN_LOGIN_WINDOW_SEC      Login throttle window in seconds (300).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Optional

from utils.logging import logger


def _int_env(name: str, default: int) -> int:
    """Read an integer environment variable with a safe fallback."""
    raw = os.environ.get(name, "")
    try:
        return int(raw) if raw.strip() else default
    except (TypeError, ValueError):
        logger.warning("ADMIN | Invalid integer for %s=%r; using %d", name, raw, default)
        return default


def _default_db_path() -> str:
    """Choose the dashboard's SQLite path.

    v10.1: unless ``ADMIN_DB_PATH`` is *explicitly* set, the dashboard now shares
    the single canonical ``mehaat.db`` with the token store and commerce data
    (one unified database). Setting ``ADMIN_DB_PATH`` still forces a separate
    file for backward compatibility.
    """
    explicit = os.environ.get("ADMIN_DB_PATH", "").strip()
    if explicit:
        return explicit
    try:
        from utils.dbpath import canonical_sqlite_path

        return canonical_sqlite_path()
    except Exception:  # noqa: BLE001 - defensive fallback
        return "mehaat.db"


def _derive_secret_key() -> str:
    """Return a session-signing secret.

    Uses ``ADMIN_SECRET_KEY`` when provided. Otherwise derives a *stable* key
    from the admin credentials so that all Gunicorn workers share the same
    signing secret (a random per-process key would invalidate sessions across
    workers). Setting ``ADMIN_SECRET_KEY`` explicitly is still recommended.
    """
    explicit = os.environ.get("ADMIN_SECRET_KEY", "").strip()
    if explicit:
        return explicit
    seed = (
        os.environ.get("ADMIN_USERNAME", "")
        + "::"
        + os.environ.get("ADMIN_PASSWORD", "")
        + "::"
        + os.environ.get("ADMIN_PASSWORD_HASH", "")
        + "::me-haat-admin-secret-v1"
    )
    logger.warning(
        "ADMIN | ADMIN_SECRET_KEY not set; deriving a stable key from credentials. "
        "Set ADMIN_SECRET_KEY for production."
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AdminConfig:
    """Immutable admin-dashboard configuration loaded from the environment."""

    username: str = field(default_factory=lambda: os.environ.get("ADMIN_USERNAME", "admin"))
    password: str = field(default_factory=lambda: os.environ.get("ADMIN_PASSWORD", ""))
    password_hash: str = field(
        default_factory=lambda: os.environ.get("ADMIN_PASSWORD_HASH", "")
    )
    secret_key: str = field(default_factory=_derive_secret_key)
    db_path: str = field(default_factory=_default_db_path)
    session_timeout_min: int = field(
        default_factory=lambda: _int_env("ADMIN_SESSION_TIMEOUT_MIN", 60)
    )
    login_max_attempts: int = field(
        default_factory=lambda: _int_env("ADMIN_LOGIN_MAX_ATTEMPTS", 5)
    )
    login_window_sec: int = field(
        default_factory=lambda: _int_env("ADMIN_LOGIN_WINDOW_SEC", 300)
    )

    @property
    def credentials_configured(self) -> bool:
        """True when a username and some form of password are configured."""
        return bool(self.username and (self.password or self.password_hash))

    def masked_summary(self) -> str:
        """Human-readable, secret-free summary for startup logs."""
        pw = "hash" if self.password_hash else ("set" if self.password else "MISSING")
        return (
            f"user={self.username or 'MISSING'} password={pw} "
            f"db={self.db_path} session_timeout={self.session_timeout_min}m"
        )


admin_config = AdminConfig()
