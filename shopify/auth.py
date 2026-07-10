"""Shopify OAuth authentication module for ME-HAAT Fashion AI Bot v4.0.

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
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
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
# State management (CSRF / replay protection)
# --------------------------------------------------------------------------- #
class StateManager:
    """Thread-safe issuer and validator of one-time OAuth ``state`` tokens.

    Each issued state is bound to the shop it was created for and carries a
    creation timestamp.  Validation is single-use: a state is consumed on
    successful verification, which — combined with the TTL expiry — defends
    against cross-site request forgery and replay of the OAuth callback.
    """

    def __init__(self, ttl_seconds: int = STATE_TTL_SECONDS) -> None:
        """Initialise the state manager.

        Args:
            ttl_seconds: Number of seconds an issued state remains valid.
        """
        self._ttl_seconds: int = ttl_seconds
        self._lock: threading.Lock = threading.Lock()
        self._states: Dict[str, Dict[str, Any]] = {}

    def issue(self, shop: str) -> str:
        """Generate, store and return a fresh state token for ``shop``.

        Args:
            shop: The validated shop domain the state is bound to.

        Returns:
            A cryptographically secure, URL-safe state token.
        """
        token = secrets.token_urlsafe(STATE_TOKEN_BYTES)
        with self._lock:
            self._purge_expired_locked()
            self._states[token] = {"shop": shop, "created_at": time.time()}
        return token

    def consume(self, token: Optional[str], shop: str) -> bool:
        """Validate and single-use consume a state token.

        Args:
            token: The state value returned by Shopify on the callback.
            shop: The shop domain the callback claims to be for.

        Returns:
            ``True`` when the token exists, is unexpired and matches ``shop``.
            The token is removed regardless of the outcome when it is found.
        """
        if not token:
            return False

        with self._lock:
            self._purge_expired_locked()
            record = self._states.pop(token, None)

        if record is None:
            return False

        if time.time() - record["created_at"] > self._ttl_seconds:
            return False

        return secrets.compare_digest(str(record["shop"]), str(shop))

    def _purge_expired_locked(self) -> None:
        """Drop expired states.  Caller must already hold ``self._lock``."""
        now = time.time()
        expired = [
            token
            for token, record in self._states.items()
            if now - record["created_at"] > self._ttl_seconds
        ]
        for token in expired:
            self._states.pop(token, None)


# --------------------------------------------------------------------------- #
# Token store
# --------------------------------------------------------------------------- #
class TokenStore:
    """Thread-safe in-memory store of per-shop Shopify access tokens.

    The store preserves installation order so that :meth:`get_default_shop`
    can deterministically resolve the "first installed" shop.  All mutating and
    reading operations are guarded by a single lock to eliminate race
    conditions under Flask's threaded request handling.

    This class is the sole intentional piece of shared mutable state in the
    module and its public API is relied upon by other project modules.
    """

    def __init__(self) -> None:
        """Initialise an empty, lock-protected token store."""
        self._lock: threading.Lock = threading.Lock()
        self._tokens: Dict[str, str] = {}
        self._order: List[str] = []

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

        with self._lock:
            if shop not in self._tokens:
                self._order.append(shop)
            self._tokens[shop] = token

    def get(self, shop: str) -> Optional[str]:
        """Return the stored token for ``shop`` or ``None`` if absent.

        Args:
            shop: The shop domain to look up.

        Returns:
            The access token when present, otherwise ``None``.
        """
        with self._lock:
            return self._tokens.get(shop)

    def remove(self, shop: str) -> bool:
        """Remove any stored token for ``shop``.

        Args:
            shop: The shop domain to forget.

        Returns:
            ``True`` when a token was removed, ``False`` when none existed.
        """
        with self._lock:
            existed = shop in self._tokens
            self._tokens.pop(shop, None)
            if shop in self._order:
                self._order.remove(shop)
            return existed

    def exists(self, shop: str) -> bool:
        """Return whether a token is stored for ``shop``.

        Args:
            shop: The shop domain to check.

        Returns:
            ``True`` when a token exists for the shop.
        """
        with self._lock:
            return shop in self._tokens

    def list_shops(self) -> List[str]:
        """Return installed shops in installation order.

        Returns:
            A new list of shop domains; safe for the caller to mutate.
        """
        with self._lock:
            return list(self._order)

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
        with self._lock:
            if not self._order:
                return None

            configured = _get_configured_default_shop()
            if configured:
                configured = configured.strip().lower()
                if configured in self._tokens:
                    return configured

            return self._order[0]

    def shop_count(self) -> int:
        """Return the number of installed shops.

        Returns:
            The count of shops with a stored token.
        """
        with self._lock:
            return len(self._order)


# --------------------------------------------------------------------------- #
# Module-level singletons (the only shared mutable state)
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

        logger.info("Shopify OAuth started for shop %s", shop)

        state = _state_manager.issue(shop)
        authorize_url = build_authorization_url(shop, state)

        logger.info("Redirecting shop %s to Shopify authorization", shop)
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
        logger.info("Token stored for shop %s", shop)
        logger.info("Shop installed: %s", shop)
        logger.info("Shopify OAuth success for shop %s", shop)

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