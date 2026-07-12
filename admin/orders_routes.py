"""
admin/orders_routes.py
-----------------------
Admin "Commerce" area for the v6.0 Enterprise Commerce order platform: a
paginated, filterable orders list, a full order-detail page with lifecycle
action forms, CSV/XLSX/PDF export, and an order-analytics dashboard (plus a
JSON API that powers its charts).

This is an additive blueprint mounted at ``/admin/commerce``. Every route is
protected by the existing :func:`admin.security.login_required`; the single
state-changing route additionally enforces :func:`admin.security.csrf_protect`.
It never mutates the existing ``/admin`` blueprint and shares its session, CSRF
token and template layout (``admin/base.html``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from flask import (
    Blueprint,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from admin import exporter
from admin.security import (
    _client_ip,
    clean_query,
    csrf_protect,
    current_user,
    get_csrf_token,
    is_authenticated,
    login_required,
)
from commerce import analytics as commerce_analytics
from commerce.service import order_service
from utils.logging import logger

admin_orders_bp = Blueprint(
    "admin_orders",
    __name__,
    url_prefix="/admin/commerce",
    template_folder="templates",
)

# Fixed page size for the orders list.
_PAGE_SIZE = 25

# Export column layout (flat projection of an order dict).
_EXPORT_COLUMNS: Sequence[str] = (
    "order_number", "customer_name", "wa_number", "products", "quantity",
    "total_amount", "currency", "payment_status", "status", "courier",
    "tracking_number", "city", "state", "created_at",
)


# --------------------------------------------------------------------------
# Template context (mirrors the admin blueprint's globals)
# --------------------------------------------------------------------------

@admin_orders_bp.context_processor
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
# Query-param helpers
# --------------------------------------------------------------------------

def _filters() -> Dict[str, str]:
    """Extract and sanitise the shared list/export filter query params."""
    return {
        "status": clean_query(request.args.get("status", ""), 32),
        "payment_status": clean_query(request.args.get("payment_status", ""), 32),
        "query": clean_query(request.args.get("q", ""), 120),
        "date_from": clean_query(request.args.get("date_from", ""), 10),
        "date_to": clean_query(request.args.get("date_to", ""), 10),
    }


def _page() -> int:
    try:
        return max(1, int(request.args.get("page", "1")))
    except (TypeError, ValueError):
        return 1


def _order_summary_cell(order: Dict[str, Any]) -> Tuple[int, str]:
    """Return ``(total_quantity, "1x A, 2x B …")`` for an order's line items."""
    items = order.get("items") or []
    qty = sum(int(i.get("quantity") or 0) for i in items)
    parts = [
        f"{int(i.get('quantity') or 0)}x {i.get('product_name') or 'Item'}"
        for i in items[:3]
    ]
    summary = ", ".join(parts)
    if len(items) > 3:
        summary += f" +{len(items) - 3} more"
    return qty, summary


# --------------------------------------------------------------------------
# Orders list
# --------------------------------------------------------------------------

@admin_orders_bp.route("/orders", methods=["GET"])
@login_required
def orders_list() -> Any:
    """Paginated, filterable table of every commerce order."""
    filters = _filters()
    page = _page()
    offset = (page - 1) * _PAGE_SIZE

    orders = order_service.list_orders(
        status=filters["status"] or None,
        payment_status=filters["payment_status"] or None,
        query=filters["query"] or None,
        date_from=filters["date_from"] or None,
        date_to=filters["date_to"] or None,
        limit=_PAGE_SIZE,
        offset=offset,
    )
    total = order_service.count_orders(
        status=filters["status"] or None,
        payment_status=filters["payment_status"] or None,
        query=filters["query"] or None,
        date_from=filters["date_from"] or None,
        date_to=filters["date_to"] or None,
    )

    # Decorate each row with a product summary + total quantity.
    rows: List[Dict[str, Any]] = []
    for order in orders:
        qty, summary = _order_summary_cell(order)
        rows.append({**order, "_qty": qty, "_products": summary,
                     "_item_count": len(order.get("items") or [])})

    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    return render_template(
        "admin/orders_list.html",
        orders=rows,
        filters=filters,
        page=page,
        total_pages=total_pages,
        total=total,
        page_size=_PAGE_SIZE,
        showing_from=(offset + 1) if total else 0,
        showing_to=min(offset + _PAGE_SIZE, total),
    )


# --------------------------------------------------------------------------
# Order detail
# --------------------------------------------------------------------------

@admin_orders_bp.route("/orders/<int:order_id>", methods=["GET"])
@login_required
def order_detail(order_id: int) -> Any:
    """Full order view: summary card, line items, tracking timeline, actions."""
    order = order_service.get_order(
        order_id=order_id, include_items=True, include_tracking=True
    )
    if order is None:
        flash("Order not found.", "error")
        return redirect(url_for("admin_orders.orders_list"))
    return render_template("admin/order_detail.html", order=order)


# --------------------------------------------------------------------------
# Order actions (state changes)
# --------------------------------------------------------------------------

@admin_orders_bp.route("/orders/<int:order_id>/action", methods=["POST"])
@login_required
@csrf_protect
def order_action(order_id: int) -> Any:
    """Dispatch a lifecycle action against an order and redirect back.

    The ``action`` form field selects one of: ``confirm``, ``cancel``,
    ``mark_packed``, ``mark_shipped`` (reads ``courier`` + ``tracking_number``),
    ``mark_delivered``, ``refund``, ``generate_invoice`` or
    ``generate_payment_link``. Each is wrapped so a failure flashes an error
    rather than 500-ing.
    """
    action = clean_query(request.form.get("action", ""), 40)
    detail_url = url_for("admin_orders.order_detail", order_id=order_id)

    order = order_service.get_order(order_id=order_id, include_items=True)
    if order is None:
        flash("Order not found.", "error")
        return redirect(url_for("admin_orders.orders_list"))

    actor = current_user() or "admin"
    ip = _client_ip()

    # Map simple status transitions to their target status.
    _STATUS_ACTIONS = {
        "confirm": "confirmed",
        "cancel": "cancelled",
        "mark_packed": "packed",
        "mark_shipped": "shipped",
        "mark_delivered": "delivered",
        "refund": "refunded",
    }

    try:
        if action in _STATUS_ACTIONS:
            new_status = _STATUS_ACTIONS[action]
            courier = clean_query(request.form.get("courier", ""), 128) or None
            tracking_number = clean_query(request.form.get("tracking_number", ""), 128) or None
            updated = order_service.set_status(
                order_id, new_status, actor=actor, ip=ip,
                courier=courier, tracking_number=tracking_number,
            )
            if updated is None:
                flash("Order not found.", "error")
            else:
                flash(f"Order marked as {new_status.replace('_', ' ')}.", "success")
                _notify_status(updated, new_status)

        elif action == "generate_invoice":
            from commerce.invoices import generate_invoice

            result = generate_invoice(order)
            inv_no = result.get("invoice_number", "")
            flash(f"Invoice {inv_no} generated.", "success")

        elif action == "generate_payment_link":
            from payments import generate_payment_link

            link = generate_payment_link(order)
            url = link.get("url", "")
            if url:
                flash(f"Payment link generated: {url}", "success")
            else:
                flash("Payment link generated (no URL returned).", "warning")
            _notify_payment_pending(order, url)

        else:
            flash(f"Unknown action: {action or '(none)'}.", "error")

    except Exception as exc:  # noqa: BLE001 - never 500 the admin on an action
        logger.error("ADMIN | order action %r failed for #%s: %s", action, order_id, exc)
        flash(f"Action failed: {exc}", "error")

    return redirect(detail_url)


def _notify_status(order: Dict[str, Any], status: str) -> None:
    """Best-effort customer status notification (never raises)."""
    try:
        from commerce.notifications import send_status_notification

        send_status_notification(order, status)
    except Exception as exc:  # noqa: BLE001
        logger.debug("ADMIN | status notification skipped for #%s: %s",
                     order.get("id"), exc)


def _notify_payment_pending(order: Dict[str, Any], link: str) -> None:
    """Best-effort payment-pending notification (never raises)."""
    try:
        from commerce.notifications import notify_payment_pending

        notify_payment_pending(order, link)
    except Exception as exc:  # noqa: BLE001
        logger.debug("ADMIN | payment notification skipped for #%s: %s",
                     order.get("id"), exc)


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

def _export_rows(filters: Dict[str, str]) -> List[Dict[str, Any]]:
    """Fetch the current filtered result set flattened for export."""
    orders = order_service.list_orders(
        status=filters["status"] or None,
        payment_status=filters["payment_status"] or None,
        query=filters["query"] or None,
        date_from=filters["date_from"] or None,
        date_to=filters["date_to"] or None,
        limit=5000,
        offset=0,
    )
    rows: List[Dict[str, Any]] = []
    for order in orders:
        qty, summary = _order_summary_cell(order)
        rows.append({
            "order_number": order.get("order_number", ""),
            "customer_name": order.get("customer_name", ""),
            "wa_number": order.get("wa_number", ""),
            "products": summary,
            "quantity": qty,
            "total_amount": order.get("total_amount", 0),
            "currency": order.get("currency", ""),
            "payment_status": order.get("payment_status", ""),
            "status": order.get("status", ""),
            "courier": order.get("courier", "") or "",
            "tracking_number": order.get("tracking_number", "") or "",
            "city": order.get("city", "") or "",
            "state": order.get("state", "") or "",
            "created_at": order.get("created_at", ""),
        })
    return rows


@admin_orders_bp.route("/orders/export", methods=["GET"])
@login_required
def orders_export() -> Any:
    """Export the current filtered orders as CSV, XLSX or PDF (``?format=``)."""
    filters = _filters()
    fmt = clean_query(request.args.get("format", "csv"), 8)
    try:
        rows = _export_rows(filters)
    except Exception as exc:  # noqa: BLE001
        logger.error("ADMIN | orders export build failed: %s", exc)
        return jsonify({"error": "export_failed"}), 500

    try:
        data, mimetype, filename = exporter.export(
            fmt, rows, _EXPORT_COLUMNS, "orders", title="Orders"
        )
    except exporter.ExportUnavailable as exc:
        logger.warning("ADMIN | orders export format unavailable: %s", exc)
        return (
            jsonify({
                "error": "format_unavailable",
                "detail": str(exc),
                "hint": "CSV always works; install openpyxl (xlsx) / reportlab (pdf).",
            }),
            501,
        )

    return Response(
        data,
        mimetype=mimetype,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# --------------------------------------------------------------------------
# Analytics
# --------------------------------------------------------------------------

@admin_orders_bp.route("/analytics", methods=["GET"])
@login_required
def orders_analytics() -> Any:
    """Order-analytics dashboard: KPI tiles + charts fed by ``commerce.analytics``."""
    bundle = commerce_analytics.analytics_bundle()
    return render_template("admin/orders_analytics.html", data=bundle)


@admin_orders_bp.route("/api/analytics", methods=["GET"])
@login_required
def api_orders_analytics() -> Any:
    """JSON bundle of every order-analytics widget (used by the charts)."""
    return jsonify(commerce_analytics.analytics_bundle())
