"""
payments/razorpay_provider.py
------------------------------
Razorpay provider using the Payment Links REST API.

* create_link -> ``POST https://api.razorpay.com/v1/payment_links`` with HTTP
  Basic auth (``key_id:key_secret``); the amount is sent in paise.
* verify_and_parse_webhook -> verifies the ``X-Razorpay-Signature`` header
  (HMAC-SHA256 hex of the raw body keyed by ``razorpay_webhook_secret``) and
  maps the event to a normalized payment status.

Requires ``razorpay_key_id`` / ``razorpay_key_secret`` for link creation and
``razorpay_webhook_secret`` for webhook verification. Missing credentials raise
a clear :class:`RuntimeError`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Dict

import requests

from config import config
from utils.logging import logger

from payments.base import PaymentLink, PaymentProvider, WebhookResult

_API_URL = "https://api.razorpay.com/v1/payment_links"

# Razorpay payment_link statuses / payment events -> normalized status.
_STATUS_MAP = {
    "paid": "paid",
    "captured": "paid",
    "authorized": "paid",
    "failed": "failed",
    "cancelled": "failed",
    "expired": "failed",
    "refunded": "refunded",
}


class RazorpayProvider(PaymentProvider):
    """Razorpay Payment Links adapter."""

    @property
    def name(self) -> str:
        """Return the provider name."""
        return "razorpay"

    def available(self) -> bool:
        """Return True when API credentials are configured."""
        return bool(config.razorpay_key_id and config.razorpay_key_secret)

    def create_link(self, order: Dict[str, Any]) -> PaymentLink:
        """Create a Razorpay Payment Link for the order.

        Args:
            order: The order dict.

        Returns:
            A :class:`PaymentLink` carrying Razorpay's ``short_url``.

        Raises:
            RuntimeError: If credentials are missing or the API call fails.
        """
        if not self.available():
            raise RuntimeError(
                "Razorpay credentials missing (RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET)"
            )

        order_number = str(order.get("order_number") or order.get("id") or "")
        currency = str(order.get("currency") or config.default_currency or "INR")
        amount_major = float(order.get("total_amount") or 0)
        amount_paise = int(round(amount_major * 100))
        expires_at = self._expiry()

        payload: Dict[str, Any] = {
            "amount": amount_paise,
            "currency": currency,
            "accept_partial": False,
            "description": f"{config.business_name} order {order_number}",
            "reference_id": order_number,
            "expire_by": int(expires_at.timestamp()),
            "customer": {
                "name": order.get("customer_name") or "",
                "contact": order.get("wa_number") or "",
            },
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "notes": {"order_number": order_number, "order_id": str(order.get("id") or "")},
        }

        try:
            response = requests.post(
                _API_URL,
                auth=(config.razorpay_key_id, config.razorpay_key_secret),
                json=payload,
                timeout=config.request_timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # noqa: BLE001 - normalized into RuntimeError
            logger.error("PAYMENTS | razorpay: create_link failed: %s", exc)
            raise RuntimeError(f"Razorpay create_link failed: {exc}") from exc

        short_url = data.get("short_url")
        link_id = data.get("id")
        if not short_url:
            raise RuntimeError(f"Razorpay response missing short_url: {data}")

        logger.info(
            "PAYMENTS | razorpay: created payment link %s for order %s",
            link_id, order_number,
        )
        return PaymentLink(
            url=short_url,
            provider=self.name,
            amount=amount_major,
            currency=currency,
            provider_link_id=link_id,
            expires_at=expires_at,
            raw=data,
        )

    def verify_and_parse_webhook(
        self, headers: Dict[str, str], raw_body: bytes
    ) -> WebhookResult:
        """Verify the ``X-Razorpay-Signature`` header and parse the event.

        Args:
            headers: Inbound HTTP headers.
            raw_body: The exact raw request body bytes.

        Returns:
            A normalized :class:`WebhookResult`; ``ok=False`` on any failure.
        """
        secret = config.razorpay_webhook_secret
        if not secret:
            logger.warning("PAYMENTS | razorpay: webhook secret not configured")
            return WebhookResult(ok=False, event="unconfigured")

        signature = self._get_header(headers, "X-Razorpay-Signature")
        if not signature:
            logger.warning("PAYMENTS | razorpay: missing X-Razorpay-Signature")
            return WebhookResult(ok=False, event="missing_signature")

        expected = hmac.new(
            secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature.strip()):
            logger.warning("PAYMENTS | razorpay: signature mismatch")
            return WebhookResult(ok=False, event="bad_signature")

        try:
            body = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.error("PAYMENTS | razorpay: webhook JSON parse failed: %s", exc)
            return WebhookResult(ok=False, event="bad_payload")

        event = body.get("event", "")
        payload = body.get("payload", {}) or {}

        link_entity = (payload.get("payment_link", {}) or {}).get("entity", {}) or {}
        payment_entity = (payload.get("payment", {}) or {}).get("entity", {}) or {}

        raw_status = (
            link_entity.get("status")
            or payment_entity.get("status")
            or ""
        ).lower()
        status = _STATUS_MAP.get(raw_status, "")

        provider_link_id = link_entity.get("id")
        provider_payment_id = payment_entity.get("id") or link_entity.get("id")

        # reference_id carries our order_number when we created the link.
        order_id = None
        notes = link_entity.get("notes") or payment_entity.get("notes") or {}
        if isinstance(notes, dict) and notes.get("order_id"):
            try:
                order_id = int(notes["order_id"])
            except (TypeError, ValueError):
                order_id = None

        logger.info(
            "PAYMENTS | razorpay: webhook event=%s status=%s link=%s payment=%s",
            event, status or raw_status, provider_link_id, provider_payment_id,
        )
        return WebhookResult(
            ok=True,
            status=status,
            provider_payment_id=provider_payment_id,
            provider_link_id=provider_link_id,
            order_id=order_id,
            event=event,
            raw=body,
        )
