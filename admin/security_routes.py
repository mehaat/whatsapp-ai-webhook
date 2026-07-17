"""
admin/security_routes.py
-------------------------
Admin "Security" area (v7.0): a security overview, the login-history audit view
(admin-only, exportable), and per-user two-factor (TOTP) enrolment.

This is an additive blueprint mounted at ``/admin/security``. Every route is
protected by :func:`admin.security.login_required`; the login-history table is
further gated by :func:`admin.rbac.role_required` at the ``admin`` level, and
the 2FA mutations enforce :func:`admin.security.csrf_protect`. It never mutates
the existing ``/admin`` blueprint and shares its session, CSRF token and
template layout (``admin/base.html``).

The building blocks in :mod:`admin.security_ext` (login recording, IP allowlist,
TOTP) are wired into the *login handler* separately; this blueprint only exposes
the operator-facing views and the 2FA self-service flow.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from flask import (
    Blueprint,
    Response,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from admin import exporter, security_ext
from admin.rbac import role_required
from admin.security import (
    csrf_protect,
    current_user,
    get_csrf_token,
    is_authenticated,
    login_required,
)
from config import config
from database.db import session_scope
from database.models import AdminUser
from utils.logging import logger

admin_security_bp = Blueprint(
    "admin_security",
    __name__,
    url_prefix="/admin/security",
    template_folder="templates",
)

# Session key holding the in-progress (unconfirmed) TOTP secret during setup.
_PENDING_2FA_KEY = "pending_totp_secret"

_LOGIN_HISTORY_COLUMNS = ["created_at", "username", "ip", "user_agent", "success"]


# --------------------------------------------------------------------------
# Template context (mirrors the admin blueprint's globals)
# --------------------------------------------------------------------------

@admin_security_bp.context_processor
def _inject_globals() -> Dict[str, Any]:
    """Expose the CSRF token + current user to this blueprint's templates."""
    return {
        "csrf_token": get_csrf_token() if is_authenticated() else "",
        "admin_user": current_user(),
        "app_version": _app_version(),
        "nav_active": request.endpoint,
    }


def _app_version() -> str:
    try:
        return config.version
    except Exception:  # noqa: BLE001
        return ""


def _current_db_user() -> Optional[Dict[str, Any]]:
    """Resolve the logged-in username to its ``admin_users`` row, if any.

    The built-in environment ``owner`` superuser has no DB row and returns
    ``None`` — callers surface a friendly "2FA applies to DB users" message.
    """
    username = current_user()
    if not username:
        return None
    try:
        with session_scope() as db:
            user = db.query(AdminUser).filter(AdminUser.username == username).first()
            if user is None:
                return None
            return {
                "id": user.id,
                "username": user.username,
                "role": user.role,
                "totp_enabled": bool(user.totp_enabled),
                "has_secret": bool(user.totp_secret),
            }
    except Exception as exc:  # noqa: BLE001
        logger.error("SECURITY | resolve current db user failed: %s", exc)
        return None


def _int_arg(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# Overview
# --------------------------------------------------------------------------

@admin_security_bp.route("/", methods=["GET"])
@login_required
def security_overview() -> Any:
    """Security landing page: 2FA status, IP allowlist state, history link."""
    db_user = _current_db_user()
    allowlist_raw = (config.admin_ip_allowlist or "").strip()
    entries = [e.strip() for e in allowlist_raw.split(",") if e.strip()]
    return render_template(
        "admin/security_overview.html",
        db_user=db_user,
        totp=(security_ext.user_totp(db_user["id"]) if db_user else None),
        two_fa_config_enabled=bool(config.admin_2fa_enabled),
        allowlist_entries=entries,
        allowlist_active=bool(entries),
    )


# --------------------------------------------------------------------------
# Login history (admin only, exportable)
# --------------------------------------------------------------------------

@admin_security_bp.route("/login-history", methods=["GET"])
@login_required
@role_required("admin")
def login_history() -> Any:
    """Table of recent admin login attempts. Supports ``?export=csv|xlsx|pdf``."""
    username = (request.args.get("username", "") or "").strip() or None
    limit = max(1, min(_int_arg("limit", 200), 2000))
    offset = max(0, _int_arg("offset", 0))
    events = security_ext.list_login_events(username=username, limit=limit, offset=offset)

    export_fmt = (request.args.get("export", "") or "").strip().lower()
    if export_fmt:
        try:
            data, mimetype, filename = exporter.export(
                export_fmt, events, _LOGIN_HISTORY_COLUMNS, "login-history",
                title="Login History",
            )
        except exporter.ExportUnavailable as exc:
            logger.warning("SECURITY | export unavailable: %s", exc)
            return {
                "error": "format_unavailable",
                "detail": str(exc),
                "hint": "CSV always works; install openpyxl (xlsx) / reportlab (pdf).",
            }, 501
        return Response(
            data,
            mimetype=mimetype,
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    return render_template(
        "admin/login_history.html",
        events=events,
        username=username or "",
        columns=_LOGIN_HISTORY_COLUMNS,
    )


# --------------------------------------------------------------------------
# Two-factor authentication (TOTP) self-service
# --------------------------------------------------------------------------

@admin_security_bp.route("/2fa", methods=["GET"])
@login_required
def twofa_setup() -> Any:
    """Show the 2FA enrolment (QR + confirm) or the disable form."""
    db_user = _current_db_user()
    if db_user is None:
        return render_template(
            "admin/twofa_setup.html",
            db_user=None,
            enabled=False,
            qr=None,
            secret=None,
        )

    status = security_ext.user_totp(db_user["id"]) or {"enabled": False}
    if status.get("enabled"):
        # Nothing pending needed when already enabled.
        session.pop(_PENDING_2FA_KEY, None)
        return render_template(
            "admin/twofa_setup.html",
            db_user=db_user,
            enabled=True,
            qr=None,
            secret=None,
        )

    # Not enabled: mint (once) a pending secret held in the session and show a QR.
    secret = session.get(_PENDING_2FA_KEY)
    if not secret:
        secret = security_ext.generate_totp_secret()
        session[_PENDING_2FA_KEY] = secret
    uri = security_ext.provisioning_uri(db_user["username"], secret)
    return render_template(
        "admin/twofa_setup.html",
        db_user=db_user,
        enabled=False,
        qr=security_ext.qr_data_uri(uri),
        secret=secret,
    )


@admin_security_bp.route("/2fa/enable", methods=["POST"])
@login_required
@csrf_protect
def twofa_enable() -> Any:
    """Verify a submitted code against the pending secret, then enable 2FA."""
    db_user = _current_db_user()
    if db_user is None:
        flash("Two-factor authentication applies to database users; the built-in "
              "environment owner cannot enrol.", "warning")
        return redirect(url_for("admin_security.twofa_setup"))

    secret = session.get(_PENDING_2FA_KEY)
    code = (request.form.get("code", "") or "").strip()
    if not secret:
        flash("Your enrolment session expired. Please scan the code again.", "error")
        return redirect(url_for("admin_security.twofa_setup"))
    if not security_ext.verify_totp(secret, code):
        flash("That code was not valid. Please try again.", "error")
        return redirect(url_for("admin_security.twofa_setup"))

    result = security_ext.enable_totp(db_user["id"], secret, actor=current_user() or "admin")
    if result.get("error"):
        flash(f"Could not enable 2FA: {result['error']}.", "error")
    else:
        session.pop(_PENDING_2FA_KEY, None)
        flash("Two-factor authentication is now enabled.", "success")
    return redirect(url_for("admin_security.twofa_setup"))


@admin_security_bp.route("/2fa/disable", methods=["POST"])
@login_required
@csrf_protect
def twofa_disable() -> Any:
    """Disable 2FA and clear the stored secret for the current user."""
    db_user = _current_db_user()
    if db_user is None:
        flash("Two-factor authentication applies to database users only.", "warning")
        return redirect(url_for("admin_security.twofa_setup"))

    result = security_ext.disable_totp(db_user["id"], actor=current_user() or "admin")
    if result.get("error"):
        flash(f"Could not disable 2FA: {result['error']}.", "error")
    else:
        session.pop(_PENDING_2FA_KEY, None)
        flash("Two-factor authentication has been disabled.", "success")
    return redirect(url_for("admin_security.twofa_setup"))
