"""
admin/developer_routes.py
--------------------------
Admin "Developer" area for the v8.0 API-key management system: list existing
keys, issue a new key (its plaintext shown exactly once), and revoke a key.

This is an additive blueprint mounted at ``/admin/developer``. Every route is
protected by :func:`admin.security.login_required` *and*
:func:`admin.rbac.role_required` at the ``admin`` level (only owners and admins
manage API keys). State-changing routes additionally enforce
:func:`admin.security.csrf_protect`.

It shares the existing admin session, CSRF token and template layout
(``admin/base.html``) and never mutates the core ``/admin`` blueprint.
"""

from __future__ import annotations

from typing import Any, Dict

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from admin.rbac import role_required
from admin.security import (
    clean_query,
    csrf_protect,
    current_user,
    get_csrf_token,
    is_authenticated,
    login_required,
)
from commerce import apikeys
from utils.logging import logger

admin_developer_bp = Blueprint(
    "admin_developer",
    __name__,
    url_prefix="/admin/developer",
    template_folder="templates",
)


# --------------------------------------------------------------------------
# Template context (mirrors the admin blueprint's globals)
# --------------------------------------------------------------------------

@admin_developer_bp.context_processor
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
        from config import config

        return config.version
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------
# List
# --------------------------------------------------------------------------

@admin_developer_bp.route("/keys", methods=["GET"])
@login_required
@role_required("admin")
def keys_list() -> Any:
    """Render the table of issued API keys (no secrets shown)."""
    return render_template(
        "admin/apikeys.html",
        keys=apikeys.list_keys(),
        nav_active="admin_developer.keys_list",
    )


# --------------------------------------------------------------------------
# Issue
# --------------------------------------------------------------------------

@admin_developer_bp.route("/keys", methods=["POST"])
@login_required
@role_required("admin")
@csrf_protect
def keys_create() -> Any:
    """Issue a new API key and show the plaintext value exactly once."""
    name = clean_query(request.form.get("name", ""), 255) or "Unnamed key"
    scopes = clean_query(request.form.get("scopes", "read"), 255) or "read"
    rate_raw = request.form.get("rate_limit_per_min", "120")
    try:
        rate = int(rate_raw)
    except (TypeError, ValueError):
        rate = 120

    result = apikeys.issue_key(
        name=name,
        scopes=scopes,
        rate_limit_per_min=rate,
        created_by=current_user() or "admin",
    )

    if not result or not result.get("api_key"):
        logger.error("ADMIN | issue API key failed for name=%r", name)
        flash("Could not issue API key. Please try again.", "error")
        return redirect(url_for("admin_developer.keys_list"))

    # The plaintext key is rendered ONCE here and never stored server-side.
    return render_template(
        "admin/apikey_created.html",
        created=result,
        name=name,
        nav_active="admin_developer.keys_list",
    )


# --------------------------------------------------------------------------
# Revoke
# --------------------------------------------------------------------------

@admin_developer_bp.route("/keys/<int:kid>/revoke", methods=["POST"])
@login_required
@role_required("admin")
@csrf_protect
def keys_revoke(kid: int) -> Any:
    """Revoke (deactivate) an API key."""
    result = apikeys.revoke_key(kid, actor=current_user() or "admin")
    if result.get("ok"):
        flash(f"API key #{kid} revoked.", "success")
    elif result.get("error") == "not_found":
        flash("API key not found.", "error")
    else:
        flash("Could not revoke API key.", "error")
    return redirect(url_for("admin_developer.keys_list"))
