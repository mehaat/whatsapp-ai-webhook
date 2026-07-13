"""
admin/routes.py
----------------
All Admin Dashboard HTTP routes: the login flow, the HTML pages, and the JSON
APIs that power live (no-reload) updates and exports. Everything is namespaced
under ``/admin`` via the blueprint and protected by :func:`login_required`
(except the login/health routes).

The blueprint is entirely additive: it shares the Flask app but registers no
handlers that collide with the existing ``/``, ``/health*``, ``/shopify/*`` or
``/webhook`` routes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from flask import (
    Blueprint,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from admin import analytics, exporter, shopify_lookup
from admin import tracker
from admin.db import get_conn
from admin.config import admin_config
from admin.security import (
    csrf_protect,
    current_user,
    end_session,
    get_csrf_token,
    is_authenticated,
    login_attempt_allowed,
    login_required,
    clean_query,
    start_session,
    verify_password,
    verify_username,
)
from utils.logging import logger

admin_bp = Blueprint(
    "admin",
    __name__,
    url_prefix="/admin",
    template_folder="templates",
    static_folder="static",
    static_url_path="/admin/static",
)


# --------------------------------------------------------------------------
# Template context (available to every admin template)
# --------------------------------------------------------------------------

@admin_bp.context_processor
def _inject_globals() -> Dict[str, Any]:
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
# Authentication
# --------------------------------------------------------------------------

@admin_bp.route("/login", methods=["GET", "POST"])
def login() -> Any:
    """Render and process the admin login form."""
    if is_authenticated():
        return redirect(url_for("admin.dashboard"))

    if request.method == "GET":
        return render_template("admin/login.html", error=None)

    if not admin_config.credentials_configured:
        return render_template(
            "admin/login.html",
            error="Admin credentials are not configured. Set ADMIN_USERNAME and "
            "ADMIN_PASSWORD (or ADMIN_PASSWORD_HASH) in the environment.",
        ), 503

    if not login_attempt_allowed():
        logger.warning("ADMIN | Login throttled")
        return render_template(
            "admin/login.html",
            error="Too many login attempts. Please wait a few minutes and try again.",
        ), 429

    username = clean_query(request.form.get("username", ""), 80)
    password = request.form.get("password", "")

    # v7.0 security helpers (IP allowlist, login history, optional 2FA).
    client_ip = (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or ""
    )
    user_agent = request.headers.get("User-Agent", "")[:255]
    try:
        from admin import security_ext
        from config import config as _cfg
    except Exception:  # noqa: BLE001 - security module optional
        security_ext, _cfg = None, None

    def _record(success: bool) -> None:
        if security_ext is not None:
            security_ext.record_login_event(
                username, ip=client_ip, user_agent=user_agent, success=success
            )

    # IP allowlist gate (empty allowlist => allow all).
    if security_ext is not None and not security_ext.ip_allowed(client_ip):
        logger.warning("ADMIN | Login blocked by IP allowlist from %s", client_ip)
        _record(False)
        return render_template(
            "admin/login.html", error="Access from your network is not permitted."
        ), 403

    # The environment ADMIN_USERNAME/ADMIN_PASSWORD is a built-in "owner"
    # superuser. Additional named users (v6.1 RBAC) are checked against the
    # admin_users table and carry their own role.
    env_ok = verify_username(username) and verify_password(password)
    db_user = None
    if not env_ok:
        try:
            from admin.rbac import verify_login

            db_user = verify_login(username, password)
        except Exception as exc:  # noqa: BLE001 - RBAC must never break core login
            logger.error("ADMIN | RBAC login check failed: %s", exc)

    if env_ok or db_user:
        # Optional TOTP second factor for DB users (env owner is exempt).
        if db_user and security_ext is not None and _cfg is not None and _cfg.admin_2fa_enabled:
            info = security_ext.user_totp(db_user["id"]) or {}
            if info.get("enabled"):
                code = request.form.get("totp_code", "").strip()
                secret = _fetch_totp_secret(db_user["id"])
                if not code or not security_ext.verify_totp(secret, code):
                    _record(False)
                    return render_template(
                        "admin/login.html",
                        error="Enter your 6-digit authenticator code.",
                        need_2fa=True,
                        prefill_username=username,
                    ), 401

        start_session(username)
        session["admin_role"] = "owner" if env_ok else db_user["role"]
        tracker.record_login(username)
        _record(True)
        logger.info("ADMIN | Login success for %s (role=%s)", username, session["admin_role"])
        nxt = request.args.get("next", "")
        if nxt.startswith("/admin"):
            return redirect(nxt)
        return redirect(url_for("admin.dashboard"))

    _record(False)
    logger.warning("ADMIN | Login failed for user=%r", username)
    return render_template("admin/login.html", error="Invalid username or password."), 401


def _fetch_totp_secret(user_id: int):
    """Return an admin user's stored TOTP secret (or None)."""
    try:
        from database.db import session_scope
        from database.models import AdminUser

        with session_scope() as session:
            user = session.get(AdminUser, user_id)
            return user.totp_secret if user else None
    except Exception:  # noqa: BLE001
        return None


@admin_bp.route("/logout", methods=["GET", "POST"])
def logout() -> Any:
    """Log out and return to the login screen."""
    end_session()
    return redirect(url_for("admin.login"))


# --------------------------------------------------------------------------
# HTML pages
# --------------------------------------------------------------------------

@admin_bp.route("/", methods=["GET"])
@admin_bp.route("/dashboard", methods=["GET"])
@login_required
def dashboard() -> Any:
    """Dashboard home with headline cards and charts."""
    return render_template(
        "admin/dashboard.html",
        stats=analytics.dashboard_stats(),
        daily=analytics.daily_messages(14),
        top_customers=analytics.top_customers(5),
        popular_products=analytics.popular_products(5),
    )


@admin_bp.route("/inbox", methods=["GET"])
@admin_bp.route("/messages", methods=["GET"])
@login_required
def inbox() -> Any:
    """Live WhatsApp inbox (messenger-style)."""
    return render_template("admin/inbox.html", conversations=analytics.inbox())


@admin_bp.route("/chat/<wa_number>", methods=["GET"])
@login_required
def chat(wa_number: str) -> Any:
    """Full conversation timeline for one customer."""
    tracker.mark_read(wa_number)
    data = analytics.chat_history(wa_number)
    return render_template("admin/chat.html", data=data, wa_number=wa_number)


@admin_bp.route("/ai-history", methods=["GET"])
@admin_bp.route("/history", methods=["GET"])
@login_required
def ai_history() -> Any:
    """AI (Gemini) response history."""
    rows = analytics.ai_history(
        search=clean_query(request.args.get("q", "")),
        period=request.args.get("period", ""),
        start=request.args.get("start", ""),
        end=request.args.get("end", ""),
        only_fallback=request.args.get("fallback") == "1",
    )
    return render_template("admin/ai_history.html", rows=rows)


@admin_bp.route("/orders", methods=["GET"])
@login_required
def orders() -> Any:
    """Shopify order lookup page (queries live Shopify)."""
    query = clean_query(request.args.get("q", ""))
    result = shopify_lookup.lookup(query, by=request.args.get("by", "auto")) if query else {
        "connected": shopify_lookup.store_connected(),
        "orders": [],
        "customer": None,
        "query": "",
    }
    if query and result.get("orders"):
        for order in result["orders"]:
            tracker.record_order_lookup({**order, "customer_name": None})
    return render_template("admin/orders.html", result=result, query=query)


@admin_bp.route("/customers", methods=["GET"])
@login_required
def customers() -> Any:
    """Customer list with search."""
    rows = analytics.list_customers(search=clean_query(request.args.get("q", "")))
    return render_template("admin/customers.html", customers=rows)


@admin_bp.route("/customers/<wa_number>", methods=["GET"])
@login_required
def customer_detail(wa_number: str) -> Any:
    """Full customer profile page."""
    return render_template(
        "admin/customer_detail.html",
        detail=analytics.customer_detail(wa_number),
        wa_number=wa_number,
    )


@admin_bp.route("/analytics", methods=["GET"])
@login_required
def analytics_page() -> Any:
    """Analytics dashboard."""
    summary = analytics.analytics_summary(
        period=request.args.get("period", ""),
        start=request.args.get("start", ""),
        end=request.args.get("end", ""),
    )
    return render_template("admin/analytics.html", summary=summary)


@admin_bp.route("/search", methods=["GET"])
@login_required
def search_page() -> Any:
    """Global search page."""
    query = clean_query(request.args.get("q", ""))
    results = analytics.global_search(query) if query else None
    return render_template("admin/search.html", results=results, query=query)


# --------------------------------------------------------------------------
# JSON APIs (used for auto-refresh, live inbox, notifications)
# --------------------------------------------------------------------------

@admin_bp.route("/api/stats", methods=["GET"])
@login_required
def api_stats() -> Any:
    return jsonify(analytics.dashboard_stats())


@admin_bp.route("/api/notifications", methods=["GET"])
@login_required
def api_notifications() -> Any:
    stats = analytics.dashboard_stats()
    return jsonify({"unread": stats["unread"], "todays_messages": stats["todays_messages"]})


@admin_bp.route("/api/read/<wa_number>", methods=["POST"])
@login_required
@csrf_protect
def api_mark_read(wa_number: str) -> Any:
    """Clear a conversation's unread counter (CSRF-protected state change)."""
    tracker.mark_read(wa_number)
    return jsonify({"ok": True})


@admin_bp.route("/api/inbox", methods=["GET"])
@login_required
def api_inbox() -> Any:
    return jsonify(
        {
            "conversations": analytics.inbox(
                search=clean_query(request.args.get("q", "")),
                only_unread=request.args.get("unread") == "1",
            )
        }
    )


@admin_bp.route("/api/chat/<wa_number>", methods=["GET"])
@login_required
def api_chat(wa_number: str) -> Any:
    return jsonify(analytics.chat_history(wa_number, search=clean_query(request.args.get("q", ""))))


@admin_bp.route("/api/ai-history", methods=["GET"])
@login_required
def api_ai_history() -> Any:
    return jsonify(
        {
            "rows": analytics.ai_history(
                search=clean_query(request.args.get("q", "")),
                period=request.args.get("period", ""),
                start=request.args.get("start", ""),
                end=request.args.get("end", ""),
                only_fallback=request.args.get("fallback") == "1",
            )
        }
    )


@admin_bp.route("/api/orders", methods=["GET"])
@login_required
def api_orders() -> Any:
    query = clean_query(request.args.get("q", ""))
    return jsonify(shopify_lookup.lookup(query, by=request.args.get("by", "auto")))


@admin_bp.route("/api/search", methods=["GET"])
@login_required
def api_search() -> Any:
    query = clean_query(request.args.get("q", ""))
    return jsonify(analytics.global_search(query) if query else {})


@admin_bp.route("/api/analytics", methods=["GET"])
@login_required
def api_analytics() -> Any:
    return jsonify(
        analytics.analytics_summary(
            period=request.args.get("period", ""),
            start=request.args.get("start", ""),
            end=request.args.get("end", ""),
        )
    )


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

_EXPORTS = {
    "inbox": (
        ["wa_number", "profile_name", "last_message", "unread_count", "message_count", "last_message_at"],
        "inbox",
    ),
    "customers": (
        ["wa_number", "profile_name", "language", "email", "msgs", "first_seen_at", "last_seen_at"],
        "customers",
    ),
    "ai": (
        ["created_at", "wa_number", "model", "user_message", "response", "latency_ms", "fallback_used", "error"],
        "ai-history",
    ),
    "orders": (
        ["order_name", "customer_name", "financial_status", "fulfillment_status", "total_price", "currency", "looked_up_at"],
        "orders",
    ),
    "messages": (
        ["created_at", "wa_number", "direction", "text", "language", "intent", "latency_ms"],
        "messages",
    ),
}


def _export_rows(kind: str) -> Tuple[List[Dict[str, Any]], Sequence[str], str]:
    """Assemble rows + columns for a given export ``kind``."""
    period = request.args.get("period", "")
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    search = clean_query(request.args.get("q", ""))

    if kind == "inbox":
        rows = analytics.inbox(search=search)
    elif kind == "customers":
        rows = analytics.list_customers(search=search)
    elif kind == "ai":
        rows = analytics.ai_history(search=search, period=period, start=start, end=end)
    elif kind == "orders":
        with get_conn() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM orders ORDER BY id DESC LIMIT 1000"
                ).fetchall()
            ]
    elif kind == "chat":
        wa = clean_query(request.args.get("wa", ""))
        rows = analytics.chat_history(wa, search=search)["messages"] if wa else []
        return rows, ["created_at", "direction", "text", "language", "intent", "latency_ms"], f"chat-{wa or 'export'}"
    else:  # messages (default)
        kind = "messages"
        with get_conn() as conn:
            where = []
            params: List[Any] = []
            lo, hi = analytics.range_bounds(period, start, end)
            if lo:
                where.append("created_at >= ?")
                params.append(lo)
            if hi:
                where.append("created_at < ?")
                params.append(hi)
            clause = f"WHERE {' AND '.join(where)}" if where else ""
            rows = [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM messages {clause} ORDER BY id DESC LIMIT 5000",
                    tuple(params),
                ).fetchall()
            ]
    columns, name = _EXPORTS[kind]
    return rows, columns, name


@admin_bp.route("/export", methods=["GET"])
@login_required
def export_data() -> Any:
    """Export a dataset as CSV, XLSX or PDF."""
    kind = request.args.get("type", "messages")
    fmt = request.args.get("format", "csv")
    try:
        rows, columns, name = _export_rows(kind)
    except Exception as exc:  # noqa: BLE001
        logger.error("ADMIN | export build failed: %s", exc)
        return jsonify({"error": "export_failed"}), 500

    try:
        data, mimetype, filename = exporter.export(fmt, rows, columns, name, title=name)
    except exporter.ExportUnavailable as exc:
        logger.warning("ADMIN | export format unavailable: %s", exc)
        return (
            jsonify(
                {
                    "error": "format_unavailable",
                    "detail": str(exc),
                    "hint": "CSV export always works; install openpyxl (xlsx) / reportlab (pdf).",
                }
            ),
            501,
        )

    return Response(
        data,
        mimetype=mimetype,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
