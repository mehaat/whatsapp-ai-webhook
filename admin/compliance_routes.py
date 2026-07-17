"""
admin/compliance_routes.py
---------------------------
Admin "Compliance & Audit" area (v8.0). Provides:

    * A compliance home: search a customer, export or erase their data, review
      recent data-subject requests, and see an audit-integrity badge.
    * A paginated, filterable audit-log viewer with CSV/XLSX/PDF export.
    * A one-click audit-chain verification.

This is an additive blueprint mounted at ``/admin/compliance``. Every route is
protected by :func:`admin.security.login_required` *and*
:func:`admin.rbac.role_required` at the ``admin`` level; state-changing routes
additionally enforce :func:`admin.security.csrf_protect`. It shares the existing
admin session, CSRF token and template layout (``admin/base.html``).
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Sequence, Tuple

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from admin import exporter
from admin.rbac import role_required
from admin.security import (
    _client_ip,
    clean_query,
    csrf_protect,
    current_user,
    get_csrf_token,
    is_authenticated,
    login_required,
)
from commerce import compliance
from commerce.audit_chain import verify_chain
from database.db import session_scope
from database.models import AuditLog, DataRequest
from utils.logging import logger

admin_compliance_bp = Blueprint(
    "admin_compliance",
    __name__,
    url_prefix="/admin/compliance",
    template_folder="templates",
)

_PAGE_SIZE = 50
_AUDIT_EXPORT_LIMIT = 5000
_AUDIT_COLUMNS = (
    "id", "created_at", "actor", "action", "entity",
    "entity_id", "detail", "ip", "prev_hash", "row_hash",
)


# --------------------------------------------------------------------------
# Template context (mirrors the admin blueprint's globals)
# --------------------------------------------------------------------------

@admin_compliance_bp.context_processor
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
# Compliance home
# --------------------------------------------------------------------------

@admin_compliance_bp.route("/", methods=["GET"])
@login_required
@role_required("admin")
def compliance_home() -> Any:
    """Compliance dashboard: data-subject tools + audit-integrity badge."""
    integrity = verify_chain()
    return render_template(
        "admin/compliance.html",
        requests=compliance.list_data_requests(100),
        pii_access=compliance.list_pii_access(25),
        integrity=integrity,
        nav_active="admin_compliance.compliance_home",
    )


@admin_compliance_bp.route("/export", methods=["POST"])
@login_required
@role_required("admin")
@csrf_protect
def export_subject() -> Any:
    """Run a GDPR/DPDP data-subject export for a WhatsApp number."""
    wa_number = clean_query(request.form.get("wa_number", ""), 32)
    if not wa_number:
        flash("Enter a WhatsApp number to export.", "error")
        return redirect(url_for("admin_compliance.compliance_home"))

    result = compliance.data_subject_export(
        wa_number, actor=current_user() or "admin", ip=_client_ip()
    )
    if result.get("ok"):
        req_id = result.get("request_id")
        link = url_for("admin_compliance.download_export", req_id=req_id)
        flash(
            f"Export ready for {wa_number}. "
            f"Download: {link}",
            "success",
        )
    else:
        flash(f"Export failed for {wa_number}.", "error")
    return redirect(url_for("admin_compliance.compliance_home"))


@admin_compliance_bp.route("/erase", methods=["POST"])
@login_required
@role_required("admin")
@csrf_protect
def erase_subject() -> Any:
    """Redact a customer's PII (retaining financial records)."""
    wa_number = clean_query(request.form.get("wa_number", ""), 32)
    if not wa_number:
        flash("Enter a WhatsApp number to erase.", "error")
        return redirect(url_for("admin_compliance.compliance_home"))

    result = compliance.erase_customer(
        wa_number, actor=current_user() or "admin", ip=_client_ip()
    )
    if result.get("ok"):
        counts = result.get("erased", {})
        flash(f"Erased PII for {wa_number}: {counts}", "success")
    else:
        flash(f"Erasure failed for {wa_number}.", "error")
    return redirect(url_for("admin_compliance.compliance_home"))


@admin_compliance_bp.route("/download/<int:req_id>", methods=["GET"])
@login_required
@role_required("admin")
def download_export(req_id: int) -> Any:
    """Serve a completed data-subject export JSON file."""
    with session_scope() as session:
        req = session.get(DataRequest, req_id)
        path = req.result_path if req is not None else None
        subject = req.subject_wa_number if req is not None else None

    if not path:
        abort(404)
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        logger.warning("ADMIN | export file missing for request #%s: %s", req_id, path)
        abort(404)

    compliance.log_pii_access(
        current_user() or "admin", subject or "", "export", ip=_client_ip()
    )
    return send_file(
        abs_path,
        mimetype="application/json",
        as_attachment=True,
        download_name=os.path.basename(abs_path),
    )


# --------------------------------------------------------------------------
# Audit-log viewer + export + verify
# --------------------------------------------------------------------------

def _audit_query(session, actor: str, action: str):
    q = session.query(AuditLog)
    if actor:
        q = q.filter(AuditLog.actor.ilike(f"%{actor}%"))
    if action:
        q = q.filter(AuditLog.action.ilike(f"%{action}%"))
    return q


def _audit_row(row: AuditLog) -> Dict[str, Any]:
    return {
        "id": row.id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "actor": row.actor,
        "action": row.action,
        "entity": row.entity,
        "entity_id": row.entity_id,
        "detail": row.detail,
        "ip": row.ip,
        "prev_hash": row.prev_hash,
        "row_hash": row.row_hash,
    }


@admin_compliance_bp.route("/audit", methods=["GET"])
@login_required
@role_required("admin")
def audit_log() -> Any:
    """Paginated audit-log viewer with actor/action filters + export."""
    actor = clean_query(request.args.get("actor", ""), 128)
    action = clean_query(request.args.get("action", ""), 64)
    export_fmt = (request.args.get("export", "") or "").lower()

    # --- export branch (CSV/XLSX/PDF) ---
    if export_fmt in {"csv", "xlsx", "excel", "pdf"}:
        with session_scope() as session:
            rows = [
                _audit_row(r)
                for r in _audit_query(session, actor, action)
                .order_by(AuditLog.id.desc())
                .limit(_AUDIT_EXPORT_LIMIT)
                .all()
            ]
        try:
            data, mimetype, filename = exporter.export(
                export_fmt, rows, _AUDIT_COLUMNS, "audit-log", title="Audit Log"
            )
        except exporter.ExportUnavailable as exc:
            logger.warning("ADMIN | audit export unavailable: %s", exc)
            flash(f"Export format unavailable: {exc}", "error")
            return redirect(url_for("admin_compliance.audit_log", actor=actor, action=action))
        return Response(
            data,
            mimetype=mimetype,
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    # --- paginated view ---
    try:
        page = max(1, int(request.args.get("page", "1")))
    except (TypeError, ValueError):
        page = 1
    offset = (page - 1) * _PAGE_SIZE

    with session_scope() as session:
        base = _audit_query(session, actor, action)
        total = base.count()
        rows = [
            _audit_row(r)
            for r in base.order_by(AuditLog.id.desc())
            .limit(_PAGE_SIZE)
            .offset(offset)
            .all()
        ]

    has_next = offset + _PAGE_SIZE < total
    return render_template(
        "admin/audit_log.html",
        rows=rows,
        total=total,
        page=page,
        page_size=_PAGE_SIZE,
        has_prev=page > 1,
        has_next=has_next,
        f_actor=actor,
        f_action=action,
        nav_active="admin_compliance.compliance_home",
    )


@admin_compliance_bp.route("/audit/verify", methods=["POST"])
@login_required
@role_required("admin")
@csrf_protect
def audit_verify() -> Any:
    """Recompute the audit hash chain and flash the result."""
    result = verify_chain()
    if result.get("ok"):
        flash(
            f"Audit chain verified: {result.get('count', 0)} rows intact.",
            "success",
        )
    else:
        broken = result.get("broken_at")
        if broken:
            flash(f"Audit chain BROKEN at row #{broken}.", "error")
        else:
            flash("Audit chain verification could not complete.", "error")
    return redirect(request.referrer or url_for("admin_compliance.audit_log"))
