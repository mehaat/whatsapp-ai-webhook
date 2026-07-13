"""
commerce/packing.py
--------------------
ReportLab PDF generators for fulfilment paperwork (v7.0):

* :func:`generate_packing_slip` — an itemised pick/pack sheet (products, qty,
  ship-to panel) the warehouse uses to assemble an order.
* :func:`generate_shipping_label` — a courier label with a large AWB, a simple
  Code128-style barcode text band, courier name and from/to blocks.

Both reuse the maroon/gold boutique palette and paragraph-style approach of
``commerce/invoices.py`` and write into ``config.invoice_output_dir``. Every
optional field is guarded so a document always renders; each entry point
returns ``{"path": <pdf_path>}``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from config import config
from utils.logging import logger

# Brand palette (shared with commerce/invoices.py).
ACCENT_MAROON = colors.HexColor("#800020")
ACCENT_GOLD = colors.HexColor("#C9A227")
LIGHT_GOLD = colors.HexColor("#F5EEDC")
DARK_TEXT = colors.HexColor("#2B2B2B")
MUTED_TEXT = colors.HexColor("#6B6B6B")


def _output_dir() -> str:
    """Return (creating if needed) the shared PDF output directory."""
    output_dir = getattr(config, "invoice_output_dir", "") or "invoices"
    os.makedirs(output_dir, exist_ok=True)
    return output_dir


def _styles() -> Dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    styles: Dict[str, ParagraphStyle] = {}
    styles["business"] = ParagraphStyle(
        "business", parent=base["Heading1"], fontName="Helvetica-Bold",
        fontSize=18, leading=22, textColor=ACCENT_MAROON, spaceAfter=2,
    )
    styles["title"] = ParagraphStyle(
        "title", parent=base["Heading1"], fontName="Helvetica-Bold",
        fontSize=20, leading=24, textColor=ACCENT_GOLD,
    )
    styles["section"] = ParagraphStyle(
        "section", parent=base["Heading2"], fontName="Helvetica-Bold",
        fontSize=10, leading=13, textColor=ACCENT_MAROON, spaceAfter=2,
    )
    styles["body"] = ParagraphStyle(
        "body", parent=base["Normal"], fontName="Helvetica",
        fontSize=9.5, leading=13, textColor=DARK_TEXT,
    )
    styles["cell"] = ParagraphStyle(
        "cell", parent=base["Normal"], fontName="Helvetica",
        fontSize=9, leading=12, textColor=DARK_TEXT,
    )
    styles["cell_head"] = ParagraphStyle(
        "cell_head", parent=base["Normal"], fontName="Helvetica-Bold",
        fontSize=9, leading=12, textColor=colors.white,
    )
    styles["awb"] = ParagraphStyle(
        "awb", parent=base["Heading1"], fontName="Helvetica-Bold",
        fontSize=30, leading=34, textColor=DARK_TEXT, alignment=TA_CENTER,
    )
    styles["barcode"] = ParagraphStyle(
        "barcode", parent=base["Normal"], fontName="Courier-Bold",
        fontSize=26, leading=28, textColor=DARK_TEXT, alignment=TA_CENTER,
    )
    styles["muted"] = ParagraphStyle(
        "muted", parent=base["Normal"], fontName="Helvetica",
        fontSize=8.5, leading=11, textColor=MUTED_TEXT, alignment=TA_LEFT,
    )
    return styles


def _business_name() -> str:
    return (getattr(config, "business_name", "") or "ME-HAAT Fashion").strip()


def _ship_to_lines(styles: Dict[str, ParagraphStyle], order: Dict[str, Any]) -> List[Any]:
    """Return the guarded ship-to paragraphs for an order."""
    name = (order.get("customer_name") or "").strip() or "Valued Customer"
    wa_number = (order.get("wa_number") or "").strip()
    city = (order.get("city") or "").strip()
    state = (order.get("state") or "").strip()

    lines: List[Any] = [Paragraph("SHIP TO", styles["section"]),
                        Paragraph(name, styles["body"])]
    if wa_number:
        lines.append(Paragraph(f"Phone: {wa_number}", styles["body"]))
    locality = ", ".join(part for part in (city, state) if part)
    if locality:
        lines.append(Paragraph(locality, styles["body"]))
    return lines


def _header(styles: Dict[str, ParagraphStyle], title: str, order: Dict[str, Any]) -> Table:
    order_number = order.get("order_number") or order.get("id") or ""
    date_str = datetime.now(timezone.utc).strftime("%d %b %Y")
    left = [Paragraph(_business_name(), styles["business"])]
    right = [
        Paragraph(title, styles["title"]),
        Spacer(1, 4),
        Paragraph(f"<b>Order:</b> {order_number}", styles["body"]),
        Paragraph(f"<b>Date:</b> {date_str}", styles["body"]),
    ]
    header = Table([[left, right]], colWidths=[95 * mm, 80 * mm])
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return header


def _gold_rule() -> Table:
    rule = Table([[""]], colWidths=[175 * mm], rowHeights=[2])
    rule.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), ACCENT_GOLD)]))
    return rule


def _build(pdf_path: str, story: List[Any], title: str) -> None:
    doc = SimpleDocTemplate(
        pdf_path, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title=title, author=_business_name(),
    )
    doc.build(story)


def generate_packing_slip(order: Dict[str, Any]) -> Dict[str, Any]:
    """Generate an itemised packing slip PDF for an order.

    Args:
        order: A v6 order dict (id, order_number, items, ship-to fields).

    Returns:
        ``{"path": <pdf_path>}``.

    Raises:
        RuntimeError: If PDF generation fails.
    """
    output_dir = _output_dir()
    order_ref = str(order.get("order_number") or order.get("id") or "packing")
    pdf_path = os.path.join(output_dir, f"packing-{order_ref}.pdf")
    styles = _styles()

    story: List[Any] = [_header(styles, "PACKING SLIP", order), Spacer(1, 6),
                        _gold_rule(), Spacer(1, 10)]

    # Ship-to panel.
    panel = Table([[_ship_to_lines(styles, order)]], colWidths=[175 * mm])
    panel.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_GOLD),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBEFORE", (0, 0), (0, -1), 2, ACCENT_MAROON),
    ]))
    story.append(panel)
    story.append(Spacer(1, 12))

    # Items table (product / variant / qty).
    rows: List[List[Any]] = [[
        Paragraph("#", styles["cell_head"]),
        Paragraph("Product", styles["cell_head"]),
        Paragraph("Variant", styles["cell_head"]),
        Paragraph("Qty", styles["cell_head"]),
        Paragraph("Packed", styles["cell_head"]),
    ]]
    items = order.get("items") or []
    total_qty = 0
    for idx, item in enumerate(items, start=1):
        qty = int(item.get("quantity") or 0)
        total_qty += qty
        rows.append([
            Paragraph(str(idx), styles["cell"]),
            Paragraph((item.get("product_name") or "-").strip() or "-", styles["cell"]),
            Paragraph((item.get("variant") or "-").strip() or "-", styles["cell"]),
            Paragraph(str(qty), styles["cell"]),
            Paragraph("&#9633;", styles["cell"]),
        ])
    if not items:
        rows.append([Paragraph("-", styles["cell"]),
                    Paragraph("No line items", styles["cell"]),
                    Paragraph("-", styles["cell"]),
                    Paragraph("-", styles["cell"]),
                    Paragraph("-", styles["cell"])])

    table = Table(rows, colWidths=[10 * mm, 95 * mm, 40 * mm, 15 * mm, 15 * mm],
                  repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT_MAROON),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (3, 0), (4, -1), "CENTER"),
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
    story.append(table)
    story.append(Spacer(1, 12))
    story.append(Paragraph(
        f"<b>Total items:</b> {len(items)} &nbsp;&nbsp; <b>Total quantity:</b> {total_qty}",
        styles["body"]))

    try:
        _build(pdf_path, story, f"Packing Slip {order_ref}")
    except Exception as exc:  # noqa: BLE001
        logger.error("PACKING | packing slip generation failed for %s: %s",
                     order_ref, exc)
        raise RuntimeError(f"Failed to generate packing slip for {order_ref}: {exc}") from exc

    logger.info("PACKING | packing slip generated -> %s", pdf_path)
    return {"path": pdf_path}


def generate_shipping_label(order: Dict[str, Any], shipment: Dict[str, Any]) -> Dict[str, Any]:
    """Generate a courier shipping label PDF for a shipment.

    Args:
        order: The order dict (ship-to fields).
        shipment: A shipment dict (awb, courier_name, provider).

    Returns:
        ``{"path": <pdf_path>}``.

    Raises:
        RuntimeError: If PDF generation fails.
    """
    output_dir = _output_dir()
    awb = str(shipment.get("awb") or "").strip()
    order_ref = str(order.get("order_number") or order.get("id") or "label")
    label_ref = awb or order_ref
    pdf_path = os.path.join(output_dir, f"label-{label_ref}.pdf")
    styles = _styles()
    courier = (shipment.get("courier_name") or shipment.get("provider") or "Courier").strip()

    story: List[Any] = [_header(styles, "SHIPPING LABEL", order), Spacer(1, 6),
                        _gold_rule(), Spacer(1, 10)]

    # Courier band.
    story.append(Paragraph(f"<b>Courier:</b> {courier}", styles["body"]))
    story.append(Spacer(1, 8))

    # Big AWB + simple barcode-ish text band.
    awb_display = awb or "PENDING"
    awb_box = Table(
        [[Paragraph("AWB", styles["muted"])],
         [Paragraph(awb_display, styles["awb"])],
         [Paragraph(f"*{awb_display}*", styles["barcode"])]],
        colWidths=[175 * mm],
    )
    awb_box.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1.2, DARK_TEXT),
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    story.append(awb_box)
    story.append(Spacer(1, 14))

    # From / To blocks side by side.
    from_lines = [
        Paragraph("FROM", styles["section"]),
        Paragraph(_business_name(), styles["body"]),
    ]
    address = (getattr(config, "business_address", "") or "").strip()
    if address:
        from_lines.append(Paragraph(address.replace("\n", "<br/>"), styles["body"]))
    phone = (getattr(config, "business_phone", "") or "").strip()
    if phone:
        from_lines.append(Paragraph(f"Phone: {phone}", styles["body"]))
    pickup_pin = (getattr(config, "pickup_pincode", "") or "").strip()
    if pickup_pin:
        from_lines.append(Paragraph(f"PIN: {pickup_pin}", styles["body"]))

    blocks = Table([[from_lines, _ship_to_lines(styles, order)]],
                   colWidths=[87 * mm, 88 * mm])
    blocks.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#E0D8C4")),
        ("INNERGRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#E0D8C4")),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(blocks)
    story.append(Spacer(1, 10))
    story.append(Paragraph(f"Order Ref: {order_ref}", styles["muted"]))

    try:
        _build(pdf_path, story, f"Shipping Label {label_ref}")
    except Exception as exc:  # noqa: BLE001
        logger.error("PACKING | shipping label generation failed for %s: %s",
                     label_ref, exc)
        raise RuntimeError(f"Failed to generate shipping label for {label_ref}: {exc}") from exc

    logger.info("PACKING | shipping label generated -> %s", pdf_path)
    return {"path": pdf_path}
