"""
admin
-----
Login-protected Admin Dashboard for ME-HAAT Fashion AI Bot (v4.2, additive).

This package is a self-contained Flask blueprint plus a best-effort event
tracker. It never alters the existing WhatsApp webhook, Shopify OAuth, product
search, Gemini AI, health checks, or any existing route. Enabling it requires
only two lines in ``app.py`` (blueprint registration via :func:`init_admin`)
and a few guarded tracker hooks.

Public API:
    init_admin(app)   -> configure sessions + register the ``/admin`` blueprint
    admin_bp          -> the Flask blueprint (for manual registration if desired)
    tracker           -> event-recording hooks called from app.py
"""

from __future__ import annotations

from datetime import timedelta

from flask import Flask

from admin import tracker  # noqa: F401 - re-exported for app.py hooks
from admin.config import admin_config
from admin.routes import admin_bp
from admin.security import secure_cookie_config
from utils.logging import logger


def init_admin(app: Flask) -> None:
    """Attach the admin dashboard to an existing Flask app (idempotent).

    Configures a stable session-signing secret, secure cookie flags and the
    idle-session lifetime, then registers the ``/admin`` blueprint. Safe to call
    once at startup; guarded so a misconfiguration can never break app boot.
    """
    try:
        if any(bp.name == "admin" for bp in app.blueprints.values()):
            return  # already registered

        # Session signing secret (stable across Gunicorn workers).
        if not app.secret_key:
            app.secret_key = admin_config.secret_key

        # Idle-session lifetime + hardened cookies.
        app.permanent_session_lifetime = timedelta(
            minutes=admin_config.session_timeout_min
        )
        is_https = app.config.get("PREFERRED_URL_SCHEME", "https") == "https"
        app.config.update(secure_cookie_config(is_https=is_https))

        # Initialise the datastore up front so the first request is fast and any
        # path/permission problem surfaces at boot (still non-fatal).
        try:
            from admin.db import init_db

            init_db()
        except Exception as exc:  # noqa: BLE001
            logger.error("ADMIN | Datastore init deferred (will retry lazily): %s", exc)

        app.register_blueprint(admin_bp)
        logger.info("ADMIN | Dashboard mounted at /admin (%s)", admin_config.masked_summary())
        if not admin_config.credentials_configured:
            logger.warning(
                "ADMIN | ADMIN_USERNAME/ADMIN_PASSWORD not set — login is disabled "
                "until they are configured."
            )
    except Exception as exc:  # noqa: BLE001 - never let the dashboard break startup
        logger.error("ADMIN | init_admin failed; dashboard disabled: %s", exc)


__all__ = ["init_admin", "admin_bp", "tracker"]
