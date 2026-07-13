"""
admin/broadcast_routes.py
--------------------------
Admin "Broadcast" area for the v7.0 WhatsApp broadcast manager: a compose form
with a live recipient-count preview, and a send action that fans the message out
to the resolved CRM audience via :func:`commerce.broadcast.send_broadcast`.

Mounted at ``/admin/broadcast``. Viewing requires an authenticated session
(:func:`admin.security.login_required`); sending additionally requires at least
the ``manager`` role (:func:`admin.rbac.role_required`) and CSRF validation.
"""

from __future__ import annotations

from typing import Any, Dict

from flask import (
    Blueprint,
    flash,
    jsonify,
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
from commerce import broadcast
from utils.logging import logger

admin_broadcast_bp = Blueprint(
    "admin_broadcast",
    __name__,
    url_prefix="/admin/broadcast",
    template_folder="templates",
)


# --------------------------------------------------------------------------
# Template context (mirrors the admin blueprint's globals)
# --------------------------------------------------------------------------

@admin_broadcast_bp.context_processor
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


def _consent_only(default: bool = True) -> bool:
    raw = request.values.get("consent_only")
    if raw is None:
        return default
    return raw in ("on", "true", "1", "yes")


# --------------------------------------------------------------------------
# Compose + send
# --------------------------------------------------------------------------

@admin_broadcast_bp.route("/", methods=["GET"])
@login_required
def broadcast_page() -> Any:
    """Render the compose form with a recipient-count preview."""
    segment = clean_query(request.args.get("segment", ""), 32) or None
    tag = clean_query(request.args.get("tag", ""), 64) or None
    consent_only = _consent_only()
    count = broadcast.recipient_count(segment=segment, tag=tag, consent_only=consent_only)
    return render_template(
        "admin/broadcast.html",
        recipient_count=count,
        segment=segment or "",
        tag=tag or "",
        consent_only=consent_only,
    )


@admin_broadcast_bp.route("/preview", methods=["GET"])
@login_required
def broadcast_preview() -> Any:
    """JSON recipient-count preview (live update as filters change)."""
    segment = clean_query(request.args.get("segment", ""), 32) or None
    tag = clean_query(request.args.get("tag", ""), 64) or None
    count = broadcast.recipient_count(
        segment=segment, tag=tag, consent_only=_consent_only()
    )
    return jsonify({"recipients": count})


@admin_broadcast_bp.route("/", methods=["POST"])
@login_required
@role_required("manager")
@csrf_protect
def broadcast_send() -> Any:
    """Send a broadcast to the resolved audience."""
    message = request.form.get("message", "")
    segment = clean_query(request.form.get("segment", ""), 32) or None
    tag = clean_query(request.form.get("tag", ""), 64) or None
    consent_only = _consent_only()
    actor = current_user() or "admin"

    if not message.strip():
        flash("Message cannot be empty.", "error")
        return redirect(url_for("admin_broadcast.broadcast_page"))

    try:
        result = broadcast.send_broadcast(
            message, segment=segment, tag=tag, consent_only=consent_only, actor=actor
        )
    except Exception as exc:  # noqa: BLE001 - never 500 the admin on a send
        logger.error("ADMIN | broadcast send failed: %s", exc)
        flash(f"Broadcast failed: {exc}", "error")
        return redirect(url_for("admin_broadcast.broadcast_page"))

    if result.get("ok"):
        flash(f"Broadcast queued for {result.get('recipients', 0)} recipient(s).", "success")
    else:
        flash(f"Broadcast not sent: {result.get('error', 'unknown error')}.", "error")
    return redirect(url_for("admin_broadcast.broadcast_page"))
