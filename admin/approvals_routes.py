"""
admin/approvals_routes.py
--------------------------
Admin "Approvals" area for the v10.0 human-approval workflow. Managers review
sensitive agent/admin actions that were queued for sign-off
(:mod:`agents.approvals`): pending requests can be approved (which executes the
underlying tool) or rejected, and recently decided requests are shown as history.

Mounted at ``/admin/approvals``. Viewing requires an authenticated session
(:func:`admin.security.login_required`); approving/rejecting additionally
requires at least the ``manager`` role (:func:`admin.rbac.role_required`) and
CSRF validation.
"""

from __future__ import annotations

from typing import Any, Dict

from flask import Blueprint, flash, redirect, render_template, request, url_for

from admin.rbac import role_required
from admin.security import (
    csrf_protect,
    current_user,
    get_csrf_token,
    is_authenticated,
    login_required,
)
from agents import approvals
from utils.logging import logger

admin_approvals_bp = Blueprint(
    "admin_approvals",
    __name__,
    url_prefix="/admin/approvals",
    template_folder="templates",
)


# --------------------------------------------------------------------------
# Template context (mirrors the admin blueprint's globals)
# --------------------------------------------------------------------------

@admin_approvals_bp.context_processor
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
# Views
# --------------------------------------------------------------------------

@admin_approvals_bp.route("/", methods=["GET"])
@login_required
def approvals_home() -> Any:
    """Render the pending-approvals queue plus recently decided history."""
    pending = approvals.list_approvals(status="pending", limit=100)
    history = [
        row
        for row in approvals.list_approvals(limit=100)
        if row.get("status") != "pending"
    ][:25]
    return render_template(
        "admin/approvals.html",
        pending=pending,
        history=history,
        pending_count=approvals.pending_count(),
    )


@admin_approvals_bp.route("/<int:aid>/approve", methods=["POST"])
@login_required
@role_required("manager")
@csrf_protect
def approve_request(aid: int) -> Any:
    """Approve a pending request, executing its underlying tool."""
    actor = current_user() or "admin"
    try:
        result = approvals.approve(aid, decided_by=actor)
    except Exception as exc:  # noqa: BLE001 - never 500 the admin on a decision
        logger.error("ADMIN | approve #%s failed: %s", aid, exc)
        flash(f"Approval failed: {exc}", "error")
        return redirect(url_for("admin_approvals.approvals_home"))

    if result.get("ok"):
        flash(f"Approved request #{aid} — action executed.", "success")
    else:
        flash(
            f"Request #{aid} approved but action {result.get('status', 'failed')}: "
            f"{result.get('error', 'unknown error')}.",
            "error",
        )
    return redirect(url_for("admin_approvals.approvals_home"))


@admin_approvals_bp.route("/<int:aid>/reject", methods=["POST"])
@login_required
@role_required("manager")
@csrf_protect
def reject_request(aid: int) -> Any:
    """Reject a pending request without executing it."""
    actor = current_user() or "admin"
    reason = (request.form.get("reason") or "").strip() or None
    try:
        result = approvals.reject(aid, decided_by=actor, reason=reason)
    except Exception as exc:  # noqa: BLE001
        logger.error("ADMIN | reject #%s failed: %s", aid, exc)
        flash(f"Rejection failed: {exc}", "error")
        return redirect(url_for("admin_approvals.approvals_home"))

    if result.get("ok"):
        flash(f"Rejected request #{aid}.", "success")
    else:
        flash(f"Could not reject request #{aid}: {result.get('error', 'unknown error')}.", "error")
    return redirect(url_for("admin_approvals.approvals_home"))
