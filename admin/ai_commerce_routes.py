"""
admin/ai_commerce_routes.py
----------------------------
ME-HAAT Fashion AI Bot v9.0 — the admin "AI Commerce" area.

An additive blueprint mounted at ``/admin/ai`` that lets store operators explore
the v9.0 Advanced AI Commerce features from the dashboard:

    * **Visual search** — upload an image and see the most similar indexed
      products (score-ranked).
    * **Index a product** — add a product image + metadata to the visual index.
    * **Stylist explorer** — "complete the look" and occasion styling guides.

Every route is protected by :func:`admin.security.login_required` and, for
state-changing POSTs, CSRF-validated. The blueprint shares the existing admin
session, CSRF token and template layout (``admin/base.html``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from admin.security import (
    current_user,
    get_csrf_token,
    is_authenticated,
    login_required,
    validate_csrf,
)
from commerce import stylist
from commerce import visual_search
from config import config
from utils.logging import logger

admin_ai_commerce_bp = Blueprint(
    "admin_ai_commerce",
    __name__,
    url_prefix="/admin/ai",
    template_folder="templates",
)

# Reject oversized uploads early (bytes).
_MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB


@admin_ai_commerce_bp.context_processor
def _inject_globals() -> Dict[str, Any]:
    """Expose the CSRF token + current user to this blueprint's templates."""
    return {
        "csrf_token": get_csrf_token() if is_authenticated() else "",
        "admin_user": current_user(),
        "nav_active": "admin_ai_commerce.ai_home",
    }


def _render(
    *,
    results: Optional[List[Dict[str, Any]]] = None,
    look: Optional[Dict[str, Any]] = None,
    occasion: Optional[Dict[str, Any]] = None,
    query_desc: str = "",
) -> Any:
    """Render the AI Commerce console with the current state."""
    return render_template(
        "admin/ai_commerce.html",
        index_size=visual_search.index_size(),
        visual_enabled=bool(config.visual_search_enabled),
        stylist_enabled=bool(config.ai_stylist_enabled),
        embedder=config.visual_embedder,
        results=results,
        look=look,
        occasion=occasion,
        query_desc=query_desc,
    )


def _bad_csrf() -> Any:
    """Flash a CSRF error and redirect back to the console."""
    logger.warning("ADMIN AI | CSRF validation failed for %s", request.path)
    flash("Session expired or invalid form token. Please try again.", "error")
    return redirect(url_for("admin_ai_commerce.ai_home"))


@admin_ai_commerce_bp.route("/", methods=["GET"])
@login_required
def ai_home() -> Any:
    """Render the AI Commerce console (visual search + stylist explorer)."""
    look: Optional[Dict[str, Any]] = None
    occasion: Optional[Dict[str, Any]] = None

    product_type = (request.args.get("product_type") or "").strip()
    color = (request.args.get("color") or "").strip()
    occ = (request.args.get("occasion") or "").strip()

    if product_type or color:
        look = stylist.complete_the_look(
            product_type=product_type or None,
            color=color or None,
            occasion=occ or None,
        )
    if occ:
        occasion = stylist.suggest_for_occasion(occ)

    return _render(look=look, occasion=occasion)


@admin_ai_commerce_bp.route("/visual-search", methods=["POST"])
@login_required
def visual_search_run() -> Any:
    """Run a visual search from an uploaded image and show ranked results."""
    if not validate_csrf():
        return _bad_csrf()

    file = request.files.get("image")
    if file is None or not file.filename:
        flash("Please choose an image to search with.", "warning")
        return redirect(url_for("admin_ai_commerce.ai_home"))

    image_bytes = file.read()
    if not image_bytes:
        flash("The uploaded image was empty.", "warning")
        return redirect(url_for("admin_ai_commerce.ai_home"))
    if len(image_bytes) > _MAX_IMAGE_BYTES:
        flash("That image is too large (max 8 MB).", "error")
        return redirect(url_for("admin_ai_commerce.ai_home"))

    results = visual_search.search_by_image(image_bytes, top_k=5)
    if not results:
        flash("No similar products found. Try indexing more products first.", "warning")
    return _render(results=results, query_desc=file.filename)


@admin_ai_commerce_bp.route("/index", methods=["POST"])
@login_required
def index_product_run() -> Any:
    """Add a product (image + metadata) to the visual index."""
    if not validate_csrf():
        return _bad_csrf()

    retailer_id = (request.form.get("product_retailer_id") or "").strip()
    if not retailer_id:
        flash("A product retailer id is required to index a product.", "warning")
        return redirect(url_for("admin_ai_commerce.ai_home"))

    file = request.files.get("image")
    image_bytes: Optional[bytes] = None
    if file is not None and file.filename:
        image_bytes = file.read()
        if image_bytes and len(image_bytes) > _MAX_IMAGE_BYTES:
            flash("That image is too large (max 8 MB).", "error")
            return redirect(url_for("admin_ai_commerce.ai_home"))

    price = _safe_float(request.form.get("price"))
    row = visual_search.index_product(
        retailer_id,
        image_bytes=image_bytes,
        product_name=(request.form.get("product_name") or "").strip() or None,
        product_type=(request.form.get("product_type") or "").strip() or None,
        color=(request.form.get("color") or "").strip() or None,
        price=price,
        image_url=(request.form.get("image_url") or "").strip() or None,
        url=(request.form.get("url") or "").strip() or None,
    )
    has_features = bool(row.get("features"))
    flash(
        f"Indexed '{retailer_id}'"
        + (" with visual features." if has_features else " (metadata only)."),
        "success" if has_features else "warning",
    )
    return redirect(url_for("admin_ai_commerce.ai_home"))


def _safe_float(value: object) -> Optional[float]:
    """Coerce an optional form value to float, or ``None``."""
    if value in (None, ""):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
