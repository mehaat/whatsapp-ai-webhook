"""
shipping/delhivery.py
----------------------
Delhivery courier adapter using the Delhivery REST API.

* create -> ``POST /api/cmu/create.json`` with a JSON shipment payload,
  authenticated by a bearer token; returns a waybill (AWB) per package.
* track  -> ``GET /api/v1/packages/json/?waybill={awb}``.

Requires ``delhivery_token``. Missing credentials raise a clear
:class:`RuntimeError`.
"""

from __future__ import annotations

import json
from typing import Any, Dict

import requests

from config import config
from utils.logging import logger

from shipping.base import ShipmentResult, ShippingProvider, TrackingResult

_BASE_URL = "https://track.delhivery.com"
_CREATE_URL = f"{_BASE_URL}/api/cmu/create.json"
_TRACK_URL = f"{_BASE_URL}/api/v1/packages/json/"

# Delhivery textual statuses -> normalized status.
_STATUS_MAP = {
    "delivered": "delivered",
    "dispatched": "out_for_delivery",
    "in transit": "in_transit",
    "manifested": "created",
    "pending": "created",
    "rto": "returned",
    "returned": "returned",
    "canceled": "cancelled",
    "cancelled": "cancelled",
}


class DelhiveryProvider(ShippingProvider):
    """Delhivery REST API adapter."""

    @property
    def name(self) -> str:
        """Return the provider name."""
        return "delhivery"

    def available(self) -> bool:
        """Return True when the API token is configured."""
        return bool(config.delhivery_token)

    def _timeout(self) -> int:
        return int(getattr(config, "request_timeout_seconds", 15) or 15)

    def _headers(self, *, form: bool = False) -> Dict[str, str]:
        if not self.available():
            raise RuntimeError("Delhivery credentials missing (DELHIVERY_TOKEN)")
        headers = {
            "Authorization": f"Token {config.delhivery_token}",
            "Accept": "application/json",
        }
        if form:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        return headers

    def create_shipment(self, order: Dict[str, Any]) -> ShipmentResult:
        """Create a Delhivery shipment (waybill) for the order.

        Args:
            order: The order dict.

        Returns:
            A :class:`ShipmentResult` describing the created shipment.

        Raises:
            RuntimeError: If credentials are missing or the API call fails.
        """
        if not self.available():
            raise RuntimeError("Delhivery credentials missing (DELHIVERY_TOKEN)")

        order_number = str(order.get("order_number") or order.get("id") or "")
        shipment = {
            "name": order.get("customer_name") or "Customer",
            "order": order_number,
            "phone": order.get("wa_number") or "",
            "address": order.get("city") or "N/A",
            "city": order.get("city") or "N/A",
            "state": order.get("state") or "N/A",
            "country": "India",
            "pin": order.get("pincode") or "000000",
            "payment_mode": "Prepaid" if order.get("payment_status") == "paid" else "COD",
            "total_amount": float(order.get("total_amount") or 0),
            "cod_amount": 0 if order.get("payment_status") == "paid"
            else float(order.get("total_amount") or 0),
        }
        payload = {
            "shipments": [shipment],
            "pickup_location": {
                "name": getattr(config, "business_name", "ME-HAAT Fashion"),
                "add": getattr(config, "business_address", "") or "N/A",
                "pin": getattr(config, "pickup_pincode", "") or "000000",
                "phone": getattr(config, "business_phone", "") or "",
            },
        }
        # Delhivery's create endpoint expects a form-encoded body of the form
        # ``format=json&data=<json>``.
        body = "format=json&data=" + json.dumps(payload)

        try:
            resp = requests.post(
                _CREATE_URL, data=body, headers=self._headers(form=True),
                timeout=self._timeout(),
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.error("SHIPPING | delhivery: create_shipment failed: %s", exc)
            raise RuntimeError(f"Delhivery create_shipment failed: {exc}") from exc

        packages = data.get("packages") or []
        awb = None
        if packages:
            awb = packages[0].get("waybill") or packages[0].get("refnum")
        if not awb and not data.get("success", True):
            raise RuntimeError(f"Delhivery create returned no waybill: {data}")

        tracking_url = None
        if awb:
            tracking_url = f"{_BASE_URL}/track/package/{awb}"

        logger.info(
            "SHIPPING | delhivery: created shipment awb=%s for order %s",
            awb, order_number,
        )
        return ShipmentResult(
            ok=bool(awb),
            provider=self.name,
            awb=str(awb) if awb else None,
            courier_name="Delhivery",
            label_url=None,
            tracking_url=tracking_url,
            provider_shipment_id=str(awb) if awb else None,
            status="created",
            raw=data,
        )

    def track(self, awb: str) -> TrackingResult:
        """Query Delhivery package tracking for a waybill.

        Args:
            awb: The waybill number.

        Returns:
            A normalized :class:`TrackingResult`; ``ok=False`` on failure.
        """
        try:
            resp = requests.get(
                _TRACK_URL, params={"waybill": awb}, headers=self._headers(),
                timeout=self._timeout(),
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.error("SHIPPING | delhivery: track failed: %s", exc)
            return TrackingResult(ok=False, status="unknown", raw={"error": str(exc)})

        shipments = (data.get("ShipmentData") or [])
        raw_status = ""
        checkpoints = []
        if shipments:
            shipment = shipments[0].get("Shipment", {}) or {}
            raw_status = (shipment.get("Status", {}) or {}).get("Status", "").lower()
            for scan in shipment.get("Scans", []) or []:
                detail = scan.get("ScanDetail", {}) or {}
                checkpoints.append({
                    "status": detail.get("Scan") or "",
                    "location": detail.get("ScannedLocation") or "",
                    "note": detail.get("Instructions") or "",
                    "time": detail.get("ScanDateTime") or "",
                })
        status = _STATUS_MAP.get(raw_status, "in_transit" if checkpoints else "unknown")
        return TrackingResult(ok=True, status=status, checkpoints=checkpoints, raw=data)

    def schedule_pickup(self, shipment: Dict[str, Any]) -> bool:
        """Request a Delhivery pickup for a persisted shipment (best-effort)."""
        try:
            resp = requests.post(
                f"{_BASE_URL}/fm/request/new/",
                json={
                    "pickup_location": getattr(config, "business_name", "ME-HAAT Fashion"),
                    "expected_package_count": 1,
                },
                headers=self._headers(),
                timeout=self._timeout(),
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - pickup is best-effort
            logger.error("SHIPPING | delhivery: schedule_pickup failed: %s", exc)
            return False
        logger.info("SHIPPING | delhivery: pickup scheduled for shipment %s",
                    shipment.get("id"))
        return True
