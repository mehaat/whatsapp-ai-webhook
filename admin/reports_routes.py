"""
admin/reports_routes.py
------------------------
Admin "Reports" area (v7.0): a hub linking every business report (GST, sales,
inventory, customers, products) with date filters, a rendered table view per
report, and CSV/XLSX/PDF export reusing :mod:`admin.exporter`.

This is an additive blueprint mounted at ``/admin/reports``. Every route is
protected by :func:`admin.security.login_required` and gated at the ``manager``
role via :func:`admin.rbac.role_required`. It never mutates the existing
``/admin`` blueprint and shares its session, CSRF token and template layout
(``admin/base.html``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

from flask import (
    Blueprint,
    Response,
    redirect,
    render_template,
    request,
    url_for,
)

from admin import exporter
from admin.rbac import role_required
from admin.security import (
    current_user,
    get_csrf_token,
    is_authenticated,
    login_required,
)
from commerce import reports as reports_service
from config import config
from utils.logging import logger

admin_reports_bp = Blueprint(
    "admin_reports",
    __name__,
    url_prefix="/admin/reports",
    template_folder="templates",
)

# Ordered report catalog: name -> (label, description, icon, takes_dates).
_REPORTS = [
    ("sales", "Sales", "Revenue and orders grouped by day or month.", "bi-graph-up-arrow", True),
    ("gst", "GST / Tax", "Per-order taxable value, GST and totals.", "bi-receipt", True),
    ("inventory", "Inventory", "Reserved vs committed stock and top ordered items.", "bi-box-seam", False),
    ("customer", "Customers", "Top customers ranked by spend.", "bi-people", True),
    ("product", "Products", "Top products by quantity sold and revenue.", "bi-bag-check", True),
]
_REPORT_LABELS = {name: label for name, label, _d, _i, _t in _REPORTS}
_REPORT_TAKES_DATES = {name: takes for name, _l, _d, _i, takes in _REPORTS}


# --------------------------------------------------------------------------
# Template context (mirrors the admin blueprint's globals)
# --------------------------------------------------------------------------

@admin_reports_bp.context_processor
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
        return config.version
    except Exception:  # noqa: BLE001
        return ""


def _report_kwargs() -> Dict[str, Any]:
    """Extract date-range + grouping query params for a report call."""
    return {
        "date_from": (request.args.get("date_from", "") or "").strip() or None,
        "date_to": (request.args.get("date_to", "") or "").strip() or None,
        "group": (request.args.get("group", "day") or "day").strip().lower(),
    }


def _rows_as_dicts(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Zip a report's list-rows onto its columns for the exporter (dict rows)."""
    columns = report.get("columns", [])
    return [dict(zip(columns, row)) for row in report.get("rows", [])]


# --------------------------------------------------------------------------
# Hub
# --------------------------------------------------------------------------

@admin_reports_bp.route("/", methods=["GET"])
@login_required
@role_required("manager")
def reports_home() -> Any:
    """Reports hub linking each report with a shared date-range filter."""
    return render_template(
        "admin/reports.html",
        reports=_REPORTS,
        report=None,
        report_name=None,
        kwargs=_report_kwargs(),
        nav_active="admin_reports.reports_home",
    )


# --------------------------------------------------------------------------
# Single report view
# --------------------------------------------------------------------------

@admin_reports_bp.route("/<report_name>", methods=["GET"])
@login_required
@role_required("manager")
def report_view(report_name: str) -> Any:
    """Render one report as a table (with the shared filter toolbar)."""
    name = (report_name or "").strip().lower()
    if name not in _REPORT_LABELS:
        return redirect(url_for("admin_reports.reports_home"))

    kwargs = _report_kwargs()
    report = reports_service.run_report(name, **kwargs)
    return render_template(
        "admin/reports.html",
        reports=_REPORTS,
        report=report,
        report_name=name,
        report_label=_REPORT_LABELS[name],
        takes_dates=_REPORT_TAKES_DATES.get(name, True),
        kwargs=kwargs,
        nav_active="admin_reports.reports_home",
    )


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

@admin_reports_bp.route("/<report_name>/export", methods=["GET"])
@login_required
@role_required("manager")
def report_export(report_name: str) -> Any:
    """Export a report as CSV, XLSX or PDF (reuses :mod:`admin.exporter`)."""
    name = (report_name or "").strip().lower()
    if name not in _REPORT_LABELS:
        return redirect(url_for("admin_reports.reports_home"))

    fmt = (request.args.get("format", "csv") or "csv").strip().lower()
    report = reports_service.run_report(name, **_report_kwargs())
    columns: Sequence[str] = report.get("columns", [])
    rows = _rows_as_dicts(report)

    try:
        data, mimetype, filename = exporter.export(
            fmt, rows, columns, f"report-{name}", title=f"{_REPORT_LABELS[name]} Report",
        )
    except exporter.ExportUnavailable as exc:
        logger.warning("REPORTS | export format unavailable: %s", exc)
        return {
            "error": "format_unavailable",
            "detail": str(exc),
            "hint": "CSV export always works; install openpyxl (xlsx) / reportlab (pdf).",
        }, 501

    return Response(
        data,
        mimetype=mimetype,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
