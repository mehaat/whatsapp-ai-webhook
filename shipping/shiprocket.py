"""
shipping/shiprocket.py
-----------------------
Shiprocket courier adapter using the Shiprocket External REST API.

* auth      -> ``POST /v1/external/auth/login`` (email/password) returns a
  bearer token which is cached on the instance.
* create    -> ``POST /v1/external/orders/create/adhoc`` creates an order and
  (best-effort) requests an AWB.
* track     -> ``GET /v1/external/courier/track/awb/{awb}``.

Requires ``shiprocket_email`` / ``shiprocket_password``. Missing credentials
raise a clear :class:`RuntimeError`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

from config import config
from utils.logging import logger

from shipping.base import ShipmentResult, ShippingProvider, TrackingResult

_BASE_URL = "https://apiv2.shiprocket.in/v1/external"
_AUTH_URL = f"{_BASE_URL}/auth/login"
_CREATE_URL = f"{_BASE_URL}/orders/create/adhoc"
_TRACK_URL = f"{_BASE_URL}/courier/track/awb"

# Shiprocket textual statuses -> normalized status.
_STATUS_MAP = {
    "delivered": "delivered",
    "out for delivery": "out_for_delivery",
    "in transit": "in_transit",
    "picked up": "in_transit",
    "shipped": "in_transit",
    "rto": "returned",
    "cancelled": "cancelled",
}


class ShiprocketProvider(ShippingProvider):
    """Shiprocket External API adapter."""

    def __init__(self) -> None:
        self._token: Optional[str] = None

    @property
    def name(self) -> str:
        """Return the provider name."""
        return "shiprocket"

    def available(self) -> bool:
        """Return True when API credentials are configured."""
        return bool(config.shiprocket_email and config.shiprocket_password)

    def _timeout(self) -> int:
        return int(getattr(config, "request_timeout_seconds", 15) or 15)

    def _authenticate(self) -> str:
        """Log in and cache the bearer token on the instance.

        Returns:
            A bearer token string.

        Raises:
            RuntimeError: If credentials are missing or auth fails.
        """
        if self._token:
            return self._token
        if not self.available():
            raise RuntimeError(
                "Shiprocket credentials missing (SHIPROCKET_EMAIL / SHIPROCKET_PASSWORD)"
            )
        try:
            resp = requests.post(
                _AUTH_URL,
                json={
                    "email": config.shiprocket_email,
                    "password": config.shiprocket_password,
                },
                timeout=self._timeout(),
            )
            resp.raise_for_status()
            token = resp.json().get("token")
        except Exception as exc:  # noqa: BLE001 - normalized into RuntimeError
            logger.error("SHIPPING | shiprocket: auth failed: %s", exc)
            raise RuntimeError(f"Shiprocket auth failed: {exc}") from exc
        if not token:
            raise RuntimeError("Shiprocket auth response missing token")
        self._token = token
        return token

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._authenticate()}",
            "Content-Type": "application/json",
        }

    def create_shipment(self, order: Dict[str, Any]) -> ShipmentResult:
        """Create a Shiprocket ad-hoc order for the given order dict.

        Args:
            order: The order dict.

        Returns:
            A :class:`ShipmentResult` describing the created shipment.

        Raises:
            RuntimeError: If credentials are missing or the API call fails.
        """
        if not self.available():
            raise RuntimeError(
                "Shiprocket credentials missing (SHIPROCKET_EMAIL / SHIPROCKET_PASSWORD)"
            )

        order_number = str(order.get("order_number") or order.get("id") or "")
        items = order.get("items") or []
        order_items = [
            {
                "name": (it.get("product_name") or "Item")[:255],
                "sku": (it.get("product_retailer_id") or it.get("product_name") or "SKU")[:80],
                "units": int(it.get("quantity") or 1),
                "selling_price": float(it.get("unit_price") or 0),
            }
            for it in items
        ] or [{
            "name": f"Order {order_number}",
            "sku": order_number or "SKU",
            "units": 1,
            "selling_price": float(order.get("total_amount") or 0),
        }]

        payload: Dict[str, Any] = {
            "order_id": order_number,
            "order_date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "pickup_location": "Primary",
            "billing_customer_name": order.get("customer_name") or "Customer",
            "billing_last_name": "",
            "billing_address": order.get("city") or "N/A",
            "billing_city": order.get("city") or "N/A",
            "billing_pincode": order.get("pincode") or "000000",
            "billing_state": order.get("state") or "N/A",
            "billing_country": "India",
            "billing_email": order.get("email") or "",
            "billing_phone": order.get("wa_number") or "",
            "shipping_is_billing": True,
            "order_items": order_items,
            "payment_method": "Prepaid" if order.get("payment_status") == "paid" else "COD",
            "sub_total": float(order.get("total_amount") or 0),
            "length": 10, "breadth": 10, "height": 10, "weight": 0.5,
        }

        try:
            resp = requests.post(
                _CREATE_URL, json=payload, headers=self._headers(),
                timeout=self._timeout(),
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.error("SHIPPING | shiprocket: create_shipment failed: %s", exc)
            raise RuntimeError(f"Shiprocket create_shipment failed: {exc}") from exc

        shipment_id = data.get("shipment_id") or data.get("order_id")
        awb = data.get("awb_code") or data.get("awb")
        courier_name = data.get("courier_name")
        tracking_url = None
        if awb:
            tracking_url = f"https://shiprocket.co/tracking/{awb}"

        logger.info(
            "SHIPPING | shiprocket: created shipment=%s awb=%s for order %s",
            shipment_id, awb, order_number,
        )
        return ShipmentResult(
            ok=True,
            provider=self.name,
            awb=str(awb) if awb else None,
            courier_name=courier_name,
            label_url=data.get("label_url"),
            tracking_url=tracking_url,
            provider_shipment_id=str(shipment_id) if shipment_id else None,
            status="created",
            raw=data,
        )

    def track(self, awb: str) -> TrackingResult:
        """Query Shiprocket tracking for an AWB.

        Args:
            awb: The air waybill number.

        Returns:
            A normalized :class:`TrackingResult`; ``ok=False`` on failure.
        """
        try:
            resp = requests.get(
                f"{_TRACK_URL}/{awb}", headers=self._headers(),
                timeout=self._timeout(),
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.error("SHIPPING | shiprocket: track failed: %s", exc)
            return TrackingResult(ok=False, status="unknown", raw={"error": str(exc)})

        tracking = data.get("tracking_data", {}) or {}
        activities = tracking.get("shipment_track_activities", []) or []
        raw_status = ""
        track_list = tracking.get("shipment_track", []) or []
        if track_list:
            raw_status = (track_list[0].get("current_status") or "").lower()
        status = _STATUS_MAP.get(raw_status, "in_transit" if activities else "unknown")

        checkpoints = [
            {
                "status": a.get("status") or a.get("activity") or "",
                "location": a.get("location") or "",
                "note": a.get("activity") or "",
                "time": a.get("date") or "",
            }
            for a in activities
        ]
        return TrackingResult(ok=True, status=status, checkpoints=checkpoints, raw=data)

    def schedule_pickup(self, shipment: Dict[str, Any]) -> bool:
        """Request a courier pickup for a persisted shipment (best-effort)."""
        provider_shipment_id = shipment.get("provider_shipment_id")
        if not provider_shipment_id:
            logger.warning("SHIPPING | shiprocket: no provider_shipment_id for pickup")
            return False
        try:
            resp = requests.post(
                f"{_BASE_URL}/courier/generate/pickup",
                json={"shipment_id": [provider_shipment_id]},
                headers=self._headers(),
                timeout=self._timeout(),
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - pickup is best-effort
            logger.error("SHIPPING | shiprocket: schedule_pickup failed: %s", exc)
            return False
        logger.info("SHIPPING | shiprocket: pickup scheduled for %s", provider_shipment_id)
        return True
