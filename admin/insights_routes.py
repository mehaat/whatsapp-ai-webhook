"""
admin/insights_routes.py
-------------------------
ME-HAAT Fashion AI Bot v9.0 — the admin "Insights" area.

An additive blueprint mounted at ``/admin/insights`` that surfaces the
:mod:`commerce.recommendations` engine to store operators:

    * **Trending products** — best sellers over a look-back window.
    * **Frequently bought together explorer** — enter a product's retailer id
      and see its cross-sell candidates.
    * **Top movers** — a compact leaderboard reusing the trending signal.

Every route is protected by :func:`admin.security.login_required`. The blueprint
shares the existing admin session, CSRF token and template layout
(``admin/base.html``); it never mutates the core ``/admin`` blueprint.
"""

from __future__ import annotations

from typing import Any, Dict

from flask import Blueprint, render_template, request

from admin.security import (
    current_user,
    get_csrf_token,
    is_authenticated,
    login_required,
)
from commerce import recommendations as reco
from config import config
from utils.logging import logger

admin_insights_bp = Blueprint(
    "admin_insights",
    __name__,
    url_prefix="/admin/insights",
    template_folder="templates",
)


@admin_insights_bp.context_processor
def _inject_globals() -> Dict[str, Any]:
    """Expose the CSRF token + current user to this blueprint's templates."""
    return {
        "csrf_token": get_csrf_token() if is_authenticated() else "",
        "admin_user": current_user(),
        "nav_active": "admin_insights.insights_home",
    }


def _int_arg(name: str, default: int) -> int:
    """Read a positive integer query arg, falling back to ``default``."""
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


@admin_insights_bp.route("/", methods=["GET"])
@login_required
def insights_home() -> Any:
    """Render the insights dashboard (trending, FBT explorer, top movers)."""
    days = _int_arg("days", 30)
    limit = _int_arg("limit", 10)
    retailer_id = (request.args.get("retailer_id", "") or "").strip()

    trending: list = []
    top_movers: list = []
    fbt: list = []
    error = ""

    try:
        trending = reco.trending(limit=limit, days=days)
        top_movers = trending[:5]
        if retailer_id:
            fbt = reco.frequently_bought_together(retailer_id, limit=limit)
    except Exception as exc:  # noqa: BLE001 - the page must always render
        logger.debug("INSIGHTS | data load failed: %r", exc)
        error = "Could not load insights right now."

    return render_template(
        "admin/insights.html",
        trending=trending,
        top_movers=top_movers,
        fbt=fbt,
        retailer_id=retailer_id,
        days=days,
        limit=limit,
        error=error,
        enabled=bool(getattr(config, "recommendations_enabled", True)),
    )
