"""
tests/test_v7_discounts.py
---------------------------
Tests for the v7.0 Coupon / Discount engine and Gift-Card ledger
(``commerce/discounts.py``).

Persistence uses the default SQLite database bootstrapped through
:func:`commerce.bootstrap`; no network is touched. Each test mints a unique
coupon / gift-card code (via :func:`secrets.token_hex`) so runs stay
independent of any pre-existing rows.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:///mehaat.db")

import commerce
from commerce import discounts

commerce.bootstrap()


def _code(prefix: str) -> str:
    """Return a unique, test-scoped coupon/gift-card code."""
    return f"{prefix}-{secrets.token_hex(4).upper()}"


# --------------------------------------------------------------------------
# Coupons
# --------------------------------------------------------------------------

def test_percent_coupon_valid_and_discount():
    code = _code("PCT")
    created = discounts.create_coupon(
        code=code, kind="percent", value=10, min_order=1000
    )
    assert "error" not in created
    assert created["code"] == code

    result = discounts.validate_coupon(code, subtotal=2000)
    assert result["valid"] is True
    assert result["discount"] == 200.0


def test_percent_coupon_below_min_order_invalid():
    code = _code("MIN")
    discounts.create_coupon(code=code, kind="percent", value=10, min_order=1000)

    result = discounts.validate_coupon(code, subtotal=500)
    assert result["valid"] is False
    assert "min" in result["reason"].lower() or "least" in result["reason"].lower()
    assert result["discount"] == 0.0


def test_expired_coupon_invalid():
    code = _code("EXP")
    past = datetime.now(timezone.utc) - timedelta(days=1)
    discounts.create_coupon(
        code=code, kind="percent", value=10, min_order=0, expires_at=past
    )

    result = discounts.validate_coupon(code, subtotal=2000)
    assert result["valid"] is False
    assert "expired" in result["reason"].lower()


def test_flat_coupon_caps_at_subtotal():
    code = _code("FLAT")
    discounts.create_coupon(code=code, kind="flat", value=500, min_order=0)

    # Flat value exceeds the subtotal -> discount capped at the subtotal.
    result = discounts.validate_coupon(code, subtotal=300)
    assert result["valid"] is True
    assert result["discount"] == 300.0

    # Subtotal above the flat value -> discount is the flat value.
    result2 = discounts.validate_coupon(code, subtotal=800)
    assert result2["valid"] is True
    assert result2["discount"] == 500.0


def test_unknown_coupon_invalid():
    result = discounts.validate_coupon(_code("NOPE"), subtotal=2000)
    assert result["valid"] is False
    assert result["coupon"] is None


def test_inactive_coupon_invalid():
    code = _code("OFF")
    created = discounts.create_coupon(code=code, kind="percent", value=10, min_order=0)
    discounts.deactivate_coupon(created["id"])

    result = discounts.validate_coupon(code, subtotal=2000)
    assert result["valid"] is False


def test_percent_coupon_max_discount_cap():
    code = _code("CAP")
    discounts.create_coupon(
        code=code, kind="percent", value=50, min_order=0, max_discount=100
    )

    result = discounts.validate_coupon(code, subtotal=1000)
    assert result["valid"] is True
    # 50% of 1000 = 500, capped at 100.
    assert result["discount"] == 100.0


# --------------------------------------------------------------------------
# Gift cards
# --------------------------------------------------------------------------

def test_gift_card_issue_redeem_and_insufficient():
    code = _code("GC")
    card = discounts.issue_gift_card(500, code=code)
    assert "error" not in card
    assert card["balance"] == 500.0

    # Redeem 200 -> balance 300.
    redeemed = discounts.redeem_gift_card(code, 200)
    assert redeemed["ok"] is True
    assert redeemed["redeemed"] == 200.0
    assert redeemed["balance_after"] == 300.0

    # Redeem 400 from a 300 balance -> insufficient.
    over = discounts.redeem_gift_card(code, 400)
    assert over["ok"] is False
    assert "insufficient" in over["message"].lower()
    assert over["balance_after"] == 300.0

    # check_gift_card reflects the remaining balance.
    status = discounts.check_gift_card(code)
    assert status is not None
    assert status["balance"] == 300.0
    assert status["usable"] is True


def test_gift_card_unknown_returns_none():
    assert discounts.check_gift_card(_code("MISS")) is None
