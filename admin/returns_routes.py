"""
admin/returns_routes.py
------------------------
Admin "Returns" area for the v7.0 Returns / Refund / Exchange (RMA) workflow: a
filterable list of every RMA, a detail page, and a single CSRF-protected form to
advance an RMA's status (recording a refund amount and resolution note).

This is an additive blueprint mounted at ``/admin/returns``. Every route is
protected by :func:`admin.security.login_required`; the state-changing POST
additionally enforces :func:`admin.security.csrf_protect`. It never mutates the
existing ``/admin`` blueprint and shares its session, CSRF token and template
layout (``admin/base.html``).
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

from admin.security import (
    clean_query,
    csrf_protect,
    current_user,
    get_csrf_token,
    is_authenticated,
    login_required,
)
from commerce import returns as returns_service
from commerce.service import order_service
from utils.logging import logger

admin_returns_bp = Blueprint(
    "admin_returns",
    __name__,
    url_prefix="/admin/returns",
    template_folder="templates",
)

_PAGE_SIZE = 25

RETURN_STATUSES = ("requested", "approved", "rejected", "completed")


# --------------------------------------------------------------------------
# Template context (mirrors the admin blueprint's globals)
# --------------------------------------------------------------------------

@admin_returns_bp.context_processor
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


def _page() -> int:
    try:
        return max(1, int(request.args.get("page", "1")))
    except (TypeError, ValueError):
        return 1


# --------------------------------------------------------------------------
# List
# --------------------------------------------------------------------------

@admin_returns_bp.route("/", methods=["GET"])
@login_required
def returns_list() -> Any:
    """Paginated, status-filterable table of every RMA."""
    status = clean_query(request.args.get("status", ""), 32)
    page = _page()
    offset = (page - 1) * _PAGE_SIZE

    rows = returns_service.list_returns(
        status=status or None, limit=_PAGE_SIZE, offset=offset
    )
    total = returns_service.count_returns(status=status or None)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

    return render_template(
        "admin/returns_list.html",
        returns=rows,
        status=status,
        statuses=RETURN_STATUSES,
        page=page,
        total_pages=total_pages,
        total=total,
        showing_from=(offset + 1) if total else 0,
        showing_to=min(offset + _PAGE_SIZE, total),
    )


# --------------------------------------------------------------------------
# Detail
# --------------------------------------------------------------------------

@admin_returns_bp.route("/<int:rid>", methods=["GET"])
@login_required
def return_detail(rid: int) -> Any:
    """Full RMA view: request details, linked order, and a status form."""
    rma = returns_service.get_return(rid)
    if rma is None:
        flash("Return request not found.", "error")
        return redirect(url_for("admin_returns.returns_list"))

    order = None
    if rma.get("order_id"):
        order = order_service.get_order(order_id=rma["order_id"], include_items=True)

    return render_template(
        "admin/return_detail.html",
        rma=rma,
        order=order,
        statuses=RETURN_STATUSES,
    )


# --------------------------------------------------------------------------
# Status change
# --------------------------------------------------------------------------

@admin_returns_bp.route("/<int:rid>/status", methods=["POST"])
@login_required
@csrf_protect
def return_status(rid: int) -> Any:
    """Advance an RMA's status (form: ``status``, ``refund_amount``, ``resolution``)."""
    detail_url = url_for("admin_returns.return_detail", rid=rid)
    status = clean_query(request.form.get("status", ""), 32)
    resolution = request.form.get("resolution", "") or None
    refund_raw = clean_query(request.form.get("refund_amount", ""), 24)
    refund_amount = None
    if refund_raw:
        try:
            refund_amount = float(refund_raw)
        except (TypeError, ValueError):
            refund_amount = None

    actor = current_user() or "admin"
    try:
        result = returns_service.update_return_status(
            rid, status, refund_amount=refund_amount, resolution=resolution, actor=actor
        )
        if result.get("error"):
            flash(f"Could not update return: {result['error']}.", "error")
        else:
            flash(f"Return marked as {status}.", "success")
            _notify_return(result, status)
    except Exception as exc:  # noqa: BLE001 - never 500 the admin on an action
        logger.error("ADMIN | return status update failed for #%s: %s", rid, exc)
        flash(f"Action failed: {exc}", "error")

    return redirect(detail_url)


def _notify_return(rma: Dict[str, Any], status: str) -> None:
    """Best-effort audit/notification log for an RMA status change (never raises)."""
    try:
        order_service.log_notification(
            kind="return_status",
            wa_number=rma.get("wa_number"),
            audience="customer",
            body=f"Return {rma.get('rma_number')} is now {status}.",
            status="sent",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("ADMIN | return notification skipped for #%s: %s", rma.get("id"), exc)
