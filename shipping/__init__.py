"""
shipping
--------
ME-HAAT Fashion AI Bot v7.0 fulfilment & shipping package.

A courier-agnostic adapter layer (mirroring the ``payments`` package):

* :mod:`shipping.base`      — the :class:`ShippingProvider` ABC + result objects.
* :mod:`shipping.manual`    — always-available offline internal fulfilment.
* :mod:`shipping.shiprocket`, :mod:`shipping.delhivery` — REST courier adapters.
* :mod:`shipping.factory`   — provider selection from ``config.shipping_provider``.
* :mod:`shipping.service`   — defensive orchestration (create/track/pickup/list).

Import-safe: importing this package never triggers network or database work.
The concrete adapters and the orchestration service are exposed lazily via the
convenience wrappers below so a missing optional dependency in one adapter never
breaks the whole package import.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from shipping.base import ShipmentResult, ShippingProvider, TrackingResult
from shipping.factory import PROVIDERS, available_providers, get_provider

__all__ = [
    "ShipmentResult",
    "TrackingResult",
    "ShippingProvider",
    "PROVIDERS",
    "available_providers",
    "get_provider",
    "create_shipment_for_order",
    "track_shipment",
    "schedule_pickup",
    "list_shipments",
    "get_shipment",
]


def create_shipment_for_order(
    order_id: int, provider_name: Optional[str] = None
) -> Dict[str, Any]:
    """Facade for :func:`shipping.service.create_shipment_for_order`."""
    from shipping.service import create_shipment_for_order as _impl

    return _impl(order_id, provider_name)


def track_shipment(
    order_id: Optional[int] = None, awb: Optional[str] = None
) -> Dict[str, Any]:
    """Facade for :func:`shipping.service.track_shipment`."""
    from shipping.service import track_shipment as _impl

    return _impl(order_id=order_id, awb=awb)


def schedule_pickup(shipment_id: int) -> Dict[str, Any]:
    """Facade for :func:`shipping.service.schedule_pickup`."""
    from shipping.service import schedule_pickup as _impl

    return _impl(shipment_id)


def list_shipments(
    status: Optional[str] = None, limit: int = 100, offset: int = 0
) -> List[Dict[str, Any]]:
    """Facade for :func:`shipping.service.list_shipments`."""
    from shipping.service import list_shipments as _impl

    return _impl(status=status, limit=limit, offset=offset)


def get_shipment(shipment_id: int) -> Optional[Dict[str, Any]]:
    """Facade for :func:`shipping.service.get_shipment`."""
    from shipping.service import get_shipment as _impl

    return _impl(shipment_id)
