"""
shipping/base.py
-----------------
Core contracts for the ME-HAAT Fashion AI Bot v7.0 fulfilment & shipping system.

Every concrete courier adapter (Manual, Shiprocket, Delhivery) implements
:class:`ShippingProvider`. Two immutable value objects flow through the system:

* :class:`ShipmentResult`  — the outcome of creating a shipment/AWB with a courier.
* :class:`TrackingResult`  — the normalized outcome of a tracking query.

Keeping these contracts free of any courier-specific detail lets the factory,
the orchestration service and the package facade treat all providers uniformly.
This mirrors the ``payments`` adapter pattern (see ``payments/base.py``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ShipmentResult:
    """The result of creating a shipment with a courier.

    Attributes:
        ok: True when the shipment/AWB was created successfully.
        provider: The provider name that produced the shipment.
        awb: The air waybill / tracking number assigned by the courier.
        courier_name: Human-readable courier name (e.g. ``"Delhivery Surface"``).
        label_url: URL (or local path) to the shipping label PDF, if any.
        tracking_url: Customer-facing tracking URL, if any.
        provider_shipment_id: The courier's own shipment/order identifier.
        status: Normalized shipment status (defaults to ``"created"``).
        raw: The raw provider response payload for auditing/debugging.
    """

    ok: bool
    provider: str
    awb: Optional[str] = None
    courier_name: Optional[str] = None
    label_url: Optional[str] = None
    tracking_url: Optional[str] = None
    provider_shipment_id: Optional[str] = None
    status: str = "created"
    raw: Optional[Dict[str, Any]] = None


@dataclass
class TrackingResult:
    """The normalized outcome of a tracking query.

    Attributes:
        ok: True when tracking data was retrieved successfully.
        status: Normalized shipment status (e.g. ``in_transit``/``delivered``).
        checkpoints: Ordered list of scan/checkpoint dicts (best-effort).
        raw: The raw provider response payload for auditing/debugging.
    """

    ok: bool
    status: str
    checkpoints: List[Dict[str, Any]] = field(default_factory=list)
    raw: Optional[Dict[str, Any]] = None


class ShippingProvider(ABC):
    """Abstract base class every courier adapter must implement."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the short, stable provider name (e.g. ``"shiprocket"``)."""
        raise NotImplementedError

    @abstractmethod
    def create_shipment(self, order: Dict[str, Any]) -> ShipmentResult:
        """Create a shipment / AWB for the given order dict.

        Args:
            order: An order dict (see the v6 ORDER contract) with at least
                ``id``, ``order_number``, ``customer_name`` and ``total_amount``.

        Returns:
            A :class:`ShipmentResult` describing the created shipment.
        """
        raise NotImplementedError

    @abstractmethod
    def track(self, awb: str) -> TrackingResult:
        """Return the current tracking state for an AWB.

        Args:
            awb: The air waybill / tracking number to query.

        Returns:
            A normalized :class:`TrackingResult`. ``ok`` is False on failure.
        """
        raise NotImplementedError

    @abstractmethod
    def schedule_pickup(self, shipment: Dict[str, Any]) -> bool:
        """Schedule a courier pickup for a persisted shipment.

        Args:
            shipment: A shipment dict (see ``shipping.service``).

        Returns:
            ``True`` when the pickup was scheduled (or is a no-op), else False.
        """
        raise NotImplementedError
