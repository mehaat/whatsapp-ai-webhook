"""
admin/api_analytics_routes.py
------------------------------
Admin **API usage analytics** for the v9.0 developer portal. Renders a
per-key usage table (issued keys joined with their metered usage over a
trailing window) plus a simple daily-totals bar chart.

This is an additive blueprint mounted at ``/admin/developer`` alongside the
existing :data:`admin.developer_routes.admin_developer_bp` (which owns the
``/keys`` routes). To avoid a route collision the analytics route lives at a
**distinct** path, ``/admin/developer/analytics``.

Every route is protected by :func:`admin.security.login_required` *and*
:func:`admin.rbac.role_required` at the ``admin`` level. It shares the admin
session, CSRF token and template layout (``admin/base.html``) and never mutates
any existing blueprint.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict

from flask import Blueprint, render_template, request

from admin.rbac import role_required
from admin.security import (
    current_user,
    get_csrf_token,
    is_authenticated,
    login_required,
)
from commerce import apikeys
from commerce.api_usage import usage_for, usage_summary
from utils.logging import logger

admin_api_analytics_bp = Blueprint(
    "admin_api_analytics",
    __name__,
    url_prefix="/admin/developer",
    template_folder="templates",
)

#: Default trailing window (days) for the analytics view.
_DEFAULT_WINDOW = 30


# --------------------------------------------------------------------------
# Template context (mirrors the admin blueprint's globals)
# --------------------------------------------------------------------------

@admin_api_analytics_bp.context_processor
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


def _window_arg() -> int:
    """Read the ``days`` query param, clamped to a sane range."""
    try:
        days = int(request.args.get("days", _DEFAULT_WINDOW))
    except (TypeError, ValueError):
        return _DEFAULT_WINDOW
    return max(1, min(days, 365))


# --------------------------------------------------------------------------
# Analytics page
# --------------------------------------------------------------------------

@admin_api_analytics_bp.route("/analytics", methods=["GET"])
@login_required
@role_required("admin")
def api_analytics() -> Any:
    """Render per-key API usage analytics over a trailing window."""
    days = _window_arg()

    # Issued keys (metadata: name / scopes / rate limit / active).
    try:
        keys = apikeys.list_keys()
    except Exception as exc:  # noqa: BLE001 - analytics must never 500
        logger.error("ADMIN | api_analytics list_keys failed: %s", exc)
        keys = []

    # Per-key usage rollup over the window (busiest first).
    summary = usage_summary(days=days)
    usage_by_prefix = {row["prefix"]: row for row in summary}

    # Join key metadata with its usage; keys with zero usage still appear.
    rows = []
    for key in keys:
        prefix = key.get("prefix")
        usage = usage_by_prefix.get(prefix, {})
        rows.append({
            "prefix": prefix,
            "name": key.get("name"),
            "scopes": key.get("scopes") or [],
            "rate_limit_per_min": key.get("rate_limit_per_min"),
            "active": key.get("active"),
            "last_used_at": key.get("last_used_at"),
            "total": usage.get("total", 0),
            "last_endpoint": usage.get("last_endpoint"),
        })
    rows.sort(key=lambda r: r["total"], reverse=True)

    # Usage rows for prefixes that have traffic but no live key record.
    known = {k.get("prefix") for k in keys}
    for row in summary:
        if row["prefix"] not in known:
            rows.append({
                "prefix": row["prefix"],
                "name": row.get("name") or "(revoked / unknown)",
                "scopes": [],
                "rate_limit_per_min": None,
                "active": False,
                "last_used_at": None,
                "total": row.get("total", 0),
                "last_endpoint": row.get("last_endpoint"),
            })

    # Aggregate daily totals across all keys for the bar/spark chart.
    daily_totals: "OrderedDict[str, int]" = OrderedDict()
    for key in keys:
        detail = usage_for(key.get("prefix"), days=days)
        for point in detail.get("daily", []):
            daily_totals[point["day"]] = daily_totals.get(point["day"], 0) + point["count"]
    # Include usage for prefixes without a live key too.
    for row in summary:
        if row["prefix"] not in known:
            detail = usage_for(row["prefix"], days=days)
            for point in detail.get("daily", []):
                daily_totals[point["day"]] = daily_totals.get(point["day"], 0) + point["count"]

    chart = [{"day": d, "count": c} for d, c in sorted(daily_totals.items())]
    grand_total = sum(r["total"] for r in rows)

    return render_template(
        "admin/api_analytics.html",
        rows=rows,
        chart=chart,
        days=days,
        grand_total=grand_total,
        key_count=len(keys),
        nav_active="admin_api_analytics.api_analytics",
    )
