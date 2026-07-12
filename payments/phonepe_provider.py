"""
payments/phonepe_provider.py
-----------------------------
PhonePe provider using the PG ``/pg/v1/pay`` API.

* create_link -> base64-encodes the request payload, computes
  ``X-VERIFY = SHA256(base64payload + "/pg/v1/pay" + salt_key) + "###" +
  salt_index``, POSTs to ``{base}/pg/v1/pay`` and returns the redirect URL.
  The base is ``https://api-preprod.phonepe.com/apis/pg-sandbox`` in sandbox
  and ``https://api.phonepe.com/apis/hermes`` in production.
* verify_and_parse_webhook -> verifies ``X-VERIFY`` on the callback
  (``SHA256(base64response + salt_key) + "###" + salt_index``) and maps the
  event to a normalized status.

Requires ``phonepe_merchant_id`` / ``phonepe_salt_key`` / ``phonepe_salt_index``.
Missing credentials raise a clear :class:`RuntimeError`.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Dict

import requests

from config import config
from utils.logging import logger

from payments.base import PaymentLink, PaymentProvider, WebhookResult

_PAY_PATH = "/pg/v1/pay"

_STATUS_MAP = {
    "PAYMENT_SUCCESS": "paid",
    "SUCCESS": "paid",
    "COMPLETED": "paid",
    "PAYMENT_ERROR": "failed",
    "PAYMENT_DECLINED": "failed",
    "FAILED": "failed",
    "PAYMENT_PENDING": "",
    "PENDING": "",
}


def _base_url() -> str:
    """Return the PhonePe PG base URL for the configured environment."""
    if (config.phonepe_env or "sandbox").lower() == "production":
        return "https://api.phonepe.com/apis/hermes"
    return "https://api-preprod.phonepe.com/apis/pg-sandbox"


def _x_verify(encoded: str, path_or_salt: str, salt_key: str, salt_index: str) -> str:
    """Build an ``X-VERIFY`` value: ``sha256(encoded + suffix + salt_key)###idx``."""
    to_hash = f"{encoded}{path_or_salt}{salt_key}".encode("utf-8")
    checksum = hashlib.sha256(to_hash).hexdigest()
    return f"{checksum}###{salt_index}"


class PhonePeProvider(PaymentProvider):
    """PhonePe PG pay adapter."""

    @property
    def name(self) -> str:
        """Return the provider name."""
        return "phonepe"

    def available(self) -> bool:
        """Return True when merchant credentials are configured."""
        return bool(config.phonepe_merchant_id and config.phonepe_salt_key)

    def create_link(self, order: Dict[str, Any]) -> PaymentLink:
        """Create a PhonePe payment and return the redirect URL.

        Args:
            order: The order dict.

        Returns:
            A :class:`PaymentLink` carrying the PhonePe redirect URL.

        Raises:
            RuntimeError: If credentials are missing or the API call fails.
        """
        if not self.available():
            raise RuntimeError(
                "PhonePe credentials missing (PHONEPE_MERCHANT_ID / PHONEPE_SALT_KEY)"
            )

        order_number = str(order.get("order_number") or order.get("id") or "")
        currency = str(order.get("currency") or config.default_currency or "INR")
        amount_major = float(order.get("total_amount") or 0)
        amount_paise = int(round(amount_major * 100))
        base = (config.shopify_app_url or "").rstrip("/")
        salt_key = config.phonepe_salt_key
        salt_index = str(config.phonepe_salt_index or "1")

        # PhonePe merchantTransactionId must be unique & <=35 chars.
        txn_id = f"MT{order_number}"[:35]
        wa_number = str(order.get("wa_number") or "").strip()

        request_payload: Dict[str, Any] = {
            "merchantId": config.phonepe_merchant_id,
            "merchantTransactionId": txn_id,
            "merchantUserId": f"U{wa_number}"[:35] if wa_number else "GUEST",
            "amount": amount_paise,
            "redirectUrl": f"{base}/pay/{order_number}/return",
            "redirectMode": "REDIRECT",
            "callbackUrl": f"{base}/payments/phonepe/webhook",
            "paymentInstrument": {"type": "PAY_PAGE"},
        }

        encoded = base64.b64encode(
            json.dumps(request_payload).encode("utf-8")
        ).decode("utf-8")
        x_verify = _x_verify(encoded, _PAY_PATH, salt_key, salt_index)

        try:
            response = requests.post(
                f"{_base_url()}{_PAY_PATH}",
                headers={
                    "Content-Type": "application/json",
                    "X-VERIFY": x_verify,
                    "accept": "application/json",
                },
                json={"request": encoded},
                timeout=config.request_timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.error("PAYMENTS | phonepe: create_link failed: %s", exc)
            raise RuntimeError(f"PhonePe create_link failed: {exc}") from exc

        instrument = (
            (data.get("data", {}) or {})
            .get("instrumentResponse", {}) or {}
        )
        redirect = instrument.get("redirectInfo", {}) or {}
        url = redirect.get("url")
        if not url:
            raise RuntimeError(f"PhonePe response missing redirect url: {data}")

        logger.info(
            "PAYMENTS | phonepe: created payment %s for order %s",
            txn_id, order_number,
        )
        return PaymentLink(
            url=url,
            provider=self.name,
            amount=amount_major,
            currency=currency,
            provider_link_id=txn_id,
            expires_at=self._expiry(),
            raw=data,
        )

    def verify_and_parse_webhook(
        self, headers: Dict[str, str], raw_body: bytes
    ) -> WebhookResult:
        """Verify the ``X-VERIFY`` callback header and parse the event.

        PhonePe callbacks post a base64 ``response`` field and sign it as
        ``SHA256(base64response + salt_key)###salt_index`` in ``X-VERIFY``.

        Args:
            headers: Inbound HTTP headers.
            raw_body: The exact raw request body bytes.

        Returns:
            A normalized :class:`WebhookResult`; ``ok=False`` on any failure.
        """
        salt_key = config.phonepe_salt_key
        if not salt_key:
            logger.warning("PAYMENTS | phonepe: salt key not configured")
            return WebhookResult(ok=False, event="unconfigured")

        signature = self._get_header(headers, "X-VERIFY")
        if not signature:
            logger.warning("PAYMENTS | phonepe: missing X-VERIFY")
            return WebhookResult(ok=False, event="missing_signature")

        try:
            body = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.error("PAYMENTS | phonepe: webhook JSON parse failed: %s", exc)
            return WebhookResult(ok=False, event="bad_payload")

        encoded = body.get("response", "")
        if not encoded:
            logger.warning("PAYMENTS | phonepe: callback missing response field")
            return WebhookResult(ok=False, event="bad_payload")

        salt_index = str(config.phonepe_salt_index or "1")
        expected = _x_verify(encoded, "", salt_key, salt_index)
        # X-VERIFY over the callback response uses no path suffix.
        import hmac  # local import; only needed for constant-time compare
        if not hmac.compare_digest(expected, signature.strip()):
            logger.warning("PAYMENTS | phonepe: signature mismatch")
            return WebhookResult(ok=False, event="bad_signature")

        try:
            decoded = json.loads(base64.b64decode(encoded).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.error("PAYMENTS | phonepe: response decode failed: %s", exc)
            return WebhookResult(ok=False, event="bad_payload")

        data = decoded.get("data", {}) or {}
        raw_state = (decoded.get("code") or data.get("state") or "").upper()
        status = _STATUS_MAP.get(raw_state, "")
        txn_id = data.get("merchantTransactionId")
        provider_payment_id = data.get("transactionId") or txn_id

        logger.info(
            "PAYMENTS | phonepe: webhook code=%s status=%s txn=%s",
            raw_state, status, txn_id,
        )
        return WebhookResult(
            ok=True,
            status=status,
            provider_payment_id=provider_payment_id,
            provider_link_id=txn_id,
            order_id=None,
            event=raw_state,
            raw=decoded,
        )
