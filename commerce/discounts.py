"""
commerce/discounts.py
----------------------
The v7.0 Coupon / Discount engine and Gift-Card ledger for the ME-HAAT
Fashion AI Bot.

This module is *pure logic*: every public function returns a plain ``dict`` (or
list of dicts / ``None``) and **never raises** — invalid input, missing records
and business-rule violations are all reported through the returned payload's
``valid`` / ``ok`` / ``reason`` fields. That contract lets the WhatsApp webhook,
the JSON API and the admin dashboard call in freely without defensive
try/excepts and without ever holding a live ORM session.

Two capabilities live here:

    * **Coupons** — percent or flat discounts with a validity window, a minimum
      order value, a global usage cap and a per-customer cap. Validation is
      side-effect free; :func:`apply_coupon_to_order` is the only mutating entry
      point and it delegates the order-total recompute to
      :class:`commerce.service.OrderService`.
    * **Gift cards** — stored-value cards with a running balance and an
      append-only transaction ledger (``issue`` / ``redeem`` / ``refund``).

Persistence goes through the shared SQLAlchemy engine via
:func:`database.db.session_scope`; Numeric columns come back as ``Decimal`` and
are coerced to ``float`` on the way out so results stay JSON-friendly.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Union

from database.db import session_scope
from database.models import Coupon, CouponRedemption, GiftCard, GiftCardTxn
from commerce.service import order_service
from utils.logging import logger

try:  # config is import-safe, but stay defensive so logic never breaks on it.
    from config import config
except Exception:  # noqa: BLE001 - pragma: no cover
    config = None  # type: ignore[assignment]


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _f(value: Any) -> float:
    """Coerce ``Decimal`` / ``None`` / numeric-ish input to ``float``."""
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _dec(value: Any, default: str = "0") -> Decimal:
    """Coerce arbitrary numeric input to ``Decimal`` (never raises)."""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    """ISO-8601 serialize a datetime (or ``None``)."""
    return dt.isoformat() if isinstance(dt, datetime) else None


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize a possibly-naive datetime to timezone-aware UTC.

    SQLite may return naive datetimes even for ``DateTime(timezone=True)``
    columns; treat those as UTC so window comparisons are always well-defined.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _gen_code(prefix: str) -> str:
    """Generate a short, unguessable uppercase code with a readable prefix.

    Args:
        prefix: A human label (e.g. ``"GIFT"`` or ``"SAVE"``) prepended to the
            random component. Non-alphanumeric characters are stripped.

    Returns:
        A code like ``"GIFT-3F9A2B7C"`` using :func:`secrets.token_hex`.
    """
    clean = "".join(ch for ch in (prefix or "").upper() if ch.isalnum()) or "ME"
    return f"{clean}-{secrets.token_hex(4).upper()}"


def _coupon_to_dict(coupon: Coupon) -> Dict[str, Any]:
    """Serialize a :class:`Coupon` ORM row to a plain, detached ``dict``."""
    return {
        "id": coupon.id,
        "code": coupon.code,
        "kind": coupon.kind,
        "value": _f(coupon.value),
        "min_order": _f(coupon.min_order),
        "max_discount": _f(coupon.max_discount) if coupon.max_discount is not None else None,
        "usage_limit": coupon.usage_limit,
        "used_count": coupon.used_count or 0,
        "per_customer_limit": coupon.per_customer_limit,
        "starts_at": _iso(coupon.starts_at),
        "expires_at": _iso(coupon.expires_at),
        "active": bool(coupon.active),
        "description": coupon.description,
        "created_at": _iso(coupon.created_at),
    }


def _gift_card_to_dict(card: GiftCard) -> Dict[str, Any]:
    """Serialize a :class:`GiftCard` ORM row to a plain, detached ``dict``."""
    return {
        "id": card.id,
        "code": card.code,
        "initial_balance": _f(card.initial_balance),
        "balance": _f(card.balance),
        "currency": card.currency,
        "issued_to": card.issued_to,
        "active": bool(card.active),
        "expires_at": _iso(card.expires_at),
        "created_at": _iso(card.created_at),
    }


def _default_currency() -> str:
    """Best-effort store default currency (falls back to ``INR``)."""
    try:
        return getattr(config, "default_currency", None) or "INR"
    except Exception:  # noqa: BLE001
        return "INR"


def _compute_discount(kind: str, value: Decimal, subtotal: Decimal,
                      max_discount: Optional[Decimal]) -> Decimal:
    """Compute the raw discount amount for a coupon against ``subtotal``.

    Args:
        kind: ``"percent"`` or ``"flat"``.
        value: The coupon value (percentage points, or a flat amount).
        subtotal: The order subtotal to discount.
        max_discount: Optional cap applied to a percentage discount.

    Returns:
        A non-negative :class:`~decimal.Decimal` discount, never exceeding the
        subtotal (flat) or the cap (percent).
    """
    if subtotal <= 0 or value <= 0:
        return Decimal("0")
    if kind == "flat":
        return min(value, subtotal)
    # percent
    discount = subtotal * value / Decimal("100")
    if max_discount is not None and max_discount > 0:
        discount = min(discount, max_discount)
    return min(discount, subtotal)


# --------------------------------------------------------------------------
# Coupon validation & application
# --------------------------------------------------------------------------

def validate_coupon(
    code: str,
    *,
    subtotal: Union[float, Decimal],
    wa_number: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate a coupon code against an order subtotal (side-effect free).

    Runs every business rule — existence, active flag, validity window, minimum
    order value, global usage cap and per-customer cap — and computes the
    discount the coupon *would* grant. Nothing is written.

    Args:
        code: The coupon code (case-insensitive, whitespace-trimmed).
        subtotal: The order subtotal to test against.
        wa_number: The customer's WhatsApp number, used to enforce
            ``per_customer_limit``. When ``None`` the per-customer cap is
            skipped.

    Returns:
        A dict ``{valid, reason, coupon, discount}`` where ``coupon`` is the
        serialized coupon (or ``None``) and ``discount`` is a ``float``.
    """
    result: Dict[str, Any] = {"valid": False, "reason": "", "coupon": None, "discount": 0.0}

    norm = (code or "").strip().upper()
    if not norm:
        result["reason"] = "No coupon code supplied."
        return result

    sub = _dec(subtotal)

    try:
        with session_scope() as db:
            coupon = db.query(Coupon).filter(Coupon.code == norm).first()
            if coupon is None:
                result["reason"] = "Coupon not found."
                return result

            result["coupon"] = _coupon_to_dict(coupon)

            if not coupon.active:
                result["reason"] = "This coupon is no longer active."
                return result

            now = _utcnow()
            starts_at = _aware(coupon.starts_at)
            expires_at = _aware(coupon.expires_at)
            if starts_at is not None and now < starts_at:
                result["reason"] = "This coupon is not yet valid."
                return result
            if expires_at is not None and now > expires_at:
                result["reason"] = "This coupon has expired."
                return result

            min_order = _dec(coupon.min_order)
            if min_order > 0 and sub < min_order:
                result["reason"] = (
                    f"Order subtotal must be at least {_f(min_order):.2f} "
                    f"to use this coupon."
                )
                return result

            if coupon.usage_limit is not None and (coupon.used_count or 0) >= coupon.usage_limit:
                result["reason"] = "This coupon has reached its usage limit."
                return result

            if coupon.per_customer_limit is not None and wa_number:
                used_by_customer = (
                    db.query(CouponRedemption)
                    .filter(
                        CouponRedemption.coupon_id == coupon.id,
                        CouponRedemption.wa_number == wa_number,
                    )
                    .count()
                )
                if used_by_customer >= coupon.per_customer_limit:
                    result["reason"] = "You have already used this coupon."
                    return result

            discount = _compute_discount(
                coupon.kind,
                _dec(coupon.value),
                sub,
                _dec(coupon.max_discount) if coupon.max_discount is not None else None,
            )
            if discount <= 0:
                result["reason"] = "This coupon yields no discount on this order."
                return result

            result["valid"] = True
            result["reason"] = "Coupon valid."
            result["discount"] = _f(discount)
            return result
    except Exception as exc:  # noqa: BLE001 - never raise from pure logic
        logger.error("DISCOUNTS | validate_coupon(%r) failed: %s", norm, exc)
        result["reason"] = "Could not validate this coupon right now."
        return result


def apply_coupon_to_order(
    code: str,
    order_id: int,
    *,
    actor: str = "system",
) -> Dict[str, Any]:
    """Validate and apply a coupon to an existing order, recomputing its total.

    The order's subtotal and WhatsApp number are read via
    :func:`commerce.service.OrderService.get_order`. On success a
    :class:`CouponRedemption` row is recorded, the coupon's ``used_count`` is
    incremented, and ``order_service.update_order_fields`` is called with the
    new ``discount`` so the order total is recomputed atomically.

    Args:
        code: The coupon code to apply.
        order_id: The target order's primary key.
        actor: Who is applying the coupon (for the audit trail).

    Returns:
        A dict ``{ok, discount, message, coupon}``. ``ok`` is ``False`` (with a
        human ``message``) when the order is missing or validation fails.
    """
    order = None
    try:
        order = order_service.get_order(order_id=order_id, include_items=False)
    except Exception as exc:  # noqa: BLE001
        logger.error("DISCOUNTS | apply_coupon load order #%s failed: %s", order_id, exc)

    if order is None:
        return {"ok": False, "discount": 0.0, "message": "Order not found.", "coupon": None}

    check = validate_coupon(
        code,
        subtotal=order.get("subtotal", 0),
        wa_number=order.get("wa_number"),
    )
    if not check["valid"]:
        return {
            "ok": False,
            "discount": 0.0,
            "message": check["reason"],
            "coupon": check["coupon"],
        }

    coupon = check["coupon"]
    discount = check["discount"]

    try:
        with session_scope() as db:
            row = db.query(Coupon).filter(Coupon.id == coupon["id"]).first()
            if row is None:
                return {"ok": False, "discount": 0.0,
                        "message": "Coupon not found.", "coupon": None}
            db.add(
                CouponRedemption(
                    coupon_id=row.id,
                    code=row.code,
                    order_id=order_id,
                    wa_number=order.get("wa_number"),
                    amount=_dec(discount),
                )
            )
            row.used_count = (row.used_count or 0) + 1
    except Exception as exc:  # noqa: BLE001
        logger.error("DISCOUNTS | record redemption for order #%s failed: %s", order_id, exc)
        return {"ok": False, "discount": 0.0,
                "message": "Could not apply the coupon.", "coupon": coupon}

    try:
        order_service.update_order_fields(order_id, discount=_dec(discount), actor=actor)
    except Exception as exc:  # noqa: BLE001
        logger.error("DISCOUNTS | recompute total for order #%s failed: %s", order_id, exc)

    logger.info(
        "DISCOUNTS | Applied coupon %s to order #%s (discount %.2f)",
        coupon["code"], order_id, discount,
    )
    return {
        "ok": True,
        "discount": discount,
        "message": f"Applied {coupon['code']} — you saved {discount:.2f}.",
        "coupon": coupon,
    }


# --------------------------------------------------------------------------
# Coupon CRUD
# --------------------------------------------------------------------------

_COUPON_FIELDS = {
    "code", "kind", "value", "min_order", "max_discount", "usage_limit",
    "per_customer_limit", "starts_at", "expires_at", "active", "description",
}


def create_coupon(**fields: Any) -> Dict[str, Any]:
    """Create a coupon from keyword fields and return it serialized.

    Recognized fields mirror the :class:`Coupon` model: ``code``, ``kind``
    (``"percent"``/``"flat"``), ``value``, ``min_order``, ``max_discount``,
    ``usage_limit``, ``per_customer_limit``, ``starts_at``, ``expires_at``,
    ``active``, ``description``. A ``code`` is generated when absent.

    Returns:
        The created coupon dict, or ``{"error": ...}`` on failure (e.g. a
        duplicate code) — this function never raises.
    """
    code = str(fields.get("code") or "").strip().upper() or _gen_code("SAVE")
    kind = str(fields.get("kind") or "percent").strip().lower()
    if kind not in {"percent", "flat"}:
        kind = "percent"

    try:
        with session_scope() as db:
            if db.query(Coupon).filter(Coupon.code == code).first() is not None:
                return {"error": f"Coupon code already exists: {code!r}"}
            coupon = Coupon(
                code=code,
                kind=kind,
                value=_dec(fields.get("value", 0)),
                min_order=_dec(fields.get("min_order", 0)),
                max_discount=(
                    _dec(fields["max_discount"])
                    if fields.get("max_discount") not in (None, "")
                    else None
                ),
                usage_limit=_int_or_none(fields.get("usage_limit")),
                used_count=0,
                per_customer_limit=_int_or_none(fields.get("per_customer_limit")),
                starts_at=_aware(fields.get("starts_at")),
                expires_at=_aware(fields.get("expires_at")),
                active=bool(fields.get("active", True)),
                description=(str(fields["description"]).strip()
                            if fields.get("description") else None),
            )
            db.add(coupon)
            db.flush()
            result = _coupon_to_dict(coupon)
        logger.info("DISCOUNTS | Created coupon %s (%s %s)", code, kind, result["value"])
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error("DISCOUNTS | create_coupon(%r) failed: %s", code, exc)
        return {"error": f"Could not create coupon: {exc}"}


def list_coupons(active: Optional[bool] = None) -> List[Dict[str, Any]]:
    """List coupons (newest first), optionally filtered by ``active`` flag.

    Args:
        active: ``True``/``False`` to filter, or ``None`` for all coupons.

    Returns:
        A list of serialized coupon dicts (empty on error).
    """
    try:
        with session_scope() as db:
            q = db.query(Coupon)
            if active is not None:
                q = q.filter(Coupon.active == bool(active))
            q = q.order_by(Coupon.id.desc())
            return [_coupon_to_dict(c) for c in q.all()]
    except Exception as exc:  # noqa: BLE001
        logger.error("DISCOUNTS | list_coupons failed: %s", exc)
        return []


def get_coupon(code_or_id: Union[str, int]) -> Optional[Dict[str, Any]]:
    """Fetch a single coupon by numeric id or by code.

    Args:
        code_or_id: An integer/string id, or a coupon code.

    Returns:
        The serialized coupon dict, or ``None`` if not found.
    """
    try:
        with session_scope() as db:
            coupon = _resolve_coupon(db, code_or_id)
            return _coupon_to_dict(coupon) if coupon is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.error("DISCOUNTS | get_coupon(%r) failed: %s", code_or_id, exc)
        return None


def update_coupon(coupon_id: int, **fields: Any) -> Dict[str, Any]:
    """Update an existing coupon's fields and return it serialized.

    Only recognized, non-``None`` fields are applied. The ``code`` is
    normalized to uppercase; ``value``/``min_order``/``max_discount`` are
    coerced to ``Decimal``; ``usage_limit``/``per_customer_limit`` to ``int``.

    Args:
        coupon_id: Target coupon primary key.
        **fields: Any subset of the writable coupon fields.

    Returns:
        The updated coupon dict, or ``{"error": ...}`` if not found / on failure.
    """
    try:
        with session_scope() as db:
            coupon = db.get(Coupon, coupon_id)
            if coupon is None:
                return {"error": "Coupon not found."}
            for key, value in fields.items():
                if key not in _COUPON_FIELDS or value is None:
                    continue
                if key == "code":
                    coupon.code = str(value).strip().upper()
                elif key == "kind":
                    kind = str(value).strip().lower()
                    coupon.kind = kind if kind in {"percent", "flat"} else coupon.kind
                elif key in {"value", "min_order"}:
                    setattr(coupon, key, _dec(value))
                elif key == "max_discount":
                    coupon.max_discount = _dec(value) if value != "" else None
                elif key in {"usage_limit", "per_customer_limit"}:
                    setattr(coupon, key, _int_or_none(value))
                elif key in {"starts_at", "expires_at"}:
                    setattr(coupon, key, _aware(value) if value != "" else None)
                elif key == "active":
                    coupon.active = bool(value)
                elif key == "description":
                    coupon.description = str(value).strip() or None
            db.flush()
            result = _coupon_to_dict(coupon)
        logger.info("DISCOUNTS | Updated coupon #%s", coupon_id)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error("DISCOUNTS | update_coupon(#%s) failed: %s", coupon_id, exc)
        return {"error": f"Could not update coupon: {exc}"}


def deactivate_coupon(coupon_id: int) -> Dict[str, Any]:
    """Deactivate a coupon (soft disable).

    Args:
        coupon_id: Target coupon primary key.

    Returns:
        ``{ok, message}`` — ``ok`` is ``False`` when the coupon is missing.
    """
    try:
        with session_scope() as db:
            coupon = db.get(Coupon, coupon_id)
            if coupon is None:
                return {"ok": False, "message": "Coupon not found."}
            coupon.active = False
            code = coupon.code
        logger.info("DISCOUNTS | Deactivated coupon #%s (%s)", coupon_id, code)
        return {"ok": True, "message": f"Coupon {code} deactivated."}
    except Exception as exc:  # noqa: BLE001
        logger.error("DISCOUNTS | deactivate_coupon(#%s) failed: %s", coupon_id, exc)
        return {"ok": False, "message": f"Could not deactivate coupon: {exc}"}


# --------------------------------------------------------------------------
# Gift cards
# --------------------------------------------------------------------------

def issue_gift_card(
    amount: Union[float, Decimal],
    *,
    currency: str = "INR",
    issued_to: Optional[str] = None,
    code: Optional[str] = None,
) -> Dict[str, Any]:
    """Issue a new stored-value gift card and open its ledger.

    Creates a :class:`GiftCard` with ``balance == initial_balance == amount``
    and an opening :class:`GiftCardTxn` of kind ``"issue"``.

    Args:
        amount: The card's starting value (must be positive).
        currency: ISO currency code (defaults to ``"INR"``).
        issued_to: Optional WhatsApp number the card is issued to.
        code: Optional explicit code; a unique one is generated when omitted.

    Returns:
        The serialized gift-card dict, or ``{"error": ...}`` on failure.
    """
    value = _dec(amount)
    if value <= 0:
        return {"error": "Gift-card amount must be positive."}

    card_code = (code or "").strip().upper() or _gen_code("GIFT")
    try:
        with session_scope() as db:
            if db.query(GiftCard).filter(GiftCard.code == card_code).first() is not None:
                return {"error": f"Gift-card code already exists: {card_code!r}"}
            card = GiftCard(
                code=card_code,
                initial_balance=value,
                balance=value,
                currency=(currency or _default_currency()).strip().upper(),
                issued_to=(issued_to or None),
                active=True,
            )
            db.add(card)
            db.flush()
            db.add(
                GiftCardTxn(
                    gift_card_id=card.id,
                    order_id=None,
                    kind="issue",
                    amount=value,
                    balance_after=value,
                )
            )
            result = _gift_card_to_dict(card)
        logger.info("DISCOUNTS | Issued gift card %s (%.2f %s)",
                    card_code, _f(value), result["currency"])
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error("DISCOUNTS | issue_gift_card(%r) failed: %s", card_code, exc)
        return {"error": f"Could not issue gift card: {exc}"}


def check_gift_card(code: str) -> Optional[Dict[str, Any]]:
    """Look up a gift card's status and remaining balance.

    Args:
        code: The gift-card code (case-insensitive).

    Returns:
        A dict of the card plus a derived ``usable`` flag and ``expired``
        indicator, or ``None`` if no such card exists.
    """
    norm = (code or "").strip().upper()
    if not norm:
        return None
    try:
        with session_scope() as db:
            card = db.query(GiftCard).filter(GiftCard.code == norm).first()
            if card is None:
                return None
            data = _gift_card_to_dict(card)
            expires_at = _aware(card.expires_at)
            expired = expires_at is not None and _utcnow() > expires_at
            data["expired"] = expired
            data["usable"] = bool(card.active) and not expired and _f(card.balance) > 0
            return data
    except Exception as exc:  # noqa: BLE001
        logger.error("DISCOUNTS | check_gift_card(%r) failed: %s", norm, exc)
        return None


def redeem_gift_card(
    code: str,
    amount: Union[float, Decimal],
    *,
    order_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Redeem value from a gift card, appending a ledger entry.

    Guards against inactive cards, expired cards, non-positive amounts and
    insufficient balance. On success the card balance is decremented and a
    :class:`GiftCardTxn` of kind ``"redeem"`` is recorded.

    Args:
        code: The gift-card code.
        amount: The amount to redeem (must be positive and <= balance).
        order_id: Optional order this redemption is attached to.

    Returns:
        A dict ``{ok, redeemed, balance_after, message}``.
    """
    norm = (code or "").strip().upper()
    want = _dec(amount)
    if not norm:
        return {"ok": False, "redeemed": 0.0, "balance_after": 0.0,
                "message": "No gift-card code supplied."}
    if want <= 0:
        return {"ok": False, "redeemed": 0.0, "balance_after": 0.0,
                "message": "Redemption amount must be positive."}

    try:
        with session_scope() as db:
            card = db.query(GiftCard).filter(GiftCard.code == norm).first()
            if card is None:
                return {"ok": False, "redeemed": 0.0, "balance_after": 0.0,
                        "message": "Gift card not found."}
            if not card.active:
                return {"ok": False, "redeemed": 0.0, "balance_after": _f(card.balance),
                        "message": "This gift card is inactive."}
            expires_at = _aware(card.expires_at)
            if expires_at is not None and _utcnow() > expires_at:
                return {"ok": False, "redeemed": 0.0, "balance_after": _f(card.balance),
                        "message": "This gift card has expired."}
            balance = _dec(card.balance)
            if want > balance:
                return {"ok": False, "redeemed": 0.0, "balance_after": _f(balance),
                        "message": "Insufficient gift-card balance."}
            new_balance = balance - want
            card.balance = new_balance
            db.add(
                GiftCardTxn(
                    gift_card_id=card.id,
                    order_id=order_id,
                    kind="redeem",
                    amount=want,
                    balance_after=new_balance,
                )
            )
            after = _f(new_balance)
        logger.info("DISCOUNTS | Redeemed %.2f from gift card %s (balance %.2f)",
                    _f(want), norm, after)
        return {
            "ok": True,
            "redeemed": _f(want),
            "balance_after": after,
            "message": f"Redeemed {_f(want):.2f}. Remaining balance {after:.2f}.",
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("DISCOUNTS | redeem_gift_card(%r) failed: %s", norm, exc)
        return {"ok": False, "redeemed": 0.0, "balance_after": 0.0,
                "message": "Could not redeem the gift card."}


def list_gift_cards() -> List[Dict[str, Any]]:
    """List all gift cards, newest first.

    Returns:
        A list of serialized gift-card dicts (empty on error).
    """
    try:
        with session_scope() as db:
            cards = db.query(GiftCard).order_by(GiftCard.id.desc()).all()
            return [_gift_card_to_dict(c) for c in cards]
    except Exception as exc:  # noqa: BLE001
        logger.error("DISCOUNTS | list_gift_cards failed: %s", exc)
        return []


# --------------------------------------------------------------------------
# Internal resolvers
# --------------------------------------------------------------------------

def _int_or_none(value: Any) -> Optional[int]:
    """Coerce to ``int`` or ``None`` (blank / invalid -> ``None``)."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _resolve_coupon(db: Any, code_or_id: Union[str, int]) -> Optional[Coupon]:
    """Resolve a coupon by numeric id, else by (uppercased) code."""
    if isinstance(code_or_id, int):
        return db.get(Coupon, code_or_id)
    text = str(code_or_id).strip()
    if text.isdigit():
        return db.get(Coupon, int(text))
    return db.query(Coupon).filter(Coupon.code == text.upper()).first()
