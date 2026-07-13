"""
admin/tickets_routes.py
------------------------
Admin "Support Tickets" area for the v7.0 helpdesk: a filterable ticket list, a
detail page showing the full message thread, and CSRF-protected actions to reply,
change status, and (re)assign a ticket.

This is an additive blueprint mounted at ``/admin/tickets``. Every route is
protected by :func:`admin.security.login_required`; state-changing POSTs also
enforce :func:`admin.security.csrf_protect`. It shares the existing admin
session, CSRF token and template layout (``admin/base.html``) and never mutates
the core ``/admin`` blueprint.
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

from admin.rbac import list_users
from admin.security import (
    clean_query,
    csrf_protect,
    current_user,
    get_csrf_token,
    is_authenticated,
    login_required,
)
from commerce import tickets as ticket_service
from commerce.service import order_service
from utils.logging import logger

admin_tickets_bp = Blueprint(
    "admin_tickets",
    __name__,
    url_prefix="/admin/tickets",
    template_folder="templates",
)

_PAGE_SIZE = 25

TICKET_STATUSES = ("open", "pending", "resolved", "closed")


# --------------------------------------------------------------------------
# Template context (mirrors the admin blueprint's globals)
# --------------------------------------------------------------------------

@admin_tickets_bp.context_processor
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

@admin_tickets_bp.route("/", methods=["GET"])
@login_required
def tickets_list() -> Any:
    """Paginated, status-filterable table of every support ticket."""
    status = clean_query(request.args.get("status", ""), 32)
    page = _page()
    offset = (page - 1) * _PAGE_SIZE

    rows = ticket_service.list_tickets(
        status=status or None, limit=_PAGE_SIZE, offset=offset
    )
    total = ticket_service.count_tickets(status=status or None)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

    return render_template(
        "admin/tickets_list.html",
        tickets=rows,
        status=status,
        statuses=TICKET_STATUSES,
        page=page,
        total_pages=total_pages,
        total=total,
        showing_from=(offset + 1) if total else 0,
        showing_to=min(offset + _PAGE_SIZE, total),
    )


# --------------------------------------------------------------------------
# Detail
# --------------------------------------------------------------------------

@admin_tickets_bp.route("/<int:tid>", methods=["GET"])
@login_required
def ticket_detail(tid: int) -> Any:
    """Full ticket view: metadata, message thread, and action forms."""
    ticket = ticket_service.get_ticket(tid)
    if ticket is None:
        flash("Ticket not found.", "error")
        return redirect(url_for("admin_tickets.tickets_list"))

    order = None
    if ticket.get("order_id"):
        order = order_service.get_order(order_id=ticket["order_id"], include_items=True)

    try:
        users = [u["username"] for u in list_users()]
    except Exception as exc:  # noqa: BLE001 - user list is optional
        logger.debug("ADMIN | tickets: could not list users: %s", exc)
        users = []

    return render_template(
        "admin/ticket_detail.html",
        ticket=ticket,
        order=order,
        statuses=TICKET_STATUSES,
        users=users,
    )


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------

@admin_tickets_bp.route("/<int:tid>/reply", methods=["POST"])
@login_required
@csrf_protect
def ticket_reply(tid: int) -> Any:
    """Append an agent reply to a ticket thread."""
    detail_url = url_for("admin_tickets.ticket_detail", tid=tid)
    body = request.form.get("body", "")
    actor = current_user() or "admin"
    if not body.strip():
        flash("Reply cannot be empty.", "error")
        return redirect(detail_url)

    result = ticket_service.add_message(tid, body, author=actor)
    if result.get("error"):
        flash(f"Could not add reply: {result['error']}.", "error")
    else:
        flash("Reply added.", "success")
    return redirect(detail_url)


@admin_tickets_bp.route("/<int:tid>/status", methods=["POST"])
@login_required
@csrf_protect
def ticket_status(tid: int) -> Any:
    """Change a ticket's status."""
    detail_url = url_for("admin_tickets.ticket_detail", tid=tid)
    status = clean_query(request.form.get("status", ""), 32)
    actor = current_user() or "admin"

    result = ticket_service.set_status(tid, status, actor=actor)
    if result.get("error"):
        flash(f"Could not update status: {result['error']}.", "error")
    else:
        flash(f"Ticket marked as {status}.", "success")
    return redirect(detail_url)


@admin_tickets_bp.route("/<int:tid>/assign", methods=["POST"])
@login_required
@csrf_protect
def ticket_assign(tid: int) -> Any:
    """(Re)assign a ticket to a user (blank clears the assignment)."""
    detail_url = url_for("admin_tickets.ticket_detail", tid=tid)
    username = clean_query(request.form.get("username", ""), 128)
    actor = current_user() or "admin"

    result = ticket_service.assign(tid, username, actor=actor)
    if result.get("error"):
        flash(f"Could not assign ticket: {result['error']}.", "error")
    else:
        flash(f"Ticket assigned to {username or 'nobody'}.", "success")
    return redirect(detail_url)
