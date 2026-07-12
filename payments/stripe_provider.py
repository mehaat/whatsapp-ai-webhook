"""
payments/stripe_provider.py
----------------------------
Stripe provider using the Checkout Sessions REST API.

* create_link -> ``POST https://api.stripe.com/v1/checkout/sessions`` with a
  ``Bearer`` secret key and form-encoded body; the amount is sent in the
  currency's smallest unit (e.g. paise for INR, cents for USD).
* verify_and_parse_webhook -> verifies the ``Stripe-Signature`` header per
  Stripe's scheme (``t=`` timestamp and ``v1=`` HMAC-SHA256 of
  ``"{t}.{payload}"`` keyed by ``stripe_webhook_secret``).

Requires ``stripe_secret_key`` for session creation and
``stripe_webhook_secret`` for webhook verification. Missing credentials raise a
clear :class:`RuntimeError`.
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

_API_URL = "https://api.stripe.com/v1/checkout/sessions"

# Stripe currencies that are "zero-decimal" (amount already in major units).
_ZERO_DECIMAL = {
    "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg",
    "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
}

_EVENT_STATUS = {
    "checkout.session.completed": "paid",
    "checkout.session.async_payment_succeeded": "paid",
    "payment_intent.succeeded": "paid",
    "checkout.session.async_payment_failed": "failed",
    "payment_intent.payment_failed": "failed",
    "charge.refunded": "refunded",
}


def _smallest_unit(amount_major: float, currency: str) -> int:
    """Convert a major-unit amount to the currency's smallest integer unit."""
    if currency.lower() in _ZERO_DECIMAL:
        return int(round(amount_major))
    return int(round(amount_major * 100))


class StripeProvider(PaymentProvider):
    """Stripe Checkout Sessions adapter."""

    @property
    def name(self) -> str:
        """Return the provider name."""
        return "stripe"

    def available(self) -> bool:
        """Return True when the Stripe secret key is configured."""
        return bool(config.stripe_secret_key)

    def create_link(self, order: Dict[str, Any]) -> PaymentLink:
        """Create a Stripe Checkout Session for the order.

        Args:
            order: The order dict.

        Returns:
            A :class:`PaymentLink` carrying the hosted checkout URL.

        Raises:
            RuntimeError: If credentials are missing or the API call fails.
        """
        if not self.available():
            raise RuntimeError("Stripe credentials missing (STRIPE_SECRET_KEY)")

        order_number = str(order.get("order_number") or order.get("id") or "")
        currency = str(order.get("currency") or config.default_currency or "INR")
        amount_major = float(order.get("total_amount") or 0)
        unit_amount = _smallest_unit(amount_major, currency)
        base = (config.shopify_app_url or "").rstrip("/")

        # Stripe expects nested params flattened with bracket notation and
        # form-encoding; requests handles the encoding of this flat dict.
        form: Dict[str, Any] = {
            "mode": "payment",
            "success_url": f"{base}/pay/{order_number}/success",
            "cancel_url": f"{base}/pay/{order_number}/cancel",
            "client_reference_id": order_number,
            "expires_at": int(self._expiry().timestamp()),
            "line_items[0][price_data][currency]": currency.lower(),
            "line_items[0][price_data][unit_amount]": unit_amount,
            "line_items[0][price_data][product_data][name]":
                f"{config.business_name} order {order_number}",
            "line_items[0][quantity]": 1,
            "metadata[order_number]": order_number,
            "metadata[order_id]": str(order.get("id") or ""),
        }

        try:
            response = requests.post(
                _API_URL,
                headers={"Authorization": f"Bearer {config.stripe_secret_key}"},
                data=form,
                timeout=config.request_timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.error("PAYMENTS | stripe: create_link failed: %s", exc)
            raise RuntimeError(f"Stripe create_link failed: {exc}") from exc

        url = data.get("url")
        session_id = data.get("id")
        if not url:
            raise RuntimeError(f"Stripe response missing url: {data}")

        logger.info(
            "PAYMENTS | stripe: created checkout session %s for order %s",
            session_id, order_number,
        )
        return PaymentLink(
            url=url,
            provider=self.name,
            amount=amount_major,
            currency=currency,
            provider_link_id=session_id,
            provider_payment_id=data.get("payment_intent"),
            expires_at=self._expiry(),
            raw=data,
        )

    def verify_and_parse_webhook(
        self, headers: Dict[str, str], raw_body: bytes
    ) -> WebhookResult:
        """Verify the ``Stripe-Signature`` header and parse the event.

        Args:
            headers: Inbound HTTP headers.
            raw_body: The exact raw request body bytes.

        Returns:
            A normalized :class:`WebhookResult`; ``ok=False`` on any failure.
        """
        secret = config.stripe_webhook_secret
        if not secret:
            logger.warning("PAYMENTS | stripe: webhook secret not configured")
            return WebhookResult(ok=False, event="unconfigured")

        sig_header = self._get_header(headers, "Stripe-Signature")
        if not sig_header:
            logger.warning("PAYMENTS | stripe: missing Stripe-Signature")
            return WebhookResult(ok=False, event="missing_signature")

        timestamp = ""
        signatures = []
        for part in sig_header.split(","):
            if "=" not in part:
                continue
            key, _, value = part.strip().partition("=")
            if key == "t":
                timestamp = value
            elif key == "v1":
                signatures.append(value)

        if not timestamp or not signatures:
            logger.warning("PAYMENTS | stripe: malformed signature header")
            return WebhookResult(ok=False, event="bad_signature")

        signed_payload = f"{timestamp}.{raw_body.decode('utf-8')}".encode("utf-8")
        expected = hmac.new(
            secret.encode("utf-8"), signed_payload, hashlib.sha256
        ).hexdigest()
        if not any(hmac.compare_digest(expected, sig) for sig in signatures):
            logger.warning("PAYMENTS | stripe: signature mismatch")
            return WebhookResult(ok=False, event="bad_signature")

        try:
            body = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.error("PAYMENTS | stripe: webhook JSON parse failed: %s", exc)
            return WebhookResult(ok=False, event="bad_payload")

        event = body.get("type", "")
        obj = ((body.get("data") or {}).get("object") or {})
        status = _EVENT_STATUS.get(event, "")

        provider_link_id = obj.get("id") if event.startswith("checkout.session") else None
        provider_payment_id = (
            obj.get("payment_intent")
            or (obj.get("id") if event.startswith("payment_intent") else None)
        )

        order_id = None
        metadata = obj.get("metadata") or {}
        if isinstance(metadata, dict) and metadata.get("order_id"):
            try:
                order_id = int(metadata["order_id"])
            except (TypeError, ValueError):
                order_id = None

        logger.info(
            "PAYMENTS | stripe: webhook event=%s status=%s session=%s intent=%s",
            event, status, provider_link_id, provider_payment_id,
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
