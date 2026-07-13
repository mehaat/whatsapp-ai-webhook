"""
admin/crm_routes.py
--------------------
Admin "Customer CRM" area for the v6.1 CRM layer: a paginated, searchable and
filterable customer table plus a full customer profile page (lifetime value,
order history, notes and a tags/segment editor).

This is an additive blueprint mounted at ``/admin/commerce/crm``. Every route
is protected by :func:`admin.security.login_required`; the state-changing POST
routes additionally enforce :func:`admin.security.csrf_protect`. It never
mutates the existing ``/admin`` blueprint and shares its session, CSRF token
and template layout (``admin/base.html``).
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
from commerce import crm
from utils.logging import logger

admin_crm_bp = Blueprint(
    "admin_crm",
    __name__,
    url_prefix="/admin/commerce/crm",
    template_folder="templates",
)

# Fixed page size for the customer list.
_PAGE_SIZE = 25


# --------------------------------------------------------------------------
# Template context (mirrors the admin blueprint's globals)
# --------------------------------------------------------------------------

@admin_crm_bp.context_processor
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
    """Extract and sanitise the shared list filter query params."""
    return {
        "query": clean_query(request.args.get("q", ""), 120),
        "segment": clean_query(request.args.get("segment", ""), 32),
        "tag": clean_query(request.args.get("tag", ""), 64),
    }


def _page() -> int:
    try:
        return max(1, int(request.args.get("page", "1")))
    except (TypeError, ValueError):
        return 1


# --------------------------------------------------------------------------
# Customer list
# --------------------------------------------------------------------------

@admin_crm_bp.route("/", methods=["GET"])
@login_required
def crm_list() -> Any:
    """Paginated, searchable and filterable table of every customer."""
    filters = _filters()
    page = _page()
    offset = (page - 1) * _PAGE_SIZE

    customers = crm.list_customers(
        query=filters["query"] or None,
        segment=filters["segment"] or None,
        tag=filters["tag"] or None,
        limit=_PAGE_SIZE,
        offset=offset,
    )
    total = crm.count_customers(
        query=filters["query"] or None,
        segment=filters["segment"] or None,
        tag=filters["tag"] or None,
    )

    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    return render_template(
        "admin/crm_list.html",
        customers=customers,
        filters=filters,
        page=page,
        total_pages=total_pages,
        total=total,
        page_size=_PAGE_SIZE,
        showing_from=(offset + 1) if total else 0,
        showing_to=min(offset + _PAGE_SIZE, total),
    )


# --------------------------------------------------------------------------
# Customer detail
# --------------------------------------------------------------------------

@admin_crm_bp.route("/<wa_number>", methods=["GET"])
@login_required
def crm_detail(wa_number: str) -> Any:
    """Full customer profile: metrics, tags/segment editor, orders and notes."""
    customer = crm.get_customer(wa_number)
    if customer is None:
        flash("Customer not found.", "error")
        return redirect(url_for("admin_crm.crm_list"))
    return render_template("admin/crm_detail.html", customer=customer, wa_number=wa_number)


# --------------------------------------------------------------------------
# Customer actions (state changes)
# --------------------------------------------------------------------------

@admin_crm_bp.route("/<wa_number>/note", methods=["POST"])
@login_required
@csrf_protect
def crm_add_note(wa_number: str) -> Any:
    """Add a free-text note to a customer and redirect back to their profile."""
    note = clean_query(request.form.get("note", ""), 2000)
    detail_url = url_for("admin_crm.crm_detail", wa_number=wa_number)
    if not note:
        flash("Note cannot be empty.", "warning")
        return redirect(detail_url)
    try:
        crm.add_note(wa_number, note, author=current_user() or "admin")
        flash("Note added.", "success")
    except Exception as exc:  # noqa: BLE001 - never 500 the admin on an action
        logger.error("ADMIN | add note failed for %s: %s", wa_number, exc)
        flash(f"Could not add note: {exc}", "error")
    return redirect(detail_url)


@admin_crm_bp.route("/<wa_number>/tags", methods=["POST"])
@login_required
@csrf_protect
def crm_set_tags(wa_number: str) -> Any:
    """Replace a customer's tags from a comma-separated input and redirect."""
    raw = clean_query(request.form.get("tags", ""), 500)
    tags = [t.strip() for t in raw.split(",") if t.strip()]
    detail_url = url_for("admin_crm.crm_detail", wa_number=wa_number)
    try:
        crm.set_tags(wa_number, tags)
        flash("Tags updated.", "success")
    except Exception as exc:  # noqa: BLE001
        logger.error("ADMIN | set tags failed for %s: %s", wa_number, exc)
        flash(f"Could not update tags: {exc}", "error")
    return redirect(detail_url)


@admin_crm_bp.route("/<wa_number>/segment", methods=["POST"])
@login_required
@csrf_protect
def crm_set_segment(wa_number: str) -> Any:
    """Set a customer's segment and redirect back to their profile."""
    segment = clean_query(request.form.get("segment", ""), 32)
    detail_url = url_for("admin_crm.crm_detail", wa_number=wa_number)
    try:
        crm.set_segment(wa_number, segment)
        flash("Segment updated.", "success")
    except Exception as exc:  # noqa: BLE001
        logger.error("ADMIN | set segment failed for %s: %s", wa_number, exc)
        flash(f"Could not update segment: {exc}", "error")
    return redirect(detail_url)
