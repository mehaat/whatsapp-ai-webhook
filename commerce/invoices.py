"""
commerce/invoices.py
--------------------
Professional PDF invoice generator for ME-HAAT Fashion AI Bot v6.0.

Renders an A4 tax invoice with ReportLab's platypus layer: business identity
header (with optional logo), an "INVOICE" title block, a "Bill To" panel, a
line-items table, a right-aligned totals block, a QR code (checkout URL or an
order summary), and a footer. A maroon/gold accent palette gives the document a
branded, boutique look.

The public entry point is :func:`generate_invoice`. It mints a sequential
invoice number, writes ``{invoice_output_dir}/{invoice_number}.pdf``, records the
invoice against the order via ``order_service.record_invoice`` and returns a
small dict describing the result.

Every optional field (logo, address, GSTIN, city/state, checkout URL, ...) is
guarded so the invoice always renders even when those fields are blank.
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Any, Dict, List, Optional

import qrcode
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from config import config
from database.db import session_scope
from commerce.numbering import next_invoice_number
from utils.logging import logger

# --- Brand palette (maroon / gold) --------------------------------------------
ACCENT_MAROON = colors.HexColor("#800020")
ACCENT_GOLD = colors.HexColor("#C9A227")
LIGHT_GOLD = colors.HexColor("#F5EEDC")
DARK_TEXT = colors.HexColor("#2B2B2B")
MUTED_TEXT = colors.HexColor("#6B6B6B")

_CURRENCY_SYMBOLS: Dict[str, str] = {
    "INR": "₹",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
}


def _currency_symbol(code: Optional[str]) -> str:
    """Return the display symbol for a currency code (falls back to the code).

    :param code: An ISO 4217 currency code such as ``"INR"``. ``None`` or an
        unknown code returns the code itself (or empty string for ``None``).
    :returns: A short currency symbol, e.g. ``"₹"`` for ``"INR"``.
    """
    if not code:
        return ""
    key = str(code).strip().upper()
    return _CURRENCY_SYMBOLS.get(key, key)


def _to_decimal(value: Any) -> Decimal:
    """Coerce a float/int/str/None into a :class:`~decimal.Decimal`.

    Blank or invalid values become ``Decimal("0")`` so totals always render.
    """
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _fmt_money(symbol: str, amount: Decimal) -> str:
    """Format an amount with a currency symbol and two decimal places."""
    return f"{symbol}{amount:,.2f}"


def _make_qr_image(payload: str, size_mm: float = 32.0) -> Image:
    """Build an in-memory QR code and wrap it as a platypus ``Image``.

    :param payload: The text/URL to encode.
    :param size_mm: Rendered square size in millimetres.
    :returns: A flowable ``Image`` backed by an in-memory PNG buffer.
    """
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(payload or "")
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return Image(buffer, width=size_mm * mm, height=size_mm * mm)


def _build_styles() -> Dict[str, ParagraphStyle]:
    """Create the named paragraph styles used across the invoice."""
    base = getSampleStyleSheet()
    styles: Dict[str, ParagraphStyle] = {}

    styles["business"] = ParagraphStyle(
        "business", parent=base["Heading1"], fontName="Helvetica-Bold",
        fontSize=20, leading=24, textColor=ACCENT_MAROON, spaceAfter=2,
    )
    styles["identity"] = ParagraphStyle(
        "identity", parent=base["Normal"], fontName="Helvetica",
        fontSize=8.5, leading=11, textColor=MUTED_TEXT,
    )
    styles["title"] = ParagraphStyle(
        "title", parent=base["Heading1"], fontName="Helvetica-Bold",
        fontSize=22, leading=24, textColor=ACCENT_GOLD, alignment=TA_RIGHT,
    )
    styles["meta"] = ParagraphStyle(
        "meta", parent=base["Normal"], fontName="Helvetica",
        fontSize=9, leading=13, textColor=DARK_TEXT, alignment=TA_RIGHT,
    )
    styles["section"] = ParagraphStyle(
        "section", parent=base["Heading2"], fontName="Helvetica-Bold",
        fontSize=10, leading=13, textColor=ACCENT_MAROON, spaceAfter=2,
    )
    styles["body"] = ParagraphStyle(
        "body", parent=base["Normal"], fontName="Helvetica",
        fontSize=9, leading=12, textColor=DARK_TEXT,
    )
    styles["cell"] = ParagraphStyle(
        "cell", parent=base["Normal"], fontName="Helvetica",
        fontSize=8.5, leading=11, textColor=DARK_TEXT,
    )
    styles["cell_head"] = ParagraphStyle(
        "cell_head", parent=base["Normal"], fontName="Helvetica-Bold",
        fontSize=8.5, leading=11, textColor=colors.white,
    )
    styles["footer"] = ParagraphStyle(
        "footer", parent=base["Normal"], fontName="Helvetica-Oblique",
        fontSize=9, leading=12, textColor=MUTED_TEXT, alignment=TA_CENTER,
    )
    styles["qr_caption"] = ParagraphStyle(
        "qr_caption", parent=base["Normal"], fontName="Helvetica",
        fontSize=7.5, leading=10, textColor=MUTED_TEXT, alignment=TA_LEFT,
    )
    return styles


def _identity_lines(styles: Dict[str, ParagraphStyle]) -> List[Paragraph]:
    """Return non-empty business identity paragraphs (address, GSTIN, ...)."""
    lines: List[Paragraph] = []
    address = (getattr(config, "business_address", "") or "").strip()
    gstin = (getattr(config, "business_gstin", "") or "").strip()
    phone = (getattr(config, "business_phone", "") or "").strip()
    email = (getattr(config, "business_email", "") or "").strip()
    website = (getattr(config, "business_website", "") or "").strip()

    if address:
        lines.append(Paragraph(address.replace("\n", "<br/>"), styles["identity"]))
    if gstin:
        lines.append(Paragraph(f"GSTIN: {gstin}", styles["identity"]))
    if phone:
        lines.append(Paragraph(f"Phone: {phone}", styles["identity"]))
    if email:
        lines.append(Paragraph(f"Email: {email}", styles["identity"]))
    if website:
        lines.append(Paragraph(website, styles["identity"]))
    return lines


def _header_flowable(styles: Dict[str, ParagraphStyle], invoice_number: str,
                     order: Dict[str, Any]) -> Table:
    """Build the top header: logo + identity on the left, INVOICE meta right."""
    business_name = (getattr(config, "business_name", "") or "ME-HAAT Fashion").strip()

    left_cells: List[Any] = []
    logo_path = (getattr(config, "invoice_logo_path", "") or "").strip()
    if logo_path and os.path.isfile(logo_path):
        try:
            left_cells.append(Image(logo_path, width=42 * mm, height=18 * mm,
                                    kind="proportional"))
            left_cells.append(Spacer(1, 4))
        except Exception as exc:  # noqa: BLE001 - a bad logo must not break the invoice
            logger.warning("INVOICE | logo render skipped (%s): %s", logo_path, exc)
    left_cells.append(Paragraph(business_name, styles["business"]))
    left_cells.extend(_identity_lines(styles))

    date_str = _format_date(order.get("created_at"))
    right_bits = [Paragraph("INVOICE", styles["title"]), Spacer(1, 6)]
    right_bits.append(Paragraph(f"<b>No:</b> {invoice_number}", styles["meta"]))
    order_number = order.get("order_number")
    if order_number:
        right_bits.append(Paragraph(f"<b>Order:</b> {order_number}", styles["meta"]))
    right_bits.append(Paragraph(f"<b>Date:</b> {date_str}", styles["meta"]))

    header = Table([[left_cells, right_bits]], colWidths=[105 * mm, 70 * mm])
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return header


def _format_date(created_at: Any) -> str:
    """Render an ISO datetime string as ``DD Mon YYYY`` (best-effort)."""
    if not created_at:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime("%d %b %Y")
    try:
        from datetime import datetime
        text = str(created_at).replace("Z", "+00:00")
        return datetime.fromisoformat(text).strftime("%d %b %Y")
    except Exception:  # noqa: BLE001
        return str(created_at)


def _bill_to_flowable(styles: Dict[str, ParagraphStyle],
                      order: Dict[str, Any]) -> Table:
    """Build the "Bill To" panel from customer fields (blank-safe)."""
    lines: List[Paragraph] = [Paragraph("BILL TO", styles["section"])]
    name = (order.get("customer_name") or "").strip()
    wa_number = (order.get("wa_number") or "").strip()
    city = (order.get("city") or "").strip()
    state = (order.get("state") or "").strip()

    lines.append(Paragraph(name or "Valued Customer", styles["body"]))
    if wa_number:
        lines.append(Paragraph(f"WhatsApp: {wa_number}", styles["body"]))
    locality = ", ".join(part for part in (city, state) if part)
    if locality:
        lines.append(Paragraph(locality, styles["body"]))

    panel = Table([[lines]], colWidths=[105 * mm])
    panel.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_GOLD),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBEFORE", (0, 0), (0, -1), 2, ACCENT_MAROON),
    ]))
    return panel


def _items_table(styles: Dict[str, ParagraphStyle], order: Dict[str, Any],
                 symbol: str) -> Table:
    """Build the line-items table with header, striped rows and totals."""
    header = [
        Paragraph("#", styles["cell_head"]),
        Paragraph("Product", styles["cell_head"]),
        Paragraph("Variant", styles["cell_head"]),
        Paragraph("Qty", styles["cell_head"]),
        Paragraph("Unit Price", styles["cell_head"]),
        Paragraph("Line Total", styles["cell_head"]),
    ]
    rows: List[List[Any]] = [header]

    items = order.get("items") or []
    for idx, item in enumerate(items, start=1):
        name = (item.get("product_name") or "").strip()
        variant = (item.get("variant") or "").strip() or "-"
        qty = item.get("quantity") or 0
        unit_price = _to_decimal(item.get("unit_price"))
        line_total = item.get("line_total")
        line_dec = _to_decimal(line_total) if line_total not in (None, "") \
            else unit_price * _to_decimal(qty)
        rows.append([
            Paragraph(str(idx), styles["cell"]),
            Paragraph(name or "-", styles["cell"]),
            Paragraph(variant, styles["cell"]),
            Paragraph(str(qty), styles["cell"]),
            Paragraph(_fmt_money(symbol, unit_price), styles["cell"]),
            Paragraph(_fmt_money(symbol, line_dec), styles["cell"]),
        ])

    if not items:
        rows.append([
            Paragraph("-", styles["cell"]),
            Paragraph("No line items", styles["cell"]),
            Paragraph("-", styles["cell"]),
            Paragraph("-", styles["cell"]),
            Paragraph("-", styles["cell"]),
            Paragraph("-", styles["cell"]),
        ])

    col_widths = [10 * mm, 62 * mm, 33 * mm, 14 * mm, 28 * mm, 28 * mm]
    table = Table(rows, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT_MAROON),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (3, 0), (5, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#E0D8C4")),
        ("LINEBELOW", (0, 0), (-1, 0), 1, ACCENT_GOLD),
    ]
    for row_index in range(1, len(rows)):
        if row_index % 2 == 0:
            style.append(("BACKGROUND", (0, row_index), (-1, row_index), LIGHT_GOLD))
    table.setStyle(TableStyle(style))
    return table


def _totals_table(styles: Dict[str, ParagraphStyle], order: Dict[str, Any],
                  symbol: str) -> Table:
    """Build the right-aligned totals block ending in a bold grand total."""
    subtotal = _to_decimal(order.get("subtotal"))
    discount = _to_decimal(order.get("discount"))
    shipping = _to_decimal(order.get("shipping"))
    tax = _to_decimal(order.get("tax"))
    grand_total = _compute_total(order)

    label_style = ParagraphStyle("tl", parent=styles["body"], alignment=TA_RIGHT)
    value_style = ParagraphStyle("tv", parent=styles["body"], alignment=TA_RIGHT)
    grand_label = ParagraphStyle(
        "gl", parent=styles["body"], alignment=TA_RIGHT,
        fontName="Helvetica-Bold", fontSize=11, textColor=colors.white,
    )
    grand_value = ParagraphStyle(
        "gv", parent=styles["body"], alignment=TA_RIGHT,
        fontName="Helvetica-Bold", fontSize=11, textColor=colors.white,
    )

    rows = [
        [Paragraph("Subtotal", label_style),
         Paragraph(_fmt_money(symbol, subtotal), value_style)],
        [Paragraph("Discount", label_style),
         Paragraph(f"-{_fmt_money(symbol, discount)}", value_style)],
        [Paragraph("Shipping", label_style),
         Paragraph(_fmt_money(symbol, shipping), value_style)],
        [Paragraph("Tax (GST)", label_style),
         Paragraph(_fmt_money(symbol, tax), value_style)],
        [Paragraph("Grand Total", grand_label),
         Paragraph(_fmt_money(symbol, grand_total), grand_value)],
    ]
    table = Table(rows, colWidths=[38 * mm, 32 * mm])
    last = len(rows) - 1
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("LINEABOVE", (0, 0), (-1, 0), 0.5, colors.HexColor("#E0D8C4")),
        ("BACKGROUND", (0, last), (-1, last), ACCENT_MAROON),
    ]))
    return table


def _compute_total(order: Dict[str, Any]) -> Decimal:
    """Grand total: prefer ``total_amount``, else subtotal-discount+ship+tax."""
    total_amount = order.get("total_amount")
    if total_amount not in (None, ""):
        return _to_decimal(total_amount)
    return (
        _to_decimal(order.get("subtotal"))
        - _to_decimal(order.get("discount"))
        + _to_decimal(order.get("shipping"))
        + _to_decimal(order.get("tax"))
    )


def _qr_payload(order: Dict[str, Any], total: Decimal, symbol: str) -> str:
    """Return the QR payload: checkout URL if present, else a text summary."""
    checkout_url = (order.get("checkout_url") or "").strip()
    if checkout_url:
        return checkout_url
    business_name = (getattr(config, "business_name", "") or "ME-HAAT Fashion").strip()
    order_number = order.get("order_number") or ""
    return f"{business_name} {order_number} {symbol}{total:,.2f}".strip()


def _build_story(order: Dict[str, Any], invoice_number: str,
                 total: Decimal, symbol: str) -> List[Any]:
    """Assemble the ordered list of platypus flowables for the document."""
    styles = _build_styles()
    story: List[Any] = []

    story.append(_header_flowable(styles, invoice_number, order))
    story.append(Spacer(1, 6))
    # Gold rule under the header.
    rule = Table([[""]], colWidths=[175 * mm], rowHeights=[2])
    rule.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), ACCENT_GOLD)]))
    story.append(rule)
    story.append(Spacer(1, 10))

    story.append(_bill_to_flowable(styles, order))
    story.append(Spacer(1, 12))

    story.append(_items_table(styles, order, symbol))
    story.append(Spacer(1, 10))

    # QR (left) beside the totals block (right).
    qr_payload = _qr_payload(order, total, symbol)
    qr_cells = [
        _make_qr_image(qr_payload),
        Spacer(1, 2),
        Paragraph("Scan to pay / view order", styles["qr_caption"]),
    ]
    bottom = Table(
        [[qr_cells, _totals_table(styles, order, symbol)]],
        colWidths=[105 * mm, 70 * mm],
    )
    bottom.setStyle(TableStyle([
        ("VALIGN", (0, 0), (0, 0), "TOP"),
        ("VALIGN", (1, 0), (1, 0), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(bottom)
    story.append(Spacer(1, 24))

    business_name = (getattr(config, "business_name", "") or "ME-HAAT Fashion").strip()
    story.append(Paragraph(
        f"Thank you for shopping with {business_name}", styles["footer"]))
    return story


def generate_invoice(order: Dict[str, Any]) -> Dict[str, Any]:
    """Generate a PDF invoice for ``order`` and record it against the order.

    Steps:
        1. Ensure ``config.invoice_output_dir`` exists.
        2. Mint the next invoice number inside a database session.
        3. Render an A4 PDF to ``{invoice_output_dir}/{invoice_number}.pdf``.
        4. Record the invoice via ``order_service.record_invoice`` (lazy import).

    :param order: An order dict following the v6 ORDER contract (id,
        order_number, items, totals, ...).
    :returns: ``{"invoice_number": str, "pdf_path": str, "total": float}``.
    :raises RuntimeError: If PDF generation fails for any reason.
    """
    output_dir = getattr(config, "invoice_output_dir", "") or "invoices"
    os.makedirs(output_dir, exist_ok=True)

    with session_scope() as session:
        invoice_number = next_invoice_number(session)

    currency = (order.get("currency") or getattr(config, "default_currency", "INR")
                or "INR")
    symbol = _currency_symbol(currency)
    total = _compute_total(order)
    pdf_path = os.path.join(output_dir, f"{invoice_number}.pdf")

    try:
        doc = SimpleDocTemplate(
            pdf_path,
            pagesize=A4,
            leftMargin=18 * mm,
            rightMargin=18 * mm,
            topMargin=16 * mm,
            bottomMargin=16 * mm,
            title=f"Invoice {invoice_number}",
            author=getattr(config, "business_name", "ME-HAAT Fashion"),
        )
        story = _build_story(order, invoice_number, total, symbol)
        doc.build(story)
    except Exception as exc:  # noqa: BLE001 - wrap in a clear, caller-facing error
        logger.error("INVOICE | PDF generation failed for %s: %s",
                     invoice_number, exc)
        raise RuntimeError(
            f"Failed to generate invoice PDF {invoice_number}: {exc}"
        ) from exc

    logger.info("INVOICE | Generated %s -> %s", invoice_number, pdf_path)

    order_id = order.get("id")
    if order_id is not None:
        try:
            from commerce.service import order_service

            order_service.record_invoice(
                order_id,
                invoice_number=invoice_number,
                pdf_path=pdf_path,
                total=total,
                currency=currency,
            )
        except Exception as exc:  # noqa: BLE001 - PDF exists; don't lose it on a DB hiccup
            logger.error("INVOICE | record_invoice failed for %s: %s",
                         invoice_number, exc)

    return {
        "invoice_number": invoice_number,
        "pdf_path": pdf_path,
        "total": float(total),
    }
