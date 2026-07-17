"""
commerce/auth.py
-----------------
Authentication primitives for the v6.0 JSON commerce API.

Three surfaces are supported so the same endpoints can serve programmatic
clients *and* the browser dashboard:

    * **Bearer JWT** — short-lived HS256 tokens minted by ``POST /api/token``
      and verified here (:func:`issue_token` / :func:`decode_token`).
    * **API key** — a static shared secret sent as ``X-API-Key`` (matched
      against ``config.api_key`` when that value is configured).
    * **Admin session** — a logged-in dashboard session, reusing
      :func:`admin.security.is_authenticated`.

The :func:`require_api_auth` decorator accepts a request when *any* of those
succeed and returns a ``401`` JSON body otherwise. None of these helpers raise
to the caller; failures degrade to "unauthenticated".
"""

from __future__ import annotations

import functools
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

import jwt
from flask import jsonify, request

from admin.config import admin_config
from admin.security import is_authenticated
from config import config
from utils.logging import logger

# JWT signing algorithm. HS256 keeps a single shared secret (no keypair mgmt).
_ALGORITHM = "HS256"


def _signing_secret() -> str:
    """Return the secret used to sign/verify JWTs.

    Prefers ``config.jwt_secret``. When that is empty, falls back to the
    admin dashboard's derived session secret so tokens still work on a
    deployment that only configured admin credentials. Returns ``""`` when no
    secret is available at all, in which case JWT auth is effectively disabled
    and only the API key / admin session paths remain.
    """
    secret = getattr(config, "jwt_secret", "") or ""
    if secret:
        return secret
    return getattr(admin_config, "secret_key", "") or ""


def _expiry_minutes() -> int:
    """Return the configured token lifetime in minutes (safe default 60)."""
    try:
        return int(getattr(config, "jwt_expiry_minutes", 60) or 60)
    except (TypeError, ValueError):
        return 60


def token_ttl_seconds() -> int:
    """Return the lifetime, in seconds, of tokens minted by :func:`issue_token`."""
    return _expiry_minutes() * 60


def issue_token(subject: str) -> str:
    """Mint a signed HS256 JWT for ``subject``.

    Args:
        subject: The token subject (typically the admin username).

    Returns:
        The encoded JWT string, or ``""`` when no signing secret is available.
    """
    secret = _signing_secret()
    if not secret:
        logger.warning("API | issue_token called with no signing secret; returning empty token")
        return ""

    now = datetime.now(timezone.utc)
    claims: Dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=_expiry_minutes()),
    }
    token = jwt.encode(claims, secret, algorithm=_ALGORITHM)
    # PyJWT>=2 returns str; older versions returned bytes.
    if isinstance(token, bytes):
        token = token.decode("utf-8")
    return token


def decode_token(token: str) -> Optional[Dict[str, Any]]:
    """Verify and decode a JWT.

    Args:
        token: The raw JWT string.

    Returns:
        The decoded claims dict when the signature and expiry are valid,
        otherwise ``None``.
    """
    if not token:
        return None
    secret = _signing_secret()
    if not secret:
        return None
    try:
        return jwt.decode(token, secret, algorithms=[_ALGORITHM])
    except jwt.PyJWTError as exc:
        logger.debug("API | JWT decode failed: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - never raise on a bad token
        logger.debug("API | JWT decode error: %s", exc)
        return None


def _bearer_token() -> str:
    """Extract the bearer token from the ``Authorization`` header, if any."""
    header = request.headers.get("Authorization", "") or ""
    parts = header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


def is_request_authenticated() -> bool:
    """Return True when the current request satisfies any auth method.

    Order of checks: Bearer JWT, then ``X-API-Key``, then an admin session.
    """
    # 1) Bearer JWT.
    token = _bearer_token()
    if token and decode_token(token) is not None:
        return True

    # 2) Static API key (only when configured).
    api_key = getattr(config, "api_key", "") or ""
    if api_key:
        provided = request.headers.get("X-API-Key", "") or ""
        if provided and provided == api_key:
            return True

    # 2b) Database-issued developer API key (v8.0), e.g. "mh_live_...".
    provided = request.headers.get("X-API-Key", "") or ""
    if provided.startswith("mh_live_"):
        try:
            from commerce.apikeys import check_rate_limit, verify_key

            record = verify_key(provided)
            if record is not None:
                if not check_rate_limit(record["prefix"], record["rate_limit_per_min"]):
                    from flask import g

                    g.api_key_rate_limited = True
                    return False
                return True
        except Exception as exc:  # noqa: BLE001 - key auth must not 500 the API
            logger.debug("API | DB api-key check failed: %s", exc)

    # 3) Authenticated admin dashboard session.
    try:
        if is_authenticated():
            return True
    except Exception as exc:  # noqa: BLE001 - session backend must not 500 the API
        logger.debug("API | admin session check failed: %s", exc)

    return False


def require_api_auth(view: Callable) -> Callable:
    """Decorator: allow the request only when it is authenticated.

    A request passes when it carries a valid ``Authorization: Bearer <jwt>``,
    a matching ``X-API-Key`` header, or a logged-in admin session. Otherwise a
    ``401`` JSON error is returned and the wrapped view is not invoked.

    Args:
        view: The Flask view function to protect.

    Returns:
        The wrapped view.
    """

    @functools.wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        if is_request_authenticated():
            # v9.0: meter developer-key usage (best-effort; never raises).
            provided = request.headers.get("X-API-Key", "") or ""
            if provided.startswith("mh_live_"):
                try:
                    from commerce.api_usage import record_usage
                    from commerce.apikeys import _parse_prefix

                    prefix = _parse_prefix(provided)
                    if prefix:
                        record_usage(prefix, f"{request.method} {request.path}")
                except Exception:  # noqa: BLE001
                    pass
            return view(*args, **kwargs)
        try:
            from flask import g

            if g.get("api_key_rate_limited"):
                return jsonify({"error": "rate_limited"}), 429
        except Exception:  # noqa: BLE001
            pass
        return jsonify({"error": "unauthorized"}), 401

    return wrapped
