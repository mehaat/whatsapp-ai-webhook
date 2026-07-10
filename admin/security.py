"""
admin/security.py
------------------
Security primitives for the Admin Dashboard:

    * Password hashing / verification (Werkzeug pbkdf2 — Werkzeug ships with
      Flask, so no new dependency is introduced).
    * ``login_required`` decorator with idle-session-timeout enforcement.
    * Per-session CSRF token generation + validation for all state-changing
      (POST) requests.
    * A per-IP login rate limiter reusing the project's :class:`RateLimiter`.
    * Small input-validation helpers (length caps, safe identifiers).

None of these helpers touch or depend on the existing WhatsApp / Shopify / AI
code paths.
"""

from __future__ import annotations

import functools
import hmac
import secrets
import time
from typing import Callable

from flask import (
    Response,
    current_app,
    jsonify,
    redirect,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from admin.config import admin_config
from utils.logging import logger
from utils.ratelimit import RateLimiter

# Per-IP login throttle (sliding window). Configurable via env.
_login_limiter = RateLimiter(
    max_requests=admin_config.login_max_attempts,
    window_seconds=admin_config.login_window_sec,
)

_SESSION_USER_KEY = "admin_user"
_SESSION_LAST_ACTIVE = "admin_last_active"
_SESSION_CSRF = "admin_csrf"


# --------------------------------------------------------------------------
# Password handling
# --------------------------------------------------------------------------

def hash_password(plaintext: str) -> str:
    """Return a salted pbkdf2 hash for a plaintext password."""
    return generate_password_hash(plaintext, method="pbkdf2:sha256")


def verify_password(candidate: str) -> bool:
    """Verify a candidate password against the configured admin credentials.

    Precedence:
        1. ``ADMIN_PASSWORD_HASH`` (Werkzeug hash) when set.
        2. ``ADMIN_PASSWORD`` (plaintext) compared in constant time.
    """
    if not candidate:
        return False
    if admin_config.password_hash:
        try:
            return check_password_hash(admin_config.password_hash, candidate)
        except Exception as exc:  # noqa: BLE001 - malformed hash must not 500
            logger.error("ADMIN | Invalid ADMIN_PASSWORD_HASH: %s", exc)
            return False
    if admin_config.password:
        return hmac.compare_digest(candidate, admin_config.password)
    return False


def verify_username(candidate: str) -> bool:
    """Constant-time username comparison."""
    return bool(candidate) and hmac.compare_digest(candidate, admin_config.username)


# --------------------------------------------------------------------------
# Login rate limiting
# --------------------------------------------------------------------------

def _client_ip() -> str:
    """Best-effort client IP, honouring a single proxy hop (Render)."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def login_attempt_allowed() -> bool:
    """Return True if this client IP may attempt a login right now."""
    return _login_limiter.is_allowed(f"login:{_client_ip()}")


# --------------------------------------------------------------------------
# Session lifecycle
# --------------------------------------------------------------------------

def start_session(username: str) -> None:
    """Establish an authenticated, permanent (timeout-bound) session."""
    session.clear()
    session.permanent = True
    session[_SESSION_USER_KEY] = username
    session[_SESSION_LAST_ACTIVE] = int(time.time())
    session[_SESSION_CSRF] = secrets.token_urlsafe(32)


def end_session() -> None:
    """Destroy the current session."""
    session.clear()


def current_user() -> str:
    """Return the logged-in username, or an empty string."""
    return session.get(_SESSION_USER_KEY, "")


def _session_expired() -> bool:
    """True when the idle-timeout window has elapsed since last activity."""
    last = session.get(_SESSION_LAST_ACTIVE)
    if last is None:
        return True
    idle = time.time() - float(last)
    return idle > admin_config.session_timeout_min * 60


def is_authenticated() -> bool:
    """True when a valid, non-expired admin session exists."""
    if not session.get(_SESSION_USER_KEY):
        return False
    if _session_expired():
        end_session()
        return False
    session[_SESSION_LAST_ACTIVE] = int(time.time())
    return True


# --------------------------------------------------------------------------
# CSRF protection
# --------------------------------------------------------------------------

def get_csrf_token() -> str:
    """Return (creating if needed) the per-session CSRF token."""
    token = session.get(_SESSION_CSRF)
    if not token:
        token = secrets.token_urlsafe(32)
        session[_SESSION_CSRF] = token
    return token


def validate_csrf() -> bool:
    """Validate the CSRF token from an incoming state-changing request.

    The token may arrive via the ``X-CSRF-Token`` header (AJAX), a
    ``csrf_token`` form field (HTML forms), or a JSON body field.
    """
    expected = session.get(_SESSION_CSRF, "")
    provided = request.headers.get("X-CSRF-Token", "") or request.form.get(
        "csrf_token", ""
    )
    if not provided and request.is_json:
        provided = (request.get_json(silent=True) or {}).get("csrf_token", "")
    return bool(expected) and bool(provided) and hmac.compare_digest(expected, provided)


# --------------------------------------------------------------------------
# Decorators
# --------------------------------------------------------------------------

def login_required(view: Callable) -> Callable:
    """Protect a view: redirect browsers to login, 401 for API/JSON callers."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if is_authenticated():
            return view(*args, **kwargs)
        if _wants_json():
            return jsonify({"error": "authentication_required"}), 401
        return redirect(url_for("admin.login", next=request.path))

    return wrapped


def csrf_protect(view: Callable) -> Callable:
    """Reject state-changing requests that fail CSRF validation."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not validate_csrf():
            logger.warning("ADMIN | CSRF validation failed for %s", request.path)
            return jsonify({"error": "invalid_csrf_token"}), 400
        return view(*args, **kwargs)

    return wrapped


def _wants_json() -> bool:
    """Heuristic: is the caller an API/XHR client rather than a browser nav?"""
    if request.path.startswith("/admin/api/"):
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


# --------------------------------------------------------------------------
# Input validation helpers
# --------------------------------------------------------------------------

def clean_query(value: str, max_length: int = 120) -> str:
    """Trim and length-cap a free-text search/query parameter."""
    if not value:
        return ""
    return value.strip()[:max_length]


def secure_cookie_config(is_https: bool) -> dict:
    """Return Flask session-cookie settings (Secure only under HTTPS)."""
    return {
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": bool(is_https),
        "SESSION_COOKIE_NAME": "mehaat_admin_session",
    }
