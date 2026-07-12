"""
payments/cashfree_provider.py
------------------------------
Cashfree provider using the Payment Gateway (PG) Orders API.

* create_link -> ``POST {base}/orders`` with ``x-client-id`` /
  ``x-client-secret`` / ``x-api-version`` headers; returns the hosted
  ``payment_link``. The base is ``https://sandbox.cashfree.com/pg`` in sandbox
  and ``https://api.cashfree.com/pg`` in production (selected by
  ``cashfree_env``).
* verify_and_parse_webhook -> verifies Cashfree's webhook signature
  (base64 HMAC-SHA256 of ``timestamp + raw_body`` keyed by
  ``cashfree_secret_key``) and maps the event to a normalized status.

Requires ``cashfree_app_id`` / ``cashfree_secret_key``. Missing credentials
raise a clear :class:`RuntimeError`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any, Dict

import requests

from config import config
from utils.logging import logger

from payments.base import PaymentLink, PaymentProvider, WebhookResult

_API_VERSION = "2023-08-01"

_STATUS_MAP = {
    "paid": "paid",
    "success": "paid",
    "successful": "paid",
    "failed": "failed",
    "cancelled": "failed",
    "user_dropped": "failed",
    "refunded": "refunded",
    "refund": "refunded",
}


def _base_url() -> str:
    """Return the Cashfree PG base URL for the configured environment."""
    if (config.cashfree_env or "sandbox").lower() == "production":
        return "https://api.cashfree.com/pg"
    return "https://sandbox.cashfree.com/pg"


class CashfreeProvider(PaymentProvider):
    """Cashfree PG Orders adapter."""

    @property
    def name(self) -> str:
        """Return the provider name."""
        return "cashfree"

    def available(self) -> bool:
        """Return True when API credentials are configured."""
        return bool(config.cashfree_app_id and config.cashfree_secret_key)

    def create_link(self, order: Dict[str, Any]) -> PaymentLink:
        """Create a Cashfree order and return its hosted payment link.

        Args:
            order: The order dict.

        Returns:
            A :class:`PaymentLink` carrying Cashfree's ``payment_link``.

        Raises:
            RuntimeError: If credentials are missing or the API call fails.
        """
        if not self.available():
            raise RuntimeError(
                "Cashfree credentials missing (CASHFREE_APP_ID / CASHFREE_SECRET_KEY)"
            )

        order_number = str(order.get("order_number") or order.get("id") or "")
        currency = str(order.get("currency") or config.default_currency or "INR")
        amount_major = float(order.get("total_amount") or 0)
        base = (config.shopify_app_url or "").rstrip("/")

        # Cashfree needs a customer_id and a phone; derive safely from the order.
        wa_number = str(order.get("wa_number") or "").strip() or "0000000000"
        customer_id = f"wa_{wa_number}".replace("+", "")

        payload: Dict[str, Any] = {
            "order_id": order_number,
            "order_amount": round(amount_major, 2),
            "order_currency": currency,
            "customer_details": {
                "customer_id": customer_id,
                "customer_phone": wa_number,
                "customer_name": order.get("customer_name") or "",
            },
            "order_meta": {
                "return_url": f"{base}/pay/{order_number}/return?order_id={{order_id}}",
                "notify_url": f"{base}/payments/cashfree/webhook",
            },
            "order_note": f"{config.business_name} order {order_number}",
        }

        try:
            response = requests.post(
                f"{_base_url()}/orders",
                headers={
                    "x-client-id": config.cashfree_app_id,
                    "x-client-secret": config.cashfree_secret_key,
                    "x-api-version": _API_VERSION,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=config.request_timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.error("PAYMENTS | cashfree: create_link failed: %s", exc)
            raise RuntimeError(f"Cashfree create_link failed: {exc}") from exc

        # The hosted checkout link lives under payment_link; fall back to the
        # session id + a hosted path if the account returns only a session.
        url = data.get("payment_link")
        session_id = data.get("payment_session_id")
        cf_order_id = data.get("cf_order_id") or data.get("order_id")
        if not url and session_id:
            url = f"{_base_url()}/view/sessions/{session_id}"
        if not url:
            raise RuntimeError(f"Cashfree response missing payment_link: {data}")

        logger.info(
            "PAYMENTS | cashfree: created order %s (cf_order_id=%s)",
            order_number, cf_order_id,
        )
        return PaymentLink(
            url=url,
            provider=self.name,
            amount=amount_major,
            currency=currency,
            provider_link_id=str(cf_order_id) if cf_order_id is not None else order_number,
            expires_at=self._expiry(),
            raw=data,
        )

    def verify_and_parse_webhook(
        self, headers: Dict[str, str], raw_body: bytes
    ) -> WebhookResult:
        """Verify Cashfree's webhook signature and parse the event.

        Cashfree signs ``{x-webhook-timestamp}{raw_body}`` with HMAC-SHA256 and
        sends the base64 digest in ``x-webhook-signature``.

        Args:
            headers: Inbound HTTP headers.
            raw_body: The exact raw request body bytes.

        Returns:
            A normalized :class:`WebhookResult`; ``ok=False`` on any failure.
        """
        secret = config.cashfree_secret_key
        if not secret:
            logger.warning("PAYMENTS | cashfree: secret not configured")
            return WebhookResult(ok=False, event="unconfigured")

        signature = self._get_header(headers, "x-webhook-signature")
        timestamp = self._get_header(headers, "x-webhook-timestamp")
        if not signature or not timestamp:
            logger.warning("PAYMENTS | cashfree: missing signature/timestamp headers")
            return WebhookResult(ok=False, event="missing_signature")

        signed = (timestamp.encode("utf-8") + raw_body)
        digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).digest()
        expected = base64.b64encode(digest).decode("utf-8")
        if not hmac.compare_digest(expected, signature.strip()):
            logger.warning("PAYMENTS | cashfree: signature mismatch")
            return WebhookResult(ok=False, event="bad_signature")

        try:
            body = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.error("PAYMENTS | cashfree: webhook JSON parse failed: %s", exc)
            return WebhookResult(ok=False, event="bad_payload")

        event = body.get("type", "")
        data = body.get("data", {}) or {}
        order_data = data.get("order", {}) or {}
        payment_data = data.get("payment", {}) or {}

        raw_status = (
            payment_data.get("payment_status")
            or order_data.get("order_status")
            or ""
        ).lower()
        status = _STATUS_MAP.get(raw_status, "")

        provider_link_id = str(
            order_data.get("cf_order_id") or order_data.get("order_id") or ""
        ) or None
        provider_payment_id = str(payment_data.get("cf_payment_id") or "") or None

        order_id = None
        # order_id in Cashfree is our order_number (a string), not the numeric
        # local id; leave order_id None so lookup falls back to link/payment ids.

        logger.info(
            "PAYMENTS | cashfree: webhook event=%s status=%s order=%s payment=%s",
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
