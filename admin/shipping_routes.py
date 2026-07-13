"""
admin/shipping_routes.py
-------------------------
Admin "Fulfilment & Shipping" area for the v7.0 shipping platform: a shipments
list, a shipment-detail page, and the state-changing actions that create a
shipment for an order, schedule a courier pickup, and refresh live tracking.

This is an additive blueprint mounted at ``/admin/shipping``. Every route is
protected by :func:`admin.security.login_required`; the state-changing routes
(create/pickup/track) additionally enforce :func:`admin.security.csrf_protect`,
and create/pickup require at least the ``staff`` role. It never mutates the
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

from admin.rbac import role_required
from admin.security import (
    clean_query,
    csrf_protect,
    current_user,
    get_csrf_token,
    is_authenticated,
    login_required,
)
from utils.logging import logger

admin_shipping_bp = Blueprint(
    "admin_shipping",
    __name__,
    url_prefix="/admin/shipping",
    template_folder="templates",
)

# Fixed page size for the shipments list.
_PAGE_SIZE = 25


# --------------------------------------------------------------------------
# Template context (mirrors the admin blueprint's globals)
# --------------------------------------------------------------------------

@admin_shipping_bp.context_processor
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
# Shipments list
# --------------------------------------------------------------------------

@admin_shipping_bp.route("/", methods=["GET"])
@login_required
def shipments_list() -> Any:
    """Paginated table of every shipment, optionally filtered by status."""
    from shipping import service as shipping_service
    from shipping.factory import available_providers

    status = clean_query(request.args.get("status", ""), 32)
    page = _page()
    offset = (page - 1) * _PAGE_SIZE

    shipments = shipping_service.list_shipments(
        status=status or None, limit=_PAGE_SIZE + 1, offset=offset,
    )
    has_next = len(shipments) > _PAGE_SIZE
    shipments = shipments[:_PAGE_SIZE]

    return render_template(
        "admin/shipments_list.html",
        shipments=shipments,
        status=status,
        page=page,
        has_next=has_next,
        providers=available_providers(),
        default_provider=_default_provider(),
    )


def _default_provider() -> str:
    try:
        from config import config

        return getattr(config, "shipping_provider", "manual") or "manual"
    except Exception:  # noqa: BLE001
        return "manual"


# --------------------------------------------------------------------------
# Shipment detail
# --------------------------------------------------------------------------

@admin_shipping_bp.route("/<int:sid>", methods=["GET"])
@login_required
def shipment_detail(sid: int) -> Any:
    """Full shipment view: summary, courier, tracking refresh + pickup actions."""
    from shipping import service as shipping_service

    shipment = shipping_service.get_shipment(sid)
    if shipment is None:
        flash("Shipment not found.", "error")
        return redirect(url_for("admin_shipping.shipments_list"))

    order = None
    try:
        from commerce.service import order_service

        order = order_service.get_order(
            order_id=shipment["order_id"], include_items=True,
        )
    except Exception as exc:  # noqa: BLE001 - detail must render without the order
        logger.debug("ADMIN | shipment order load skipped for #%s: %s", sid, exc)

    return render_template(
        "admin/shipment_detail.html",
        shipment=shipment,
        order=order,
        providers=_providers(),
    )


def _providers() -> list:
    try:
        from shipping.factory import available_providers

        return available_providers()
    except Exception:  # noqa: BLE001
        return ["manual"]


# --------------------------------------------------------------------------
# Actions (state changes) — CSRF + staff role
# --------------------------------------------------------------------------

@admin_shipping_bp.route("/order/<int:order_id>/create", methods=["POST"])
@login_required
@role_required("staff")
@csrf_protect
def create_shipment(order_id: int) -> Any:
    """Create a shipment for an order and redirect to its shipment detail."""
    from shipping import service as shipping_service

    provider_name = clean_query(request.form.get("provider", ""), 32) or None
    result = shipping_service.create_shipment_for_order(order_id, provider_name)

    if result.get("ok") and result.get("shipment"):
        shipment = result["shipment"]
        flash(
            f"Shipment created (AWB {shipment.get('awb') or 'n/a'}); "
            f"order marked shipped.",
            "success",
        )
        return redirect(url_for("admin_shipping.shipment_detail", sid=shipment["id"]))

    flash(f"Could not create shipment: {result.get('error') or 'unknown error'}.",
          "error")
    return redirect(url_for("admin_shipping.shipments_list"))


@admin_shipping_bp.route("/<int:sid>/pickup", methods=["POST"])
@login_required
@role_required("staff")
@csrf_protect
def schedule_pickup(sid: int) -> Any:
    """Schedule a courier pickup for a shipment and redirect back."""
    from shipping import service as shipping_service

    result = shipping_service.schedule_pickup(sid)
    if result.get("ok"):
        flash("Pickup scheduled with the courier.", "success")
    else:
        flash(f"Pickup could not be scheduled: {result.get('error') or 'unknown'}.",
              "warning")
    return redirect(url_for("admin_shipping.shipment_detail", sid=sid))


@admin_shipping_bp.route("/<int:sid>/track", methods=["POST"])
@login_required
@csrf_protect
def refresh_tracking(sid: int) -> Any:
    """Refresh live tracking for a shipment and redirect back."""
    from shipping import service as shipping_service

    shipment = shipping_service.get_shipment(sid)
    if shipment is None:
        flash("Shipment not found.", "error")
        return redirect(url_for("admin_shipping.shipments_list"))

    result = shipping_service.track_shipment(awb=shipment.get("awb"))
    if result.get("ok"):
        flash(f"Tracking refreshed: {result.get('status') or 'unknown'}.", "success")
    else:
        flash(f"Tracking refresh failed: {result.get('error') or 'unknown'}.",
              "warning")
    return redirect(url_for("admin_shipping.shipment_detail", sid=sid))
