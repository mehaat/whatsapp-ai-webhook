"""
admin/tenants_routes.py
-------------------------
Admin "Stores" area for the v8.0 multi-store / multi-tenant layer: a list of
tenants plus create / edit / activate-deactivate flows and a "View as" switch
that scopes the dashboard to a single store.

This is an additive blueprint mounted at ``/admin/tenants``. Every route is
protected by :func:`admin.security.login_required` *and*
:func:`admin.rbac.role_required` at the ``admin`` level. It never mutates the
core ``/admin`` blueprint and shares its session, CSRF token and template
layout (``admin/base.html``); state-changing routes additionally enforce
:func:`admin.security.csrf_protect`.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
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
from commerce import tenancy
from utils.logging import logger

admin_tenants_bp = Blueprint(
    "admin_tenants",
    __name__,
    url_prefix="/admin/tenants",
    template_folder="templates",
)

# Session key holding the "View as" tenant scope for dashboard filtering.
_SESSION_TENANT_KEY = "admin_tenant_id"


# --------------------------------------------------------------------------
# Template context (mirrors the admin blueprint's globals)
# --------------------------------------------------------------------------

@admin_tenants_bp.context_processor
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


def _active_tenant_id() -> Any:
    """Return the currently selected "View as" tenant id (or None for all)."""
    return session.get(_SESSION_TENANT_KEY)


# --------------------------------------------------------------------------
# List
# --------------------------------------------------------------------------

@admin_tenants_bp.route("/", methods=["GET"])
@login_required
@role_required("admin")
def tenants_list() -> Any:
    """Render the table of tenants with a per-row 'View as' switch."""
    return render_template(
        "admin/tenants_list.html",
        tenants=tenancy.list_tenants(),
        active_tenant_id=_active_tenant_id(),
        nav_active="admin_tenants.tenants_list",
    )


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------

@admin_tenants_bp.route("/new", methods=["GET", "POST"])
@login_required
@role_required("admin")
@csrf_protect
def tenant_new() -> Any:
    """Show and process the create-tenant form."""
    if request.method == "GET":
        return render_template(
            "admin/tenant_form.html",
            mode="create",
            tenant=None,
            nav_active="admin_tenants.tenants_list",
        )

    form = _read_form()
    config_value, config_error = _parse_config(form["config_json"])
    if config_error:
        flash(config_error, "error")
        return _rerender_create(form)

    try:
        tenant = tenancy.create_tenant(
            form["slug"],
            form["name"],
            shopify_domain=form["shopify_domain"] or None,
            whatsapp_phone_number_id=form["whatsapp_phone_number_id"] or None,
            catalog_id=form["catalog_id"] or None,
            host=form["host"] or None,
            config=config_value,
            actor=current_user() or "admin",
        )
        if not tenant:
            flash("Could not create store (slug and name are required).", "error")
            return _rerender_create(form)
        flash(f"Store {tenant['slug']!r} saved.", "success")
        return redirect(url_for("admin_tenants.tenants_list"))
    except Exception as exc:  # noqa: BLE001 - never 500 the admin on a form
        logger.error("ADMIN | create tenant failed: %s", exc)
        flash(f"Could not create store: {exc}", "error")
        return _rerender_create(form)


# --------------------------------------------------------------------------
# Edit
# --------------------------------------------------------------------------

@admin_tenants_bp.route("/<int:tid>/edit", methods=["GET", "POST"])
@login_required
@role_required("admin")
@csrf_protect
def tenant_edit(tid: int) -> Any:
    """Edit a tenant's routing fields and JSON config."""
    tenant = tenancy.get_tenant(tid)
    if tenant is None:
        flash("Store not found.", "error")
        return redirect(url_for("admin_tenants.tenants_list"))

    if request.method == "GET":
        return render_template(
            "admin/tenant_form.html",
            mode="edit",
            tenant=tenant,
            nav_active="admin_tenants.tenants_list",
        )

    form = _read_form()
    config_value, config_error = _parse_config(form["config_json"])
    if config_error:
        flash(config_error, "error")
        return render_template(
            "admin/tenant_form.html", mode="edit",
            tenant={**tenant, **form}, nav_active="admin_tenants.tenants_list",
        )

    try:
        updated = tenancy.update_tenant(
            tid,
            slug=form["slug"],
            name=form["name"],
            shopify_domain=form["shopify_domain"] or None,
            whatsapp_phone_number_id=form["whatsapp_phone_number_id"] or None,
            catalog_id=form["catalog_id"] or None,
            host=form["host"] or None,
            config=config_value,
        )
        if updated is None:
            flash("Store not found.", "error")
        else:
            flash("Store updated.", "success")
        return redirect(url_for("admin_tenants.tenants_list"))
    except Exception as exc:  # noqa: BLE001
        logger.error("ADMIN | edit tenant #%s failed: %s", tid, exc)
        flash(f"Could not update store: {exc}", "error")
        return render_template(
            "admin/tenant_form.html", mode="edit",
            tenant={**tenant, **form}, nav_active="admin_tenants.tenants_list",
        )


# --------------------------------------------------------------------------
# Toggle active
# --------------------------------------------------------------------------

@admin_tenants_bp.route("/<int:tid>/toggle", methods=["POST"])
@login_required
@role_required("admin")
@csrf_protect
def tenant_toggle(tid: int) -> Any:
    """Activate or deactivate a tenant."""
    tenant = tenancy.get_tenant(tid)
    if tenant is None:
        flash("Store not found.", "error")
        return redirect(url_for("admin_tenants.tenants_list"))
    try:
        new_active = not tenant.get("active", True)
        tenancy.set_active(tid, new_active, actor=current_user() or "admin")
        flash(
            f"Store {tenant['slug']!r} {'activated' if new_active else 'deactivated'}.",
            "success",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("ADMIN | toggle tenant #%s failed: %s", tid, exc)
        flash(f"Could not update store: {exc}", "error")
    return redirect(url_for("admin_tenants.tenants_list"))


# --------------------------------------------------------------------------
# Switch ("View as")
# --------------------------------------------------------------------------

@admin_tenants_bp.route("/switch", methods=["POST"])
@login_required
@role_required("admin")
@csrf_protect
def tenant_switch() -> Any:
    """Set the dashboard "View as" tenant scope in the session.

    An empty value or ``"all"`` clears the scope (view every store).
    """
    raw = (request.form.get("tenant_id", "") or "").strip().lower()
    if raw in ("", "all"):
        session.pop(_SESSION_TENANT_KEY, None)
        flash("Viewing all stores.", "success")
    else:
        tenant = tenancy.get_tenant(int(raw)) if raw.isdigit() else tenancy.get_tenant(raw)
        if tenant is None:
            flash("Store not found.", "error")
        else:
            session[_SESSION_TENANT_KEY] = tenant["id"]
            flash(f"Now viewing {tenant['name']}.", "success")
    return redirect(url_for("admin_tenants.tenants_list"))


# --------------------------------------------------------------------------
# Form helpers
# --------------------------------------------------------------------------

def _read_form() -> Dict[str, str]:
    """Read + length-cap the tenant form fields into a plain dict."""
    return {
        "slug": clean_query(request.form.get("slug", ""), 64),
        "name": clean_query(request.form.get("name", ""), 255),
        "shopify_domain": clean_query(request.form.get("shopify_domain", ""), 255),
        "whatsapp_phone_number_id": clean_query(
            request.form.get("whatsapp_phone_number_id", ""), 64
        ),
        "catalog_id": clean_query(request.form.get("catalog_id", ""), 128),
        "host": clean_query(request.form.get("host", ""), 255),
        "config_json": request.form.get("config_json", ""),
    }


def _parse_config(raw: str) -> tuple[Dict[str, Any], str]:
    """Parse the optional JSON config textarea.

    Returns:
        A ``(config_dict, error_message)`` tuple. ``error_message`` is empty on
        success; ``config_dict`` is ``{}`` when the field is blank.
    """
    raw = (raw or "").strip()
    if not raw:
        return {}, ""
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            return {}, "Config must be a JSON object (e.g. {\"key\": \"value\"})."
        return value, ""
    except Exception:  # noqa: BLE001
        return {}, "Config is not valid JSON."


def _rerender_create(form: Dict[str, str]) -> Any:
    """Re-render the create form preserving submitted values."""
    return render_template(
        "admin/tenant_form.html",
        mode="create",
        tenant={**form, "active": True},
        nav_active="admin_tenants.tenants_list",
    )
