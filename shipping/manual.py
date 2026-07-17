"""
shipping/manual.py
--------------------
Manual courier provider — the always-available, fully offline default.

Generates an internal AWB (``"MH" + digits``) and a customer-facing tracking
link derived from the configured business website. No external API is called,
so this provider works even without any courier credentials configured. It is
the fallback the factory returns for unknown/unconfigured providers.
"""

from __future__ import annotations

import random
import time
from typing import Any, Dict
from urllib.parse import quote

from config import config
from utils.logging import logger

from shipping.base import ShipmentResult, ShippingProvider, TrackingResult


class ManualProvider(ShippingProvider):
    """Offline internal-fulfilment provider; always available."""

    @property
    def name(self) -> str:
        """Return the provider name."""
        return "manual"

    def _generate_awb(self, order: Dict[str, Any]) -> str:
        """Mint a stable-ish internal AWB of the form ``MH<digits>``."""
        order_id = str(order.get("id") or "").strip()
        suffix = order_id.zfill(4) if order_id else f"{random.randint(0, 9999):04d}"
        # Millisecond epoch keeps AWBs unique per shipment.
        return f"MH{int(time.time() * 1000)}{suffix}"

    def _tracking_url(self, awb: str) -> str:
        """Build a customer-facing tracking link on the business site."""
        base = (getattr(config, "business_website", "") or "").rstrip("/")
        if not base:
            base = "https://mehaatfaishon.com"
        return f"{base}/tracking?awb={quote(awb)}"

    def create_shipment(self, order: Dict[str, Any]) -> ShipmentResult:
        """Create an internal shipment with a generated AWB (no network).

        Args:
            order: The order dict.

        Returns:
            A :class:`ShipmentResult` with ``ok=True`` and an internal AWB.
        """
        awb = self._generate_awb(order)
        tracking_url = self._tracking_url(awb)
        courier_name = (getattr(config, "business_name", "") or "ME-HAAT Fashion").strip()

        logger.info(
            "SHIPPING | manual: created internal shipment awb=%s for order %s",
            awb, order.get("order_number") or order.get("id"),
        )
        return ShipmentResult(
            ok=True,
            provider=self.name,
            awb=awb,
            courier_name=f"{courier_name} Fulfilment",
            label_url=None,
            tracking_url=tracking_url,
            provider_shipment_id=awb,
            status="created",
            raw={"internal": True, "awb": awb},
        )

    def track(self, awb: str) -> TrackingResult:
        """Return a generic tracking status for an internal AWB.

        Args:
            awb: The internal AWB.

        Returns:
            A :class:`TrackingResult` with a generic ``in_transit`` status.
        """
        return TrackingResult(
            ok=True,
            status="in_transit",
            checkpoints=[{
                "status": "in_transit",
                "location": "",
                "note": "Shipment is being processed by the store.",
            }],
            raw={"internal": True, "awb": awb},
        )

    def schedule_pickup(self, shipment: Dict[str, Any]) -> bool:
        """Internal fulfilment needs no courier pickup; always succeeds."""
        logger.info(
            "SHIPPING | manual: pickup is a no-op for internal shipment %s",
            shipment.get("id"),
        )
        return True

    @staticmethod
    def available() -> bool:
        """Manual fulfilment is always available (no credentials required)."""
        return True
