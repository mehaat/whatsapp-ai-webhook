"""
tests/test_v6_invoices.py
-------------------------
Unit tests for the v6.0 PDF invoice generator (``commerce/invoices.py``).

These tests exercise the full ReportLab + qrcode render path without touching a
database or network: ``next_invoice_number``, ``session_scope`` and the lazily
imported ``order_service.record_invoice`` are all monkeypatched out, so the only
real work is building and writing the PDF.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

import commerce.invoices as invoices


def _make_order() -> dict:
    """Return a representative v6 order dict for invoicing."""
    return {
        "id": 101,
        "order_number": "MH-2026-000042",
        "wa_number": "+919812345678",
        "customer_name": "Aditi Sharma",
        "currency": "INR",
        "subtotal": 4500.0,
        "discount": 250.0,
        "shipping": 60.0,
        "tax": 210.0,
        "total_amount": 4520.0,
        "checkout_url": "https://mehaatfaishon.com/checkout/abc123",
        "city": "Jaipur",
        "state": "Rajasthan",
        "created_at": "2026-07-12T10:30:00+00:00",
        "items": [
            {
                "product_name": "Bandhani Silk Saree",
                "variant": "Maroon / Free Size",
                "product_retailer_id": "SAR-001",
                "quantity": 1,
                "unit_price": 3200.0,
                "line_total": 3200.0,
                "currency": "INR",
            },
            {
                "product_name": "Kundan Jhumka Earrings",
                "variant": "Gold",
                "product_retailer_id": "JWL-014",
                "quantity": 2,
                "unit_price": 650.0,
                "line_total": 1300.0,
                "currency": "INR",
            },
        ],
    }


@pytest.fixture
def patched_invoices(monkeypatch, tmp_path):
    """Patch out DB/number/service deps and point output at a temp dir."""
    fake_config = SimpleNamespace(
        invoice_output_dir=str(tmp_path),
        business_name="ME-HAAT Fashion",
        business_address="",
        business_gstin="",
        business_phone="",
        business_email="",
        business_website="",
        invoice_logo_path="",
        default_currency="INR",
    )
    monkeypatch.setattr(invoices, "config", fake_config)
    monkeypatch.setattr(invoices, "next_invoice_number", lambda session: "INV-TEST-0001")

    @contextlib.contextmanager
    def _dummy_scope():
        yield None

    monkeypatch.setattr(invoices, "session_scope", _dummy_scope)

    # The lazily imported record_invoice must not touch a database.
    import commerce.service as service_module

    monkeypatch.setattr(
        service_module.order_service, "record_invoice",
        lambda *args, **kwargs: {}, raising=True,
    )
    return fake_config


def test_generate_invoice_writes_valid_pdf(patched_invoices):
    """A full order produces a real, non-empty PDF and the minted number."""
    result = invoices.generate_invoice(_make_order())

    assert result["invoice_number"] == "INV-TEST-0001"
    assert result["total"] == pytest.approx(4520.0)

    pdf_path = result["pdf_path"]
    import os

    assert os.path.isfile(pdf_path)
    assert os.path.getsize(pdf_path) > 0
    with open(pdf_path, "rb") as handle:
        assert handle.read(4) == b"%PDF"


def test_generate_invoice_handles_blank_optionals(patched_invoices):
    """Missing checkout URL, city/state and empty items still render a PDF."""
    order = _make_order()
    order["checkout_url"] = ""
    order["city"] = ""
    order["state"] = ""
    order["items"] = []
    order["total_amount"] = ""  # force computed fallback

    result = invoices.generate_invoice(order)

    import os

    assert os.path.isfile(result["pdf_path"])
    with open(result["pdf_path"], "rb") as handle:
        assert handle.read(4) == b"%PDF"


def test_currency_symbol_mapping():
    """Known codes map to symbols; unknown falls back to the code."""
    assert invoices._currency_symbol("INR") == "₹"
    assert invoices._currency_symbol("USD") == "$"
    assert invoices._currency_symbol("EUR") == "€"
    assert invoices._currency_symbol("GBP") == "£"
    assert invoices._currency_symbol("JPY") == "JPY"
    assert invoices._currency_symbol("") == ""
