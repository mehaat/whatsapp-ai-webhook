"""
admin/catalog_routes.py
------------------------
Admin "Catalog" area for the v7.0 merchandising surface: product bundles
(create / list / deactivate), every customer's saved wishlist items, and the
working-cart list with an abandoned-cart filter.

This is an additive blueprint mounted at ``/admin/catalog``. Every route is
protected by :func:`admin.security.login_required`; the state-changing POST
routes additionally enforce :func:`admin.security.csrf_protect`. It never
mutates the existing ``/admin`` blueprint and shares its session, CSRF token and
template layout (``admin/base.html``).
"""

from __future__ import annotations

from typing import Any, Dict, List

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
from commerce import bundles as bundles_service
from commerce import carts as carts_service
from commerce import wishlist as wishlist_service
from utils.logging import logger

admin_catalog_bp = Blueprint(
    "admin_catalog",
    __name__,
    url_prefix="/admin/catalog",
    template_folder="templates",
)

_PAGE_SIZE = 50


# --------------------------------------------------------------------------
# Template context (mirrors the admin blueprint's globals)
# --------------------------------------------------------------------------

@admin_catalog_bp.context_processor
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
# Bundles
# --------------------------------------------------------------------------

@admin_catalog_bp.route("/bundles", methods=["GET"])
@login_required
def bundles_list() -> Any:
    """Table of every bundle (active + inactive)."""
    rows = bundles_service.list_bundles()
    return render_template("admin/bundles_list.html", bundles=rows)


@admin_catalog_bp.route("/bundles/new", methods=["GET", "POST"])
@login_required
@csrf_protect
def bundle_new() -> Any:
    """Create a new bundle from a simple form.

    The ``items`` field is entered as one ``retailer_id:qty`` pair per line (qty
    optional, defaults to 1).
    """
    if request.method == "GET":
        # The create form lives on the bundles list page; reuse that template.
        return render_template(
            "admin/bundles_list.html", bundles=bundles_service.list_bundles(), show_new=True
        )

    name = clean_query(request.form.get("name", ""), 255)
    sku = clean_query(request.form.get("sku", ""), 64) or None
    currency = clean_query(request.form.get("currency", "INR"), 8) or "INR"
    price_raw = clean_query(request.form.get("price", ""), 24)
    try:
        price = float(price_raw) if price_raw else 0.0
    except (TypeError, ValueError):
        price = 0.0

    items = _parse_items(request.form.get("items", ""))

    if not name:
        flash("A bundle name is required.", "error")
        return redirect(url_for("admin_catalog.bundle_new"))

    result = bundles_service.create_bundle(name, price, items, sku=sku, currency=currency)
    if result.get("error"):
        flash(f"Could not create bundle: {result['error']}.", "error")
        return redirect(url_for("admin_catalog.bundle_new"))

    flash(f"Bundle {name!r} created.", "success")
    return redirect(url_for("admin_catalog.bundles_list"))


@admin_catalog_bp.route("/bundles/<int:bid>/deactivate", methods=["POST"])
@login_required
@csrf_protect
def bundle_deactivate(bid: int) -> Any:
    """Soft-disable a bundle."""
    result = bundles_service.deactivate_bundle(bid)
    if result.get("error"):
        flash(f"Could not deactivate bundle: {result['error']}.", "error")
    else:
        flash("Bundle deactivated.", "success")
    return redirect(url_for("admin_catalog.bundles_list"))


def _parse_items(raw: str) -> List[Dict[str, Any]]:
    """Parse a textarea of ``retailer_id:qty`` lines into bundle item dicts."""
    items: List[Dict[str, Any]] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            rid, _, qty = line.partition(":")
        else:
            rid, qty = line, "1"
        rid = rid.strip()
        if not rid:
            continue
        try:
            qty_int = int(qty.strip() or "1")
        except (TypeError, ValueError):
            qty_int = 1
        items.append({"retailer_id": rid, "qty": max(1, qty_int)})
    return items


# --------------------------------------------------------------------------
# Wishlist
# --------------------------------------------------------------------------

@admin_catalog_bp.route("/wishlist", methods=["GET"])
@login_required
def wishlist_list() -> Any:
    """Every customer's saved wishlist items."""
    rows = wishlist_service.list_all(limit=_PAGE_SIZE * 4, offset=0)
    total = wishlist_service.count_all()
    return render_template("admin/wishlist_list.html", items=rows, total=total)


# --------------------------------------------------------------------------
# Carts
# --------------------------------------------------------------------------

@admin_catalog_bp.route("/carts", methods=["GET"])
@login_required
def carts_list() -> Any:
    """Working carts, with an ``?abandoned=1`` filter for at-risk carts."""
    abandoned = request.args.get("abandoned") == "1"
    status = clean_query(request.args.get("status", ""), 16)

    if abandoned:
        rows = carts_service.find_abandoned()
    else:
        rows = carts_service.list_carts(status=status or None, limit=_PAGE_SIZE, offset=0)

    return render_template(
        "admin/carts_list.html",
        carts=rows,
        abandoned=abandoned,
        status=status,
    )
