"""
payments/manual_upi.py
------------------------
Manual UPI provider — the always-available, fully offline default.

Builds a standard UPI deep link (``upi://pay?...``) that any UPI app on the
customer's phone can open to pay directly to the merchant's VPA. No external
API is called, so this provider works even without any payment gateway
credentials configured.

There is no callback/webhook for manual UPI, so
:meth:`ManualUpiProvider.verify_and_parse_webhook` always reports ``ok=False``.
"""

from __future__ import annotations

from typing import Any, Dict
from urllib.parse import quote, urlencode

from config import config
from utils.logging import logger

from payments.base import PaymentLink, PaymentProvider, WebhookResult


class ManualUpiProvider(PaymentProvider):
    """Offline UPI deep-link provider; always available."""

    @property
    def name(self) -> str:
        """Return the provider name."""
        return "manual_upi"

    def create_link(self, order: Dict[str, Any]) -> PaymentLink:
        """Build a ``upi://pay`` deep link for the order.

        When no UPI VPA is configured, fall back to a hosted note page under
        ``shopify_app_url`` so the customer always receives a usable link.

        Args:
            order: The order dict.

        Returns:
            A :class:`PaymentLink` carrying the deep link (or fallback URL).
        """
        order_number = str(order.get("order_number") or order.get("id") or "")
        currency = str(order.get("currency") or config.default_currency or "INR")
        amount = order.get("total_amount") or 0
        # UPI expects the amount as a plain decimal string in major units.
        amount_str = f"{float(amount):.2f}"

        vpa = getattr(config, "upi_vpa", "") or ""
        if not vpa:
            base = (getattr(config, "shopify_app_url", "") or "").rstrip("/")
            fallback_url = f"{base}/pay/{quote(order_number)}"
            logger.warning(
                "PAYMENTS | manual_upi: UPI_VPA is not configured; returning "
                "hosted fallback link for order %s",
                order_number,
            )
            return PaymentLink(
                url=fallback_url,
                provider=self.name,
                amount=amount,
                currency=currency,
                provider_link_id=order_number,
                expires_at=self._expiry(),
                raw={"fallback": True, "reason": "upi_vpa_unset"},
            )

        payee_name = getattr(config, "upi_payee_name", "") or config.business_name
        params = {
            "pa": vpa,
            "pn": payee_name,
            "am": amount_str,
            "cu": currency,
            "tn": order_number,
        }
        # urlencode with quote_via=quote keeps the deep link RFC-3986 clean.
        query = urlencode(params, quote_via=quote)
        url = f"upi://pay?{query}"

        logger.info(
            "PAYMENTS | manual_upi: built UPI link for order %s (%s %s)",
            order_number, amount_str, currency,
        )
        return PaymentLink(
            url=url,
            provider=self.name,
            amount=amount,
            currency=currency,
            provider_link_id=order_number,
            expires_at=self._expiry(),
            raw={"vpa": vpa, "params": params},
        )

    def verify_and_parse_webhook(
        self, headers: Dict[str, str], raw_body: bytes
    ) -> WebhookResult:
        """Manual UPI has no callback; always report unsupported.

        Args:
            headers: Ignored.
            raw_body: Ignored.

        Returns:
            A :class:`WebhookResult` with ``ok=False`` and ``event="unsupported"``.
        """
        return WebhookResult(ok=False, event="unsupported")

    @staticmethod
    def available() -> bool:
        """Manual UPI is always available (no credentials required)."""
        return True
