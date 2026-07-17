"""
admin/promos_routes.py
------------------------
Admin "Promotions" area for the v7.0 Coupon / Discount engine and Gift-Card
ledger. Provides a coupon list, create / edit / deactivate flows, a gift-card
list and a gift-card issue action.

This is an additive blueprint mounted at ``/admin/promos``. It shares the
existing admin session, CSRF token and template layout (``admin/base.html``)
and never mutates the core ``/admin`` blueprint. Every route requires an
authenticated session (:func:`admin.security.login_required`); create / edit /
deactivate / issue additionally require at least the ``manager`` role
(:func:`admin.rbac.role_required`) and CSRF validation on POST
(:func:`admin.security.csrf_protect`).
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
from commerce import discounts
from utils.logging import logger

admin_promos_bp = Blueprint(
    "admin_promos",
    __name__,
    url_prefix="/admin/promos",
    template_folder="templates",
)


# --------------------------------------------------------------------------
# Template context (mirrors the admin blueprint's globals)
# --------------------------------------------------------------------------

@admin_promos_bp.context_processor
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
# Coupons
# --------------------------------------------------------------------------

@admin_promos_bp.route("/coupons", methods=["GET"])
@login_required
def coupons_list() -> Any:
    """Render the table of coupons (optionally filtered by active state)."""
    active_arg = request.args.get("active", "")
    active = None
    if active_arg in {"1", "true", "active"}:
        active = True
    elif active_arg in {"0", "false", "inactive"}:
        active = False
    return render_template(
        "admin/promos_list.html",
        coupons=discounts.list_coupons(active=active),
        active_filter=active_arg,
        nav_active="admin_promos.coupons_list",
    )


@admin_promos_bp.route("/coupons/new", methods=["GET", "POST"])
@login_required
@role_required("manager")
@csrf_protect
def coupon_new() -> Any:
    """Show and process the create-coupon form."""
    if request.method == "GET":
        return render_template(
            "admin/promo_form.html",
            mode="create",
            coupon=None,
            nav_active="admin_promos.coupons_list",
        )

    fields = _coupon_form_fields()
    result = discounts.create_coupon(**fields)
    if "error" in result:
        flash(result["error"], "error")
        return render_template(
            "admin/promo_form.html",
            mode="create",
            coupon=fields,
            nav_active="admin_promos.coupons_list",
        )

    flash(f"Coupon {result['code']} created.", "success")
    return redirect(url_for("admin_promos.coupons_list"))


@admin_promos_bp.route("/coupons/<int:cid>/edit", methods=["GET", "POST"])
@login_required
@role_required("manager")
@csrf_protect
def coupon_edit(cid: int) -> Any:
    """Edit an existing coupon."""
    coupon = discounts.get_coupon(cid)
    if coupon is None:
        flash("Coupon not found.", "error")
        return redirect(url_for("admin_promos.coupons_list"))

    if request.method == "GET":
        return render_template(
            "admin/promo_form.html",
            mode="edit",
            coupon=coupon,
            nav_active="admin_promos.coupons_list",
        )

    fields = _coupon_form_fields()
    result = discounts.update_coupon(cid, **fields)
    if "error" in result:
        flash(result["error"], "error")
        return render_template(
            "admin/promo_form.html",
            mode="edit",
            coupon={**coupon, **fields, "id": cid},
            nav_active="admin_promos.coupons_list",
        )

    flash(f"Coupon {result['code']} updated.", "success")
    return redirect(url_for("admin_promos.coupons_list"))


@admin_promos_bp.route("/coupons/<int:cid>/deactivate", methods=["POST"])
@login_required
@role_required("manager")
@csrf_protect
def coupon_deactivate(cid: int) -> Any:
    """Deactivate a coupon."""
    result = discounts.deactivate_coupon(cid)
    flash(result.get("message", "Done."), "success" if result.get("ok") else "error")
    return redirect(url_for("admin_promos.coupons_list"))


# --------------------------------------------------------------------------
# Gift cards
# --------------------------------------------------------------------------

@admin_promos_bp.route("/giftcards", methods=["GET"])
@login_required
def giftcards_list() -> Any:
    """Render the table of gift cards."""
    return render_template(
        "admin/giftcards_list.html",
        gift_cards=discounts.list_gift_cards(),
        nav_active="admin_promos.giftcards_list",
    )


@admin_promos_bp.route("/giftcards/issue", methods=["POST"])
@login_required
@role_required("manager")
@csrf_protect
def giftcard_issue() -> Any:
    """Issue a new gift card from the amount form."""
    amount = _to_float(request.form.get("amount", ""))
    currency = clean_query(request.form.get("currency", "INR"), 8) or "INR"
    issued_to = clean_query(request.form.get("issued_to", ""), 32) or None
    code = clean_query(request.form.get("code", ""), 64) or None

    if amount is None or amount <= 0:
        flash("Enter a positive gift-card amount.", "error")
        return redirect(url_for("admin_promos.giftcards_list"))

    result = discounts.issue_gift_card(
        amount, currency=currency, issued_to=issued_to, code=code
    )
    if "error" in result:
        flash(result["error"], "error")
    else:
        flash(
            f"Gift card {result['code']} issued for "
            f"{result['currency']} {result['initial_balance']:.2f}.",
            "success",
        )
    return redirect(url_for("admin_promos.giftcards_list"))


# --------------------------------------------------------------------------
# Form helpers
# --------------------------------------------------------------------------

def _coupon_form_fields() -> Dict[str, Any]:
    """Extract coupon fields from the submitted form."""
    form = request.form
    return {
        "code": clean_query(form.get("code", ""), 64),
        "kind": clean_query(form.get("kind", "percent"), 16),
        "value": _to_float(form.get("value", "")) or 0,
        "min_order": _to_float(form.get("min_order", "")) or 0,
        "max_discount": _to_float(form.get("max_discount", "")),
        "usage_limit": _to_int(form.get("usage_limit", "")),
        "per_customer_limit": _to_int(form.get("per_customer_limit", "")),
        "starts_at": _to_dt(form.get("starts_at", "")),
        "expires_at": _to_dt(form.get("expires_at", "")),
        "active": form.get("active") == "on",
        "description": clean_query(form.get("description", ""), 500) or None,
    }


def _to_float(value: str) -> Any:
    """Parse a form value to ``float`` or ``None``."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _to_int(value: str) -> Any:
    """Parse a form value to ``int`` or ``None``."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _to_dt(value: str):
    """Parse an ``<input type=datetime-local>`` value to an aware UTC datetime."""
    value = (value or "").strip()
    if not value:
        return None
    from datetime import datetime, timezone

    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    logger.debug("ADMIN | promos: unparseable datetime %r", value)
    return None
