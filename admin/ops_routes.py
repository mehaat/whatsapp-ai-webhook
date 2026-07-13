"""
admin/ops_routes.py
--------------------
Admin "Ops" area for the v7.0 operations dashboards:

* **Payments dashboard** (``/admin/ops/payments``) — a table of recent payment
  rows plus stat tiles (total collected, total pending, and a per-provider
  breakdown), backed by a JSON endpoint (``/admin/ops/payments/data``) for live
  refresh.
* **Employee dashboard** (``/admin/ops/employees``) — every dashboard user with
  a per-user action count (from the audit log) and last-login timestamp (from
  the login-event log).

Mounted at ``/admin/ops``; every route requires an authenticated session
(:func:`admin.security.login_required`). The data functions
(:func:`payments_dashboard_data`, :func:`employees_data`) are plain, importable,
never-raising helpers returning JSON-friendly ``dict`` / ``list`` structures.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from flask import (
    Blueprint,
    jsonify,
    render_template,
    request,
)

from admin.security import (
    current_user,
    get_csrf_token,
    is_authenticated,
    login_required,
)
from utils.logging import logger

admin_ops_bp = Blueprint(
    "admin_ops",
    __name__,
    url_prefix="/admin/ops",
    template_folder="templates",
)

_PAYMENT_LIMIT = 200
# Payment statuses that count as "collected" vs "pending" in the tiles.
_PAID_STATES = ("paid",)
_PENDING_STATES = ("pending",)


# --------------------------------------------------------------------------
# Template context (mirrors the admin blueprint's globals)
# --------------------------------------------------------------------------

@admin_ops_bp.context_processor
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


def _f(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if isinstance(dt, datetime) else None


# --------------------------------------------------------------------------
# Payments dashboard data
# --------------------------------------------------------------------------

def payments_dashboard_data(limit: int = _PAYMENT_LIMIT) -> Dict[str, Any]:
    """Assemble the payments dashboard payload. Never raises.

    Returns a dict with:
        * ``payments``: recent payment rows (newest-first);
        * ``total_collected`` / ``total_pending``: summed amounts;
        * ``count`` / ``paid_count`` / ``pending_count``;
        * ``by_provider``: list of ``{provider, count, collected, pending}``;
        * ``currency``: a best-effort display currency.
    """
    data: Dict[str, Any] = {
        "payments": [],
        "total_collected": 0.0,
        "total_pending": 0.0,
        "count": 0,
        "paid_count": 0,
        "pending_count": 0,
        "by_provider": [],
        "currency": "INR",
    }
    try:
        from database.db import session_scope
        from database.models import Order, Payment

        with session_scope() as session:
            payments = (
                session.query(Payment)
                .order_by(Payment.created_at.desc(), Payment.id.desc())
                .limit(limit)
                .all()
            )
            # Resolve order numbers for display in one pass.
            order_ids = {p.order_id for p in payments if p.order_id is not None}
            numbers: Dict[int, str] = {}
            if order_ids:
                for o in session.query(Order).filter(Order.id.in_(order_ids)).all():
                    numbers[o.id] = o.order_number

            providers: Dict[str, Dict[str, Any]] = {}
            rows: List[Dict[str, Any]] = []
            for p in payments:
                amount = _f(p.amount)
                status = (p.status or "").lower()
                if p.currency:
                    data["currency"] = p.currency
                rows.append({
                    "id": p.id,
                    "order_id": p.order_id,
                    "order_number": numbers.get(p.order_id),
                    "provider": p.provider,
                    "provider_payment_id": p.provider_payment_id,
                    "amount": amount,
                    "currency": p.currency,
                    "status": status,
                    "created_at": _iso(p.created_at),
                })
                prov = providers.setdefault(
                    p.provider or "unknown",
                    {"provider": p.provider or "unknown", "count": 0,
                     "collected": 0.0, "pending": 0.0},
                )
                prov["count"] += 1
                if status in _PAID_STATES:
                    data["total_collected"] += amount
                    data["paid_count"] += 1
                    prov["collected"] += amount
                elif status in _PENDING_STATES:
                    data["total_pending"] += amount
                    data["pending_count"] += 1
                    prov["pending"] += amount

            data["payments"] = rows
            data["count"] = len(rows)
            data["by_provider"] = sorted(
                providers.values(), key=lambda x: x["collected"], reverse=True
            )
        return data
    except Exception as exc:  # noqa: BLE001
        logger.error("ADMIN | payments_dashboard_data failed: %s", exc)
        return data


# --------------------------------------------------------------------------
# Employee dashboard data
# --------------------------------------------------------------------------

def employees_data() -> List[Dict[str, Any]]:
    """Return dashboard users enriched with action count + last login. Never raises."""
    try:
        from sqlalchemy import func

        from admin.rbac import list_users
        from database.db import session_scope
        from database.models import AuditLog, LoginEvent

        users = list_users()

        action_counts: Dict[str, int] = {}
        last_logins: Dict[str, str] = {}
        with session_scope() as session:
            for actor, count in (
                session.query(AuditLog.actor, func.count(AuditLog.id))
                .group_by(AuditLog.actor)
                .all()
            ):
                if actor:
                    action_counts[actor] = int(count)
            for username, last in (
                session.query(LoginEvent.username, func.max(LoginEvent.created_at))
                .filter(LoginEvent.success.is_(True))
                .group_by(LoginEvent.username)
                .all()
            ):
                if username:
                    last_logins[username] = _iso(last)

        enriched: List[Dict[str, Any]] = []
        for u in users:
            uname = u.get("username")
            enriched.append({
                **u,
                "action_count": action_counts.get(uname, 0),
                "last_login_event": last_logins.get(uname) or u.get("last_login_at"),
            })
        return enriched
    except Exception as exc:  # noqa: BLE001
        logger.error("ADMIN | employees_data failed: %s", exc)
        return []


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@admin_ops_bp.route("/payments", methods=["GET"])
@login_required
def payments_dashboard() -> Any:
    """Render the payments dashboard (tiles + recent payments table)."""
    return render_template("admin/payments_dashboard.html", data=payments_dashboard_data())


@admin_ops_bp.route("/payments/data", methods=["GET"])
@login_required
def payments_data() -> Any:
    """JSON payload powering the payments dashboard (live refresh)."""
    return jsonify(payments_dashboard_data())


@admin_ops_bp.route("/employees", methods=["GET"])
@login_required
def employees() -> Any:
    """Render the employee dashboard (users + activity + last login)."""
    return render_template("admin/employees.html", employees=employees_data())
