"""Shopify OAuth authentication module for ME-HAAT Fashion AI Bot v5.

This module provides a self-contained Flask blueprint that implements the
complete Shopify OAuth 2.0 authorization-code grant, including install
initiation, callback handling, HMAC verification, CSRF state protection with
replay defence, secure token exchange and multi-store token persistence.

The rest of the project depends on exactly two public objects exported here::

    from shopify.auth import shopify_auth_bp, token_store

``shopify_auth_bp`` is a :class:`flask.Blueprint` that the application
registers via ``app.register_blueprint(shopify_auth_bp)``.  ``token_store`` is
a process-wide, thread-safe :class:`TokenStore` singleton whose API
(``list_shops``, ``get_default_shop`` and friends) is already consumed
elsewhere in the codebase and therefore must remain stable.

No other file in the project needs to change for this module to work.

v5 production patch — persistence
---------------------------------
Earlier revisions kept the OAuth ``state`` and the per-shop access tokens in
process memory.  Under Gunicorn with more than one worker this breaks the OAuth
flow: the worker that issues the ``state`` on ``/shopify/install`` is usually
not the worker that receives ``/shopify/callback``, so the callback worker has
no record of the state and rejects the request with "Invalid or expired OAuth
state".  In-memory tokens also vanish on every restart/deploy.

This patch replaces **only the storage implementation** with a durable SQLite
backend (Python standard-library ``sqlite3`` — no new dependency).  Both the
state store and the token store now persist to a single SQLite database that is
shared by every worker process and survives restarts, deploys and Render
container recycles.  WAL journalling plus a busy timeout make concurrent access
from 1, 2 or 8 workers safe, and ``state`` consumption is a single atomic
``DELETE ... RETURNING`` statement so replay is impossible even across workers.

The public classes, functions, constants, singletons and routes are unchanged;
their behaviour is identical from the caller's perspective.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, Response, jsonify, redirect, request

# --------------------------------------------------------------------------- #
# Logger
# --------------------------------------------------------------------------- #
# Prefer the shared project logger.  Fall back to a module-local standard
# logger so that importing this file can never fail merely because the logging
# helper is unavailable in some execution context (tests, tooling, etc.).
try:  # pragma: no cover - exercised implicitly by the running application
    from utils.logging import logger
except Exception:  # pragma: no cover - defensive fallback only
    import logging

    logger = logging.getLogger("shopify.auth")
    if not logger.handlers:
        _handler = logging.StreamHandler()
        _handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        logger.addHandler(_handler)
        logger.setLevel(logging.INFO)


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
#: Blueprint URL prefix.  Routes resolve to ``/shopify/install`` and
#: ``/shopify/callback`` to match the URLs already registered with Shopify.
BLUEPRINT_URL_PREFIX: str = "/shopify"

#: Shopify authorization endpoint template (per-store).
SHOPIFY_AUTHORIZE_URL_TEMPLATE: str = "https://{shop}/admin/oauth/authorize"

#: Shopify access-token exchange endpoint template (per-store).
SHOPIFY_ACCESS_TOKEN_URL_TEMPLATE: str = "https://{shop}/admin/oauth/access_token"

#: Default OAuth scopes used when ``SHOPIFY_SCOPES`` is not configured.
DEFAULT_SHOPIFY_SCOPES: str = "read_products,read_orders"

#: Lifetime, in seconds, of a generated OAuth ``state`` value.  States older
#: than this are treated as expired and rejected, mitigating replay attacks.
STATE_TTL_SECONDS: int = 600

#: Number of random bytes used when generating an OAuth ``state`` token.
STATE_TOKEN_BYTES: int = 32

#: Timeout, in seconds, applied to the outbound token-exchange HTTP request.
TOKEN_EXCHANGE_TIMEOUT_SECONDS: int = 15

#: Query parameters that are excluded from the HMAC digest computation, as
#: mandated by the Shopify OAuth specification.
HMAC_EXCLUDED_PARAMS: Tuple[str, ...] = ("hmac", "signature")

#: Fallback SQLite database location used when ``DATABASE_URL`` is absent or is
#: not a SQLite URL.  On Render this path lives on the mounted persistent disk.
DEFAULT_SQLITE_PATH: str = "/var/data/mehaat.db"

#: SQLite busy timeout (milliseconds) applied to every connection so concurrent
#: workers wait for a lock instead of failing immediately.
SQLITE_BUSY_TIMEOUT_MS: int = 30000


# --------------------------------------------------------------------------- #
# Environment helpers
# --------------------------------------------------------------------------- #
def _get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    """Return a stripped environment variable value.

    Args:
        name: Name of the environment variable to read.
        default: Value returned when the variable is unset or empty.

    Returns:
        The trimmed variable value, or ``default`` when it is missing or blank.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    trimmed = raw.strip()
    return trimmed if trimmed else default


def _get_api_key() -> Optional[str]:
    """Return the configured Shopify API key (client id)."""
    return _get_env("SHOPIFY_API_KEY")


def _get_api_secret() -> Optional[str]:
    """Return the configured Shopify API secret (client secret)."""
    return _get_env("SHOPIFY_API_SECRET")


def _get_app_url() -> Optional[str]:
    """Return the public application URL without a trailing slash."""
    app_url = _get_env("SHOPIFY_APP_URL")
    if app_url is None:
        return None
    return app_url.rstrip("/")


def _get_scopes() -> str:
    """Return the configured OAuth scopes, falling back to sane defaults."""
    return _get_env("SHOPIFY_SCOPES", DEFAULT_SHOPIFY_SCOPES) or DEFAULT_SHOPIFY_SCOPES


def _get_configured_default_shop() -> Optional[str]:
    """Return the operator-configured default shop, if any."""
    return _get_env("SHOPIFY_DEFAULT_SHOP")


def _get_redirect_uri() -> Optional[str]:
    """Return the fully-qualified OAuth callback URL.

    Returns:
        The callback URL derived from ``SHOPIFY_APP_URL``, or ``None`` when the
        application URL is not configured.
    """
    app_url = _get_app_url()
    if not app_url:
        return None
    return f"{app_url}{BLUEPRINT_URL_PREFIX}/callback"


# --------------------------------------------------------------------------- #
# SQLite persistence layer (shared by the state store and the token store)
# --------------------------------------------------------------------------- #
# A single connection-per-operation model is used because ``sqlite3`` objects
# are not safe to share between threads.  Opening a connection is cheap for
# SQLite, and WAL journalling lets many readers run concurrently with a single
# writer.  A process-local lock serialises writers within a worker; SQLite's
# own file locking (plus the busy timeout) serialises writers across workers.
_DB_PATH_LOCK: threading.Lock = threading.Lock()
_WRITE_LOCK: threading.Lock = threading.Lock()
_RESOLVED_DB_PATH: Optional[str] = None
_SCHEMA_READY: bool = False


def _sqlite_path_from_database_url(database_url: Optional[str]) -> Optional[str]:
    """Extract a filesystem path from a ``sqlite://`` URL.

    Understands the SQLAlchemy-style conventions used elsewhere in the project:

        * ``sqlite:///relative.db``       -> ``relative.db`` (relative to cwd)
        * ``sqlite:////var/data/x.db``    -> ``/var/data/x.db`` (absolute)
        * ``sqlite://``                   -> ``None`` (no usable path)

    Non-SQLite URLs (e.g. PostgreSQL) return ``None`` so the caller falls back
    to the default SQLite path.

    Args:
        database_url: The raw ``DATABASE_URL`` value, or ``None``.

    Returns:
        A filesystem path, or ``None`` when the URL is not a usable SQLite URL.
    """
    if not database_url:
        return None
    url = database_url.strip()
    lowered = url.lower()
    if not lowered.startswith("sqlite:"):
        return None

    # Strip the scheme and the (always empty) authority component.
    remainder = url[len("sqlite://"):] if lowered.startswith("sqlite://") else url[len("sqlite:"):]
    if not remainder:
        return None
    # SQLAlchemy treats the first slash after the empty authority as a
    # separator: three slashes => relative path, four slashes => absolute.
    path = remainder[1:] if remainder.startswith("/") else remainder
    if not path or path == ":memory:":
        return None
    return path


def _candidate_db_paths() -> List[str]:
    """Return candidate database paths in priority order.

    Priority:
        1. A SQLite path parsed from ``DATABASE_URL`` (reused when available).
        2. The default ``/var/data/mehaat.db`` (Render persistent disk).
        3. The directory of ``TOKEN_STORE_PATH`` (already mounted on Render).
        4. A local ``mehaat.db`` in the working directory (dev/tests fallback).
    """
    candidates: List[str] = []
    from_url = _sqlite_path_from_database_url(os.environ.get("DATABASE_URL"))
    if from_url:
        candidates.append(from_url)
    candidates.append(DEFAULT_SQLITE_PATH)
    token_store_path = _get_env("TOKEN_STORE_PATH")
    if token_store_path:
        directory = os.path.dirname(token_store_path) or "."
        candidates.append(os.path.join(directory, "mehaat.db"))
    candidates.append(os.path.join(os.getcwd(), "mehaat.db"))
    # De-duplicate while preserving order.
    seen: set = set()
    unique: List[str] = []
    for path in candidates:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _resolve_db_path() -> str:
    """Resolve (once) and return a writable SQLite database path.

    The first candidate whose parent directory can be created/written is chosen.
    The result is cached for the lifetime of the process.
    """
    global _RESOLVED_DB_PATH
    if _RESOLVED_DB_PATH is not None:
        return _RESOLVED_DB_PATH

    with _DB_PATH_LOCK:
        if _RESOLVED_DB_PATH is not None:
            return _RESOLVED_DB_PATH

        last_error: Optional[Exception] = None
        for candidate in _candidate_db_paths():
            try:
                directory = os.path.dirname(candidate)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                # Probe writability by opening (and closing) a connection.
                probe = sqlite3.connect(candidate, timeout=5.0)
                probe.execute("PRAGMA journal_mode=WAL;")
                probe.close()
                _RESOLVED_DB_PATH = candidate
                logger.info("OAUTH_DB | Using SQLite persistence at %s", candidate)
                return _RESOLVED_DB_PATH
            except Exception as exc:  # noqa: BLE001 - try the next candidate
                last_error = exc
                logger.debug("OAUTH_DB | Path %s unusable: %s", candidate, exc)

        # As an absolute last resort, use an in-memory-per-process path so the
        # application still starts (single-worker/dev only).  This should never
        # be reached on Render because /var/data is mounted and writable.
        fallback = os.path.join(os.getcwd(), "mehaat_oauth_fallback.db")
        logger.error(
            "OAUTH_DB | No preferred SQLite path was writable (%s); "
            "falling back to %s",
            last_error,
            fallback,
        )
        _RESOLVED_DB_PATH = fallback
        return _RESOLVED_DB_PATH


def _connect() -> sqlite3.Connection:
    """Open a configured SQLite connection (row access by column name)."""
    conn = sqlite3.connect(_resolve_db_path(), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS};")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


_SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS oauth_states (
    state       TEXT PRIMARY KEY,
    shop        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oauth_states_expires ON oauth_states(expires_at);

CREATE TABLE IF NOT EXISTS shop_tokens (
    shop         TEXT PRIMARY KEY,
    access_token TEXT NOT NULL,
    installed_at REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shop_tokens_installed ON shop_tokens(installed_at);
"""


def _ensure_schema() -> None:
    """Create the OAuth tables if they do not yet exist (idempotent, once)."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _WRITE_LOCK:
        if _SCHEMA_READY:
            return
        try:
            conn = _connect()
            try:
                conn.executescript(_SCHEMA_SQL)
                conn.commit()
            finally:
                conn.close()
            _SCHEMA_READY = True
        except Exception as exc:  # noqa: BLE001
            # Mark ready to avoid hammering a broken path; operations will still
            # surface their own errors and are individually guarded.
            _SCHEMA_READY = True
            logger.error("OAUTH_DB | Schema initialisation failed: %s", exc)


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def is_valid_shop_domain(shop: Optional[str]) -> bool:
    """Validate that ``shop`` is a well-formed ``*.myshopify.com`` domain.

    The check is deliberately strict: only lowercase-insensitive alphanumeric
    labels and hyphens are accepted for the store handle, the domain must end
    in ``.myshopify.com`` and must not contain path, port or scheme fragments.

    Args:
        shop: Candidate shop domain supplied by an untrusted caller.

    Returns:
        ``True`` when the domain is safe to use, ``False`` otherwise.
    """
    if not shop or not isinstance(shop, str):
        return False

    candidate = shop.strip().lower()
    if len(candidate) > 253:
        return False

    # Reject anything carrying scheme, path, query, port or whitespace.
    for illegal in ("/", "\\", "?", "#", "@", ":", " ", "\t", "\n", "\r"):
        if illegal in candidate:
            return False

    suffix = ".myshopify.com"
    if not candidate.endswith(suffix):
        return False

    handle = candidate[: -len(suffix)]
    if not handle or len(handle) > 60:
        return False

    if handle.startswith("-") or handle.endswith("-"):
        return False

    for char in handle:
        if not (char.isascii() and (char.isalnum() or char == "-")):
            return False

    return True


def _normalise_shop(shop: Optional[str]) -> Optional[str]:
    """Return a canonical, validated shop domain or ``None``.

    Args:
        shop: Raw shop value from a request parameter.

    Returns:
        The lowercase, trimmed domain when valid, otherwise ``None``.
    """
    if not shop or not isinstance(shop, str):
        return None
    candidate = shop.strip().lower()
    return candidate if is_valid_shop_domain(candidate) else None


# --------------------------------------------------------------------------- #
# HMAC validation
# --------------------------------------------------------------------------- #
def verify_hmac(params: Dict[str, str], secret: Optional[str]) -> bool:
    """Verify a Shopify request HMAC signature.

    Implements the algorithm described in the Shopify OAuth documentation:
    every query parameter except ``hmac`` (and the legacy ``signature``) is
    collected, the pairs are sorted lexicographically by key, joined into a
    canonical ``key=value&key=value`` message, and an HMAC-SHA256 digest is
    computed with the application's shared secret.  The hex digest is compared
    against the supplied ``hmac`` value in constant time.

    Args:
        params: Flattened request query parameters (single value per key).
        secret: The Shopify API shared secret.

    Returns:
        ``True`` when the signature is present and valid, ``False`` otherwise.
    """
    if not secret:
        logger.error("Shopify OAuth failure: SHOPIFY_API_SECRET is not configured")
        return False

    provided_hmac = params.get("hmac")
    if not provided_hmac:
        return False

    message_pairs = [
        f"{key}={value}"
        for key, value in sorted(params.items())
        if key not in HMAC_EXCLUDED_PARAMS
    ]
    message = "&".join(message_pairs)

    computed = hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    try:
        return hmac.compare_digest(computed, provided_hmac)
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# State management (CSRF / replay protection) — SQLite backed
# --------------------------------------------------------------------------- #
class StateManager:
    """Persistent issuer and validator of one-time OAuth ``state`` tokens.

    Each issued state is bound to the shop it was created for and carries a
    creation timestamp and an absolute expiry.  Validation is single-use: a
    state is deleted atomically on lookup, which — combined with the TTL expiry
    — defends against cross-site request forgery and replay of the OAuth
    callback.

    State is stored in the ``oauth_states`` SQLite table, so it is shared across
    every Gunicorn worker and survives restarts, deploys and Render container
    recycles.  This is the fix for the multi-worker "Invalid or expired OAuth
    state" failure: the worker that issues a state and the worker that consumes
    it on the callback read and write the same durable table.
    """

    def __init__(self, ttl_seconds: int = STATE_TTL_SECONDS) -> None:
        """Initialise the state manager.

        Args:
            ttl_seconds: Number of seconds an issued state remains valid.
        """
        self._ttl_seconds: int = int(ttl_seconds)

    def issue(self, shop: str) -> str:
        """Generate, persist and return a fresh state token for ``shop``.

        Args:
            shop: The validated shop domain the state is bound to.

        Returns:
            A cryptographically secure, URL-safe state token.

        Raises:
            ValueError: If ``shop`` is empty.
        """
        if not shop:
            raise ValueError("'shop' is required to issue an OAuth state")

        _ensure_schema()
        token = secrets.token_urlsafe(STATE_TOKEN_BYTES)
        now = time.time()
        expires_at = now + self._ttl_seconds

        with _WRITE_LOCK:
            conn = _connect()
            try:
                # Opportunistically clear expired rows so the table stays small.
                conn.execute("DELETE FROM oauth_states WHERE expires_at < ?", (now,))
                conn.execute(
                    "INSERT OR REPLACE INTO oauth_states "
                    "(state, shop, created_at, expires_at) VALUES (?, ?, ?, ?)",
                    (token, shop, now, expires_at),
                )
                conn.commit()
            finally:
                conn.close()

        logger.info("STATE_CREATED | shop=%s ttl=%ss", shop, self._ttl_seconds)
        return token

    def consume(self, token: Optional[str], shop: str) -> bool:
        """Validate and single-use consume a state token.

        The row is deleted atomically on lookup (``DELETE ... RETURNING``) so a
        given state can be used at most once, even if two workers race on the
        same callback.  Expiry and shop binding are then verified in constant
        time.

        Args:
            token: The state value returned by Shopify on the callback.
            shop: The shop domain the callback claims to be for.

        Returns:
            ``True`` when the token existed, was unexpired and matched ``shop``.
            The token is removed regardless of the outcome when it is found.
        """
        if not token or not shop:
            return False

        _ensure_schema()
        with _WRITE_LOCK:
            conn = _connect()
            try:
                row = conn.execute(
                    "DELETE FROM oauth_states WHERE state = ? "
                    "RETURNING shop, expires_at",
                    (token,),
                ).fetchone()
                conn.commit()
            finally:
                conn.close()

        if row is None:
            logger.warning("STATE_INVALID | shop=%s (unknown or already used)", shop)
            return False

        stored_shop = str(row["shop"])
        expires_at = float(row["expires_at"])

        if time.time() > expires_at:
            logger.warning("STATE_EXPIRED | shop=%s", shop)
            return False

        if not secrets.compare_digest(stored_shop, str(shop)):
            logger.warning(
                "STATE_MISMATCH | expected=%s got=%s", stored_shop, shop
            )
            return False

        logger.info("STATE_VALIDATED | shop=%s", shop)
        return True

    def cleanup(self) -> int:
        """Delete all expired state rows.

        Returns:
            The number of expired states removed.
        """
        _ensure_schema()
        now = time.time()
        with _WRITE_LOCK:
            conn = _connect()
            try:
                cursor = conn.execute(
                    "DELETE FROM oauth_states WHERE expires_at < ?", (now,)
                )
                removed = cursor.rowcount if cursor.rowcount is not None else 0
                conn.commit()
            finally:
                conn.close()
        if removed:
            logger.info("STATE_CLEANUP | removed=%d expired state(s)", removed)
        return int(removed)


# --------------------------------------------------------------------------- #
# Token store — SQLite backed
# --------------------------------------------------------------------------- #
class TokenStore:
    """Persistent store of per-shop Shopify access tokens.

    Tokens are stored in the ``shop_tokens`` SQLite table so installations
    survive restarts and deploys and are visible to every Gunicorn worker.  The
    installation order is preserved (via ``installed_at``) so that
    :meth:`get_default_shop` deterministically resolves the "first installed"
    shop, exactly as the previous in-memory implementation did.

    The public API is unchanged and is relied upon by other project modules
    (``shopify.client``, ``shopify.search``, ``utils.health``, the admin
    dashboard).  Both :meth:`get` and :meth:`get_token` are provided as they are
    both used across the codebase.
    """

    def __init__(self) -> None:
        """Initialise the token store (schema is created lazily on first use)."""
        # No in-memory state: the SQLite table is the single source of truth.
        return None

    def save(self, shop: str, token: str) -> None:
        """Persist (or update) the access token for a shop.

        Args:
            shop: The shop domain the token belongs to.
            token: The Shopify access token to store.

        Raises:
            ValueError: If ``shop`` or ``token`` is empty.
        """
        if not shop or not token:
            raise ValueError("Both 'shop' and 'token' are required to save a token")

        _ensure_schema()
        now = time.time()
        with _WRITE_LOCK:
            conn = _connect()
            try:
                existed = conn.execute(
                    "SELECT 1 FROM shop_tokens WHERE shop = ?", (shop,)
                ).fetchone()
                conn.execute(
                    "INSERT INTO shop_tokens (shop, access_token, installed_at, "
                    "updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(shop) DO UPDATE SET "
                    "access_token = excluded.access_token, "
                    "updated_at = excluded.updated_at",
                    (shop, token, now, now),
                )
                conn.commit()
            finally:
                conn.close()

        if existed:
            logger.info("TOKEN_SAVED | shop=%s (updated)", shop)
        else:
            logger.info("TOKEN_SAVED | shop=%s", shop)
            logger.info("SHOP_INSTALLED | shop=%s", shop)

    def get(self, shop: str) -> Optional[str]:
        """Return the stored token for ``shop`` or ``None`` if absent.

        Args:
            shop: The shop domain to look up.

        Returns:
            The access token when present, otherwise ``None``.
        """
        if not shop:
            return None
        _ensure_schema()
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT access_token FROM shop_tokens WHERE shop = ?", (shop,)
            ).fetchone()
        finally:
            conn.close()
        return row["access_token"] if row else None

    def get_token(self, shop: str) -> Optional[str]:
        """Return the stored token for ``shop`` (alias of :meth:`get`).

        Provided because :mod:`shopify.client` and :mod:`shopify.search` resolve
        tokens via ``token_store.get_token(shop)``.

        Args:
            shop: The shop domain to look up.

        Returns:
            The access token when present, otherwise ``None``.
        """
        return self.get(shop)

    def remove(self, shop: str) -> bool:
        """Remove any stored token for ``shop``.

        Args:
            shop: The shop domain to forget.

        Returns:
            ``True`` when a token was removed, ``False`` when none existed.
        """
        if not shop:
            return False
        _ensure_schema()
        with _WRITE_LOCK:
            conn = _connect()
            try:
                cursor = conn.execute(
                    "DELETE FROM shop_tokens WHERE shop = ?", (shop,)
                )
                removed = (cursor.rowcount or 0) > 0
                conn.commit()
            finally:
                conn.close()
        if removed:
            logger.info("SHOP_REMOVED | shop=%s", shop)
        return removed

    def exists(self, shop: str) -> bool:
        """Return whether a token is stored for ``shop``.

        Args:
            shop: The shop domain to check.

        Returns:
            ``True`` when a token exists for the shop.
        """
        if not shop:
            return False
        _ensure_schema()
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM shop_tokens WHERE shop = ?", (shop,)
            ).fetchone()
        finally:
            conn.close()
        return row is not None

    def list_shops(self) -> List[str]:
        """Return installed shops in installation order.

        Returns:
            A new list of shop domains; safe for the caller to mutate.
        """
        _ensure_schema()
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT shop FROM shop_tokens ORDER BY installed_at ASC, rowid ASC"
            ).fetchall()
        finally:
            conn.close()
        return [row["shop"] for row in rows]

    def get_default_shop(self) -> Optional[str]:
        """Resolve the default shop for the application.

        Resolution order:
            1. ``SHOPIFY_DEFAULT_SHOP`` when it is set *and* installed.
            2. The single installed shop when exactly one exists.
            3. The first installed shop when several exist.
            4. ``None`` when no shop is installed.

        Returns:
            The resolved default shop domain, or ``None``.
        """
        shops = self.list_shops()
        if not shops:
            return None

        configured = _get_configured_default_shop()
        if configured:
            configured = configured.strip().lower()
            if configured in shops:
                return configured

        return shops[0]

    def shop_count(self) -> int:
        """Return the number of installed shops.

        Returns:
            The count of shops with a stored token.
        """
        _ensure_schema()
        conn = _connect()
        try:
            row = conn.execute("SELECT COUNT(*) AS c FROM shop_tokens").fetchone()
        finally:
            conn.close()
        return int(row["c"]) if row else 0


# --------------------------------------------------------------------------- #
# Module-level singletons (durable, shared across workers via SQLite)
# --------------------------------------------------------------------------- #
token_store: TokenStore = TokenStore()
_state_manager: StateManager = StateManager()


# --------------------------------------------------------------------------- #
# OAuth URL builder
# --------------------------------------------------------------------------- #
def build_authorization_url(shop: str, state: str) -> str:
    """Construct the Shopify authorization URL for the install redirect.

    Args:
        shop: The validated shop domain to authorize against.
        state: The one-time CSRF state token to embed.

    Returns:
        A fully-qualified Shopify authorization URL.

    Raises:
        RuntimeError: If required configuration (API key or app URL) is absent.
    """
    api_key = _get_api_key()
    redirect_uri = _get_redirect_uri()

    if not api_key:
        raise RuntimeError("SHOPIFY_API_KEY is not configured")
    if not redirect_uri:
        raise RuntimeError("SHOPIFY_APP_URL is not configured")

    query = urllib.parse.urlencode(
        {
            "client_id": api_key,
            "scope": _get_scopes(),
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    base = SHOPIFY_AUTHORIZE_URL_TEMPLATE.format(shop=shop)
    return f"{base}?{query}"


# --------------------------------------------------------------------------- #
# Token exchange
# --------------------------------------------------------------------------- #
def exchange_code_for_token(shop: str, code: str) -> str:
    """Exchange an authorization code for a permanent access token.

    Performs the server-to-server ``POST`` to
    ``https://{shop}/admin/oauth/access_token`` using only the Python standard
    library, so the module introduces no third-party HTTP dependency.

    Args:
        shop: The validated shop domain.
        code: The authorization code returned to the OAuth callback.

    Returns:
        The Shopify access token.

    Raises:
        RuntimeError: When configuration is missing, the HTTP call fails, or
            the response does not contain an access token.
    """
    api_key = _get_api_key()
    api_secret = _get_api_secret()

    if not api_key or not api_secret:
        raise RuntimeError("Shopify API credentials are not configured")

    url = SHOPIFY_ACCESS_TOKEN_URL_TEMPLATE.format(shop=shop)
    payload = json.dumps(
        {
            "client_id": api_key,
            "client_secret": api_secret,
            "code": code,
        }
    ).encode("utf-8")

    http_request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(
            http_request, timeout=TOKEN_EXCHANGE_TIMEOUT_SECONDS
        ) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:  # noqa: PERF203 - explicit branch
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise RuntimeError(
            f"Token exchange failed with HTTP {exc.code}: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Token exchange request error: {exc.reason}") from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Token exchange returned a non-JSON response") from exc

    access_token = parsed.get("access_token")
    if not access_token or not isinstance(access_token, str):
        raise RuntimeError("Token exchange response did not include an access token")

    return access_token


# --------------------------------------------------------------------------- #
# JSON response helpers
# --------------------------------------------------------------------------- #
def _json_error(message: str, status_code: int, **extra: Any) -> Response:
    """Build a standardised JSON error response.

    Args:
        message: Human-readable error description.
        status_code: HTTP status code to attach to the response.
        **extra: Optional additional fields to include in the payload.

    Returns:
        A Flask JSON :class:`~flask.Response` carrying ``status_code``.
    """
    payload: Dict[str, Any] = {"ok": False, "error": message}
    payload.update(extra)
    response = jsonify(payload)
    response.status_code = status_code
    return response


def _json_success(status_code: int = 200, **fields: Any) -> Response:
    """Build a standardised JSON success response.

    Args:
        status_code: HTTP status code to attach to the response.
        **fields: Fields to include alongside ``{"ok": True}``.

    Returns:
        A Flask JSON :class:`~flask.Response` carrying ``status_code``.
    """
    payload: Dict[str, Any] = {"ok": True}
    payload.update(fields)
    response = jsonify(payload)
    response.status_code = status_code
    return response


def _flatten_args() -> Dict[str, str]:
    """Return request query parameters as a flat ``str`` -> ``str`` mapping.

    Only the first value of any repeated parameter is retained, matching the
    canonicalisation Shopify performs when computing the request HMAC.

    Returns:
        A dictionary of the request's query parameters.
    """
    return {key: request.args.get(key, "") for key in request.args.keys()}


# --------------------------------------------------------------------------- #
# Blueprint and routes
# --------------------------------------------------------------------------- #
shopify_auth_bp: Blueprint = Blueprint(
    "shopify_auth",
    __name__,
    url_prefix=BLUEPRINT_URL_PREFIX,
)


@shopify_auth_bp.route("/install", methods=["GET"])
def shopify_install() -> Response:
    """Begin the Shopify OAuth flow by redirecting to the authorize screen.

    Query Args:
        shop: The ``*.myshopify.com`` domain to install the app on.

    Returns:
        A ``302`` redirect to Shopify on success, or a JSON error response.
    """
    try:
        shop = _normalise_shop(request.args.get("shop"))
        if shop is None:
            logger.warning("Shopify OAuth failure: invalid shop domain on install")
            return _json_error("A valid 'shop' parameter is required", 400)

        if not _get_api_key() or not _get_redirect_uri():
            logger.error("Shopify OAuth failure: application is not configured")
            return _json_error("Shopify integration is not configured", 500)

        logger.info("OAUTH_INSTALL | shop=%s", shop)

        state = _state_manager.issue(shop)
        authorize_url = build_authorization_url(shop, state)

        logger.info("OAUTH_INSTALL | redirecting shop=%s to Shopify authorize", shop)
        return redirect(authorize_url, code=302)

    except RuntimeError as exc:
        logger.error("Shopify OAuth failure during install: %s", exc)
        return _json_error("Shopify integration is not configured", 500)
    except Exception as exc:  # noqa: BLE001 - never let Flask crash
        logger.exception("Unexpected error during Shopify install: %s", exc)
        return _json_error("Internal server error", 500)


@shopify_auth_bp.route("/callback", methods=["GET"])
def shopify_callback() -> Response:
    """Handle the Shopify OAuth callback and complete token exchange.

    Validates the request ``state``, ``hmac`` and ``shop`` parameters, then
    exchanges the authorization ``code`` for an access token which is persisted
    in :data:`token_store`.

    Returns:
        A JSON success response on completion, or a JSON error response with an
        appropriate HTTP status code on any validation or processing failure.
    """
    try:
        params = _flatten_args()

        shop = _normalise_shop(params.get("shop"))
        if shop is None:
            logger.warning("Shopify OAuth failure: invalid shop on callback")
            return _json_error("A valid 'shop' parameter is required", 400)

        logger.info("OAUTH_CALLBACK | shop=%s", shop)

        code = params.get("code")
        if not code:
            logger.warning("Shopify OAuth failure: missing code for %s", shop)
            return _json_error("Missing authorization 'code'", 400)

        if not verify_hmac(params, _get_api_secret()):
            logger.warning("Shopify OAuth failure: invalid HMAC for %s", shop)
            return _json_error("HMAC validation failed", 401)

        if not _state_manager.consume(params.get("state"), shop):
            logger.warning("Shopify OAuth failure: invalid state for %s", shop)
            return _json_error("Invalid or expired OAuth state", 403)

        access_token = exchange_code_for_token(shop, code)

        token_store.save(shop, access_token)
        logger.info("OAUTH_CALLBACK | success shop=%s", shop)

        return _json_success(
            status_code=200,
            shop=shop,
            message="Shopify app installed successfully",
            installed_shops=token_store.shop_count(),
        )

    except RuntimeError as exc:
        logger.error("Shopify OAuth failure during callback: %s", exc)
        return _json_error("Failed to complete Shopify authorization", 502)
    except Exception as exc:  # noqa: BLE001 - never let Flask crash
        logger.exception("Unexpected error during Shopify callback: %s", exc)
        return _json_error("Internal server error", 500)


@shopify_auth_bp.route("/status", methods=["GET"])
def shopify_status() -> Response:
    """Report the current installation status of the integration.

    Returns:
        A JSON response listing installed shops and the resolved default shop.
    """
    try:
        return _json_success(
            status_code=200,
            shops=token_store.list_shops(),
            default_shop=token_store.get_default_shop(),
            shop_count=token_store.shop_count(),
        )
    except Exception as exc:  # noqa: BLE001 - never let Flask crash
        logger.exception("Unexpected error building Shopify status: %s", exc)
        return _json_error("Internal server error", 500)


__all__ = ["shopify_auth_bp", "token_store", "TokenStore"]
