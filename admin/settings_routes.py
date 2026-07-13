"""
admin/settings_routes.py
-------------------------
Admin "Settings" area for the v7.0 DB-backed runtime overrides. Renders a form
of the curated :data:`commerce.settings_store.EDITABLE_SETTINGS` populated with
current values, and saves each on submit.

Mounted at ``/admin/settings``. Viewing requires an authenticated session
(:func:`admin.security.login_required`); saving additionally requires the
``admin`` role (:func:`admin.rbac.role_required`) and CSRF validation. Values
written here override the env-var boot defaults for callers that opt into
:func:`commerce.settings_store.get_setting`.
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
    csrf_protect,
    current_user,
    get_csrf_token,
    is_authenticated,
    login_required,
)
from commerce import settings_store
from utils.logging import logger

admin_settings_bp = Blueprint(
    "admin_settings",
    __name__,
    url_prefix="/admin/settings",
    template_folder="templates",
)


# --------------------------------------------------------------------------
# Template context (mirrors the admin blueprint's globals)
# --------------------------------------------------------------------------

@admin_settings_bp.context_processor
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
# Settings page
# --------------------------------------------------------------------------

@admin_settings_bp.route("/", methods=["GET"])
@login_required
def settings_page() -> Any:
    """Render the editable-settings form with current DB override values."""
    current = settings_store.all_settings()
    fields = []
    for item in settings_store.EDITABLE_SETTINGS:
        value = current.get(item["key"])
        fields.append({**item, "value": value if value is not None else ""})
    return render_template("admin/settings.html", fields=fields, current=current)


@admin_settings_bp.route("/", methods=["POST"])
@login_required
@role_required("admin")
@csrf_protect
def settings_save() -> Any:
    """Persist each editable setting from the submitted form."""
    actor = current_user() or "admin"
    saved = 0
    for item in settings_store.EDITABLE_SETTINGS:
        key = item["key"]
        if item["type"] == "bool":
            value = "true" if request.form.get(key) in ("on", "true", "1") else "false"
        else:
            value = (request.form.get(key, "") or "").strip()
        try:
            settings_store.set_setting(key, value, actor=actor)
            saved += 1
        except Exception as exc:  # noqa: BLE001 - never 500 the admin on a save
            logger.error("ADMIN | settings save failed for %s: %s", key, exc)

    flash(f"Saved {saved} setting(s).", "success")
    return redirect(url_for("admin_settings.settings_page"))
