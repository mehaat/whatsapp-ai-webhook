"""
commerce/service.py
--------------------
The v6.0 order lifecycle service — the single source of truth for orders,
line items, payments, tracking, invoices, notifications, and the audit trail.

Everything returns plain ``dict`` structures (Decimals coerced to float,
datetimes to ISO-8601), so callers — the WhatsApp webhook, the JSON API, the
notification layer, the invoice generator and the admin dashboard — never hold
a live ORM session and can freely serialize results.

Persistence uses the shared SQLAlchemy engine (SQLite by default, PostgreSQL
via ``DATABASE_URL``). All writes go through ``session_scope`` for atomic
commit/rollback.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from config import config
from utils.logging import logger

from commerce.numbering import next_order_number
from commerce.schema import ParsedOrder

# Status transitions that also move fulfilment/payment state.
_STATUS_FULFILLMENT = {
    "confirmed": None,
    "packed": "packed",
    "shipped": "shipped",
    "out_for_delivery": "out_for_delivery",
    "delivered": "fulfilled",
}


def _f(value: Any) -> float:
    """Coerce Decimal/None to float for JSON output."""
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if isinstance(dt, datetime) else None


def _metric(name: str, amount: float = 1.0) -> None:
    """Best-effort Prometheus counter increment (never raises)."""
    try:
        from utils.observability import incr

        incr(name, amount)
    except Exception:  # noqa: BLE001
        pass


def _order_to_dict(order, items=None) -> Dict[str, Any]:
    data = {
        "id": order.id,
        "order_number": order.order_number,
        "wa_number": order.wa_number,
        "customer_name": order.customer_name,
        "language": order.language,
        "wa_order_id": order.wa_order_id,
        "catalog_id": order.catalog_id,
        "currency": order.currency,
        "subtotal": _f(order.subtotal),
        "discount": _f(order.discount),
        "shipping": _f(order.shipping),
        "tax": _f(order.tax),
        "total_amount": _f(order.total_amount),
        "status": order.status,
        "payment_status": order.payment_status,
        "fulfillment_status": order.fulfillment_status,
        "shopify_draft_order_id": order.shopify_draft_order_id,
        "shopify_order_id": order.shopify_order_id,
        "checkout_url": order.checkout_url,
        "invoice_url": order.invoice_url,
        "courier": order.courier,
        "tracking_number": order.tracking_number,
        "city": order.city,
        "state": order.state,
        "notes": order.notes,
        "created_at": _iso(order.created_at),
        "updated_at": _iso(order.updated_at),
    }
    if items is not None:
        data["items"] = [_item_to_dict(i) for i in items]
    return data


def _item_to_dict(item) -> Dict[str, Any]:
    return {
        "id": item.id,
        "product_retailer_id": item.product_retailer_id,
        "product_id": item.product_id,
        "variant_id": item.variant_id,
        "product_name": item.product_name,
        "variant": item.variant,
        "quantity": item.quantity,
        "unit_price": _f(item.unit_price),
        "currency": item.currency,
        "line_total": _f(item.line_total),
    }


def _tracking_to_dict(t) -> Dict[str, Any]:
    return {
        "id": t.id,
        "status": t.status,
        "courier": t.courier,
        "tracking_number": t.tracking_number,
        "location": t.location,
        "note": t.note,
        "created_at": _iso(t.created_at),
    }


class OrderService:
    """High-level, session-managed API over the commerce tables."""

    # -- creation ---------------------------------------------------------

    def create_order(
        self,
        parsed: ParsedOrder,
        *,
        discount: Optional[Decimal] = None,
        shipping: Optional[Decimal] = None,
        tax: Optional[Decimal] = None,
        status: str = "received",
        actor: str = "whatsapp",
    ) -> Dict[str, Any]:
        """Persist a parsed WhatsApp order and return its serialized form."""
        from database.db import session_scope
        from database.models import Order, OrderItem

        subtotal = parsed.subtotal
        discount = discount if discount is not None else Decimal(str(config.shipping_flat and 0 or 0))
        discount = discount or Decimal("0")
        shipping = shipping if shipping is not None else Decimal(str(config.shipping_flat or 0))
        if tax is None:
            rate = Decimal(str(config.tax_rate_percent or 0)) / Decimal("100")
            tax = (subtotal - discount) * rate
        total = subtotal - discount + shipping + tax

        # v8.0: tag the order with the resolved tenant (default when single-store).
        tenant_id = None
        try:
            from commerce.tenancy import current_tenant_id

            tenant_id = current_tenant_id()
        except Exception:  # noqa: BLE001
            tenant_id = None

        with session_scope() as session:
            number = next_order_number(session)
            order = Order(
                order_number=number,
                tenant_id=tenant_id,
                wa_number=parsed.wa_number,
                customer_name=parsed.customer_name,
                language=parsed.language,
                wa_order_id=parsed.wa_order_id,
                catalog_id=parsed.catalog_id,
                currency=parsed.currency or config.default_currency,
                subtotal=subtotal,
                discount=discount,
                shipping=shipping,
                tax=tax,
                total_amount=total,
                status=status,
                payment_status="pending",
                fulfillment_status="unfulfilled",
                notes=parsed.note,
            )
            session.add(order)
            session.flush()

            for it in parsed.items:
                session.add(
                    OrderItem(
                        order_id=order.id,
                        product_retailer_id=it.product_retailer_id,
                        product_id=it.product_id,
                        variant_id=it.variant_id,
                        product_name=it.product_name,
                        variant=it.variant,
                        quantity=it.quantity,
                        unit_price=it.unit_price,
                        currency=it.currency,
                        line_total=it.line_total,
                    )
                )
            session.flush()
            self._audit(session, actor, "order.create", "order", str(order.id),
                        f"{number} total={_f(total)} {order.currency}")
            items = session.query(OrderItem).filter_by(order_id=order.id).all()
            result = _order_to_dict(order, items)

        logger.info("COMMERCE | Created order %s for %s (total %.2f %s)",
                    number, parsed.wa_number, _f(total), result["currency"])
        return result

    # -- reads ------------------------------------------------------------

    def get_order(
        self, *, order_id: Optional[int] = None, order_number: Optional[str] = None,
        include_items: bool = True, include_tracking: bool = False,
    ) -> Optional[Dict[str, Any]]:
        from database.db import session_scope
        from database.models import Order, OrderItem, Tracking

        with session_scope() as session:
            order = self._resolve(session, order_id, order_number)
            if order is None:
                return None
            items = session.query(OrderItem).filter_by(order_id=order.id).all() \
                if include_items else None
            data = _order_to_dict(order, items)
            if include_tracking:
                events = session.query(Tracking).filter_by(order_id=order.id) \
                    .order_by(Tracking.created_at.asc(), Tracking.id.asc()).all()
                data["tracking"] = [_tracking_to_dict(e) for e in events]
            return data

    def list_orders(
        self,
        *,
        status: Optional[str] = None,
        payment_status: Optional[str] = None,
        query: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        tenant_id: Optional[int] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        from database.db import session_scope
        from database.models import Order

        with session_scope() as session:
            q = session.query(Order)
            q = self._apply_filters(
                q, Order, status, payment_status, query, date_from, date_to, tenant_id
            )
            q = q.order_by(Order.created_at.desc(), Order.id.desc()).limit(limit).offset(offset)
            return [_order_to_dict(o) for o in q.all()]

    def count_orders(
        self, *, status: Optional[str] = None, payment_status: Optional[str] = None,
        query: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
        tenant_id: Optional[int] = None,
    ) -> int:
        from database.db import session_scope
        from database.models import Order

        with session_scope() as session:
            q = session.query(Order)
            q = self._apply_filters(
                q, Order, status, payment_status, query, date_from, date_to, tenant_id
            )
            return q.count()

    def latest_order_for(self, wa_number: str) -> Optional[Dict[str, Any]]:
        from database.db import session_scope
        from database.models import Order, Tracking, OrderItem

        with session_scope() as session:
            order = session.query(Order).filter_by(wa_number=wa_number) \
                .order_by(Order.created_at.desc(), Order.id.desc()).first()
            if order is None:
                return None
            items = session.query(OrderItem).filter_by(order_id=order.id).all()
            data = _order_to_dict(order, items)
            events = session.query(Tracking).filter_by(order_id=order.id) \
                .order_by(Tracking.created_at.asc(), Tracking.id.asc()).all()
            data["tracking"] = [_tracking_to_dict(e) for e in events]
            return data

    def get_tracking(self, order_id: int) -> List[Dict[str, Any]]:
        from database.db import session_scope
        from database.models import Tracking

        with session_scope() as session:
            events = session.query(Tracking).filter_by(order_id=order_id) \
                .order_by(Tracking.created_at.asc(), Tracking.id.asc()).all()
            return [_tracking_to_dict(e) for e in events]

    # -- updates ----------------------------------------------------------

    def set_status(
        self,
        order_id: int,
        status: str,
        *,
        actor: str = "admin",
        courier: Optional[str] = None,
        tracking_number: Optional[str] = None,
        location: Optional[str] = None,
        note: Optional[str] = None,
        ip: Optional[str] = None,
        add_tracking: bool = True,
    ) -> Optional[Dict[str, Any]]:
        from database.db import session_scope
        from database.models import Order, Tracking

        status = (status or "").strip().lower()
        with session_scope() as session:
            order = session.get(Order, order_id)
            if order is None:
                return None
            order.status = status
            if status in _STATUS_FULFILLMENT and _STATUS_FULFILLMENT[status]:
                order.fulfillment_status = _STATUS_FULFILLMENT[status]
            if status == "refunded":
                order.payment_status = "refunded"
            if courier:
                order.courier = courier
            if tracking_number:
                order.tracking_number = tracking_number
            if add_tracking:
                session.add(Tracking(
                    order_id=order.id, status=status, courier=order.courier,
                    tracking_number=order.tracking_number, location=location, note=note,
                ))
            self._audit(session, actor, "order.status", "order", str(order.id), status)
            data = _order_to_dict(order)
        # Apply inventory-reservation side effects AFTER the transaction closes
        # (separate session avoids nested-lock issues on SQLite).
        self._apply_reservation_side_effects(order_id, status)
        logger.info("COMMERCE | Order %s -> %s", data["order_number"], status)
        return data

    def set_payment_status(
        self, order_id: int, payment_status: str, *, actor: str = "system",
    ) -> Optional[Dict[str, Any]]:
        from database.db import session_scope
        from database.models import Order

        with session_scope() as session:
            order = session.get(Order, order_id)
            if order is None:
                return None
            order.payment_status = (payment_status or "").strip().lower()
            self._audit(session, actor, "order.payment_status", "order",
                        str(order.id), order.payment_status)
            return _order_to_dict(order)

    def update_order_fields(
        self, order_id: int, *, actor: str = "admin", ip: Optional[str] = None, **fields,
    ) -> Optional[Dict[str, Any]]:
        """Generic guarded field update (used by admin edits and the API)."""
        from database.db import session_scope
        from database.models import Order

        allowed = {
            "customer_name", "courier", "tracking_number", "city", "state",
            "notes", "discount", "shipping", "tax", "fulfillment_status",
        }
        with session_scope() as session:
            order = session.get(Order, order_id)
            if order is None:
                return None
            changed = []
            for key, value in fields.items():
                if key in allowed and value is not None:
                    setattr(order, key, value)
                    changed.append(key)
            if {"discount", "shipping", "tax"} & set(changed):
                order.total_amount = (
                    Decimal(str(order.subtotal)) - Decimal(str(order.discount or 0))
                    + Decimal(str(order.shipping or 0)) + Decimal(str(order.tax or 0))
                )
            self._audit(session, actor, "order.update", "order", str(order.id),
                        ",".join(changed), ip)
            return _order_to_dict(order)

    def set_shopify_links(
        self, order_id: int, *, draft_order_id: Optional[str] = None,
        shopify_order_id: Optional[str] = None, checkout_url: Optional[str] = None,
        invoice_url: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        from database.db import session_scope
        from database.models import Order

        with session_scope() as session:
            order = session.get(Order, order_id)
            if order is None:
                return None
            if draft_order_id:
                order.shopify_draft_order_id = draft_order_id
            if shopify_order_id:
                order.shopify_order_id = shopify_order_id
            if checkout_url:
                order.checkout_url = checkout_url
            if invoice_url:
                order.invoice_url = invoice_url
            return _order_to_dict(order)

    def add_tracking(
        self, order_id: int, status: str, *, courier: Optional[str] = None,
        tracking_number: Optional[str] = None, location: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        return self.set_status(
            order_id, status, actor="tracking", courier=courier,
            tracking_number=tracking_number, location=location, note=note,
        )

    # -- payments ---------------------------------------------------------

    def record_payment(
        self, order_id: int, *, provider: str, amount: Decimal, currency: str,
        payment_url: Optional[str] = None, provider_link_id: Optional[str] = None,
        provider_payment_id: Optional[str] = None, status: str = "pending",
        expires_at: Optional[datetime] = None, raw: Optional[str] = None,
    ) -> Dict[str, Any]:
        from database.db import session_scope
        from database.models import Payment

        with session_scope() as session:
            payment = Payment(
                order_id=order_id, provider=provider, amount=amount, currency=currency,
                payment_url=payment_url, provider_link_id=provider_link_id,
                provider_payment_id=provider_payment_id, status=status,
                expires_at=expires_at, raw=raw,
            )
            session.add(payment)
            session.flush()
            self._audit(session, "system", "payment.create", "payment", str(payment.id),
                        f"{provider} {status} {_f(amount)} {currency}")
            _metric("payments_total")
            return {
                "id": payment.id, "order_id": order_id, "provider": provider,
                "payment_url": payment_url, "provider_link_id": provider_link_id,
                "provider_payment_id": provider_payment_id, "amount": _f(amount),
                "currency": currency, "status": status, "expires_at": _iso(expires_at),
            }

    def mark_payment_paid(
        self, *, provider_payment_id: Optional[str] = None,
        provider_link_id: Optional[str] = None, order_id: Optional[int] = None,
        status: str = "paid", raw: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update a payment (by provider id / link / order) and sync the order."""
        from database.db import session_scope
        from database.models import Payment, Order

        with session_scope() as session:
            payment = None
            q = session.query(Payment)
            if provider_payment_id:
                payment = q.filter_by(provider_payment_id=provider_payment_id).first()
            if payment is None and provider_link_id:
                payment = q.filter_by(provider_link_id=provider_link_id).first()
            if payment is None and order_id:
                payment = q.filter_by(order_id=order_id) \
                    .order_by(Payment.created_at.desc()).first()
            if payment is None:
                return None
            payment.status = status
            if raw:
                payment.raw = raw
            order = session.get(Order, payment.order_id)
            if order is not None:
                order.payment_status = "paid" if status == "paid" else status
                if status == "paid" and order.status == "received":
                    order.status = "confirmed"
                if status == "paid":
                    _metric("revenue_total", _f(payment.amount))
            self._audit(session, "payment_webhook", "payment.update", "payment",
                        str(payment.id), status)
            return _order_to_dict(order) if order is not None else None

    # -- invoices & notifications ----------------------------------------

    def record_invoice(
        self, order_id: int, *, invoice_number: str, pdf_path: str,
        total: Decimal, currency: str,
    ) -> Dict[str, Any]:
        from database.db import session_scope
        from database.models import Invoice

        with session_scope() as session:
            inv = Invoice(
                order_id=order_id, invoice_number=invoice_number, pdf_path=pdf_path,
                total=total, currency=currency,
            )
            session.add(inv)
            session.flush()
            return {"id": inv.id, "invoice_number": invoice_number, "pdf_path": pdf_path}

    def log_notification(
        self, *, kind: str, order_id: Optional[int] = None, wa_number: Optional[str] = None,
        audience: str = "customer", body: Optional[str] = None, status: str = "sent",
    ) -> None:
        from database.db import session_scope
        from database.models import NotificationLog

        _metric("notifications_total")
        try:
            with session_scope() as session:
                session.add(NotificationLog(
                    order_id=order_id, wa_number=wa_number, kind=kind, audience=audience,
                    body=(body or "")[:4000], status=status,
                ))
        except Exception as exc:  # noqa: BLE001 - never break the caller on logging
            logger.debug("COMMERCE | notification log failed: %s", exc)

    def audit(
        self, *, actor: str, action: str, entity: Optional[str] = None,
        entity_id: Optional[str] = None, detail: Optional[str] = None, ip: Optional[str] = None,
    ) -> None:
        from database.db import session_scope

        try:
            with session_scope() as session:
                self._audit(session, actor, action, entity, entity_id, detail, ip)
        except Exception as exc:  # noqa: BLE001
            logger.debug("COMMERCE | audit failed: %s", exc)

    # -- internals --------------------------------------------------------

    @staticmethod
    def _apply_reservation_side_effects(order_id: int, status: str) -> None:
        """Release inventory on cancel/refund; commit it on fulfilment.

        Guarded and lazy so the order lifecycle never depends on the reservation
        module being importable or enabled.
        """
        try:
            from commerce.reservations import commit_for_order, release_for_order

            if status in {"cancelled", "refunded"}:
                release_for_order(order_id)
            elif status in {"shipped", "delivered"}:
                commit_for_order(order_id)
        except Exception as exc:  # noqa: BLE001 - reservations are best-effort
            logger.debug("COMMERCE | reservation side effect skipped: %s", exc)

    @staticmethod
    def _audit(session, actor, action, entity=None, entity_id=None, detail=None, ip=None) -> None:
        from database.models import AuditLog

        row = AuditLog(
            actor=actor or "system", action=action, entity=entity, entity_id=entity_id,
            detail=(detail or "")[:2000], ip=ip,
        )
        session.add(row)
        # v8.0 tamper-evident hash chain (best-effort; never breaks the write).
        try:
            from commerce.audit_chain import apply_chain

            apply_chain(session, row)
        except Exception as exc:  # noqa: BLE001
            logger.debug("COMMERCE | audit chain skipped: %s", exc)

    @staticmethod
    def _resolve(session, order_id, order_number):
        from database.models import Order

        if order_id is not None:
            return session.get(Order, order_id)
        if order_number:
            return session.query(Order).filter_by(order_number=order_number).first()
        return None

    def soft_delete_order(self, order_id: int, *, actor: str = "admin", ip=None):
        """Soft-delete an order (hidden from listings; data retained)."""
        from database.db import session_scope
        from database.models import Order

        with session_scope() as session:
            order = session.get(Order, order_id)
            if order is None:
                return None
            order.deleted_at = datetime.now(timezone.utc)
            self._audit(session, actor, "order.soft_delete", "order", str(order.id), None, ip)
            return _order_to_dict(order)

    @staticmethod
    def _apply_filters(q, Order, status, payment_status, query, date_from, date_to, tenant_id=None):
        # Exclude soft-deleted orders from all listings/counts.
        q = q.filter(Order.deleted_at.is_(None))
        if tenant_id:
            q = q.filter(Order.tenant_id == tenant_id)
        if status:
            q = q.filter(Order.status == status)
        if payment_status:
            q = q.filter(Order.payment_status == payment_status)
        if query:
            like = f"%{query}%"
            q = q.filter(
                (Order.order_number.ilike(like))
                | (Order.wa_number.ilike(like))
                | (Order.customer_name.ilike(like))
            )
        if date_from:
            q = q.filter(Order.created_at >= _parse_day(date_from))
        if date_to:
            q = q.filter(Order.created_at <= _parse_day(date_to, end=True))
        return q


def _parse_day(value: str, end: bool = False) -> datetime:
    try:
        dt = datetime.strptime(value[:10], "%Y-%m-%d")
        if end:
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc)


# Module-level singleton used across the app.
order_service = OrderService()
