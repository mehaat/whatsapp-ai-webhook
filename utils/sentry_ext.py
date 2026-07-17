"""
utils/sentry_ext.py
--------------------
v9.0 Sentry deep-integration helpers for ME-HAAT Fashion AI Bot.

``sentry-sdk`` is an *optional* dependency: this module guards its import (and
each integration's import) so the application runs identically whether or not
Sentry is installed or a DSN is configured.

Typical wiring (done by the app, not here)::

    from utils.sentry_ext import init_sentry
    init_sentry(app)  # after Flask app is created

Every function is guarded and never raises.
"""

from __future__ import annotations

from typing import Any, Optional

from config import config
from utils.logging import logger

# Whether Sentry was successfully initialised this process.
_active = False


def sentry_active() -> bool:
    """Return whether Sentry is initialised and active.

    Returns:
        ``True`` if :func:`init_sentry` succeeded, else ``False``.
    """
    return _active


def init_sentry(flask_app: Optional[Any] = None) -> bool:
    """Initialise the Sentry SDK if a DSN is configured and the SDK is present.

    Enables whichever of the Flask and Celery integrations are importable, sets
    ``environment``, ``traces_sample_rate`` and ``release`` from config, and
    disables PII (``send_default_pii=False``).

    Args:
        flask_app: Optional Flask app; accepted for call-site symmetry. The
            Flask integration hooks in globally, so the instance is not
            required.

    Returns:
        ``True`` if Sentry is now active, ``False`` otherwise. Never raises.
    """
    global _active

    dsn = getattr(config, "sentry_dsn", "") or ""
    if not dsn:
        logger.info("sentry: no DSN configured; Sentry disabled")
        return False

    try:
        import sentry_sdk
    except Exception:
        logger.info("sentry: sentry-sdk not installed; Sentry disabled")
        return False

    integrations = []
    try:
        from sentry_sdk.integrations.flask import FlaskIntegration

        integrations.append(FlaskIntegration())
    except Exception as exc:
        logger.debug("sentry: Flask integration unavailable: %r", exc)
    try:
        from sentry_sdk.integrations.celery import CeleryIntegration

        integrations.append(CeleryIntegration())
    except Exception as exc:
        logger.debug("sentry: Celery integration unavailable: %r", exc)

    try:
        sentry_sdk.init(
            dsn=dsn,
            integrations=integrations,
            environment=getattr(config, "sentry_environment", "production"),
            traces_sample_rate=float(getattr(config, "sentry_traces_sample_rate", 0.0) or 0.0),
            release=f"mehaat@{config.version}",
            send_default_pii=False,
        )
    except Exception as exc:
        logger.warning("sentry: init failed: %r", exc)
        return False

    _active = True
    logger.info(
        "sentry: initialised (env=%s, integrations=%s)",
        getattr(config, "sentry_environment", "production"),
        [type(i).__name__ for i in integrations],
    )
    return True


def capture(exc: BaseException) -> None:
    """Report an exception to Sentry, or log it if Sentry is inactive.

    Args:
        exc: The exception instance to capture.

    Returns:
        ``None``. Never raises.
    """
    if _active:
        try:
            import sentry_sdk

            sentry_sdk.capture_exception(exc)
            return
        except Exception as inner:  # pragma: no cover - defensive
            logger.debug("sentry: capture failed: %r", inner)
    logger.error("captured exception (sentry inactive): %r", exc)


def add_breadcrumb(message: str, category: str = "app", level: str = "info") -> None:
    """Add a Sentry breadcrumb (no-op when Sentry is inactive).

    Args:
        message: The breadcrumb message.
        category: Breadcrumb category (default ``"app"``).
        level: Severity level (default ``"info"``).

    Returns:
        ``None``. Never raises.
    """
    if not _active:
        return
    try:
        import sentry_sdk

        sentry_sdk.add_breadcrumb(message=message, category=category, level=level)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("sentry: add_breadcrumb failed: %r", exc)
