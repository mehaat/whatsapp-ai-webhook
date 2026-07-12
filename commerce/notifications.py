"""
commerce/notifications.py
-------------------------
Bilingual (Hindi + English) WhatsApp customer notifications for the ME-HAAT
Fashion AI Bot v6.0 commerce flow, plus lightweight admin alerts.

Each customer notification:
    1. Builds the message text, respecting ``order["language"]`` (Hindi text
       when the order language is "hindi", otherwise English). For the most
       important lifecycle messages a bilingual block is emitted so nothing is
       lost regardless of the customer's language preference.
    2. Lazily sends it via :func:`whatsapp.sender.send_text_message`.
    3. Logs the attempt via ``order_service.log_notification``.
    4. Returns ``True`` on success, ``False`` otherwise.

Every function is resilient: a send/log failure is logged and ``False`` is
returned. Notifications never raise back into the caller.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from config import config
from utils.logging import logger


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------

def _is_hindi(order: Dict[str, Any]) -> bool:
    """Return True when the order's customer prefers Hindi."""
    language = str(order.get("language") or "").strip().lower()
    return language in {"hindi", "hi"}


def _send(to_number: Optional[str], text: str) -> bool:
    """Lazily send a WhatsApp text message, never raising.

    Args:
        to_number: Recipient WhatsApp number (E.164 digits, no '+').
        text: Message body to send.

    Returns:
        True if the send succeeded, False otherwise.
    """
    if not to_number:
        logger.warning("NOTIFY | missing recipient number; skipping send")
        return False
    try:
        from whatsapp.sender import send_text_message  # lazy import

        return bool(send_text_message(to_number, text))
    except Exception as exc:  # noqa: BLE001 - notifications must never raise
        logger.error("NOTIFY | send failed: %s", exc)
        return False


def _log(
    *,
    kind: str,
    order: Optional[Dict[str, Any]] = None,
    wa_number: Optional[str] = None,
    audience: str = "customer",
    body: Optional[str] = None,
    status: str = "sent",
) -> None:
    """Lazily record a notification in the audit log, never raising."""
    try:
        from commerce.service import order_service  # lazy import

        order_service.log_notification(
            kind=kind,
            order_id=(order or {}).get("id") if order is not None else None,
            wa_number=wa_number or (order or {}).get("wa_number"),
            audience=audience,
            body=body,
            status=status,
        )
    except Exception as exc:  # noqa: BLE001 - logging must never break sends
        logger.debug("NOTIFY | log_notification failed: %s", exc)


def _dispatch(order: Dict[str, Any], kind: str, text: str) -> bool:
    """Send ``text`` to the order's customer and record the outcome.

    Returns:
        True if the underlying send succeeded, False otherwise.
    """
    to_number = order.get("wa_number")
    ok = _send(to_number, text)
    _log(
        kind=kind,
        order=order,
        wa_number=to_number,
        audience="customer",
        body=text,
        status="sent" if ok else "failed",
    )
    return ok


# --------------------------------------------------------------------------
# Customer lifecycle notifications
# --------------------------------------------------------------------------

def notify_order_received(order: Dict[str, Any]) -> bool:
    """Notify the customer that their order has been received.

    Args:
        order: Order dict (see module contract for keys).

    Returns:
        True if the message was sent, False otherwise.
    """
    order_number = order.get("order_number", "")
    english = (
        "🙏 Thank you.\n\n"
        "Your order has been received.\n\n"
        "Order Number:\n"
        f"{order_number}\n\n"
        "We are preparing your order."
    )
    if _is_hindi(order):
        hindi = (
            "🙏 धन्यवाद।\n\n"
            "आपका ऑर्डर प्राप्त हो गया है।\n\n"
            "ऑर्डर नंबर:\n"
            f"{order_number}\n\n"
            "हम आपका ऑर्डर तैयार कर रहे हैं।"
        )
        text = f"{hindi}\n\n---\n\n{english}"
    else:
        text = english
    return _dispatch(order, "order_received", text)


def notify_order_confirmed(order: Dict[str, Any]) -> bool:
    """Notify the customer that their order has been confirmed.

    Args:
        order: Order dict.

    Returns:
        True if the message was sent, False otherwise.
    """
    estimate = config.delivery_estimate
    english = (
        "✅ Your order has been confirmed.\n\n"
        "Estimated Delivery:\n"
        f"{estimate}"
    )
    if _is_hindi(order):
        hindi = (
            "✅ आपका ऑर्डर कन्फर्म हो गया है।\n\n"
            "अनुमानित डिलीवरी:\n"
            f"{estimate}"
        )
        text = f"{hindi}\n\n---\n\n{english}"
    else:
        text = english
    return _dispatch(order, "order_confirmed", text)


def notify_payment_pending(order: Dict[str, Any], payment_link: str) -> bool:
    """Notify the customer that payment is pending, with a payment link.

    Args:
        order: Order dict.
        payment_link: URL the customer taps to complete payment.

    Returns:
        True if the message was sent, False otherwise.
    """
    english = (
        "💳 Payment Pending\n\n"
        "Click below to complete payment.\n\n"
        f"{payment_link}"
    )
    if _is_hindi(order):
        hindi = (
            "💳 भुगतान बाकी है\n\n"
            "भुगतान पूरा करने के लिए नीचे क्लिक करें।\n\n"
            f"{payment_link}"
        )
        text = f"{hindi}\n\n---\n\n{english}"
    else:
        text = english
    return _dispatch(order, "payment_pending", text)


def notify_order_shipped(order: Dict[str, Any]) -> bool:
    """Notify the customer that their order has shipped.

    Tracking number and courier are pulled from the order dict (falling back
    to the latest tracking event when the top-level fields are absent).

    Args:
        order: Order dict.

    Returns:
        True if the message was sent, False otherwise.
    """
    tracking_number = order.get("tracking_number")
    courier = order.get("courier")
    tracking_events = order.get("tracking") or []
    if (not tracking_number or not courier) and tracking_events:
        latest = tracking_events[-1] or {}
        tracking_number = tracking_number or latest.get("tracking_number")
        courier = courier or latest.get("courier")

    tracking_number = tracking_number or "N/A"
    courier = courier or "N/A"

    english = (
        "📦 Your order has been shipped.\n\n"
        "Tracking Number:\n"
        f"{tracking_number}\n\n"
        "Courier:\n"
        f"{courier}"
    )
    if _is_hindi(order):
        hindi = (
            "📦 आपका ऑर्डर भेज दिया गया है।\n\n"
            "ट्रैकिंग नंबर:\n"
            f"{tracking_number}\n\n"
            "कूरियर:\n"
            f"{courier}"
        )
        text = f"{hindi}\n\n---\n\n{english}"
    else:
        text = english
    return _dispatch(order, "order_shipped", text)


def notify_order_delivered(order: Dict[str, Any]) -> bool:
    """Notify the customer that their order has been delivered.

    Args:
        order: Order dict.

    Returns:
        True if the message was sent, False otherwise.
    """
    business_name = config.business_name
    english = (
        f"❤️ Thank you for shopping with {business_name}.\n\n"
        "Please share your feedback."
    )
    if _is_hindi(order):
        hindi = (
            f"❤️ {business_name} से खरीदारी करने के लिए धन्यवाद।\n\n"
            "कृपया अपनी प्रतिक्रिया साझा करें।"
        )
        text = f"{hindi}\n\n---\n\n{english}"
    else:
        text = english
    return _dispatch(order, "order_delivered", text)


# --------------------------------------------------------------------------
# Admin alerts
# --------------------------------------------------------------------------

def notify_admin(text: str, admin_number: str) -> bool:
    """Send an admin alert (new order / payment / low stock, etc.).

    Args:
        text: Alert body.
        admin_number: Admin WhatsApp number (E.164 digits, no '+').

    Returns:
        True if the alert was sent, False otherwise.
    """
    ok = _send(admin_number, text)
    _log(
        kind="admin_alert",
        wa_number=admin_number,
        audience="admin",
        body=text,
        status="sent" if ok else "failed",
    )
    return ok


# --------------------------------------------------------------------------
# Dispatcher
# --------------------------------------------------------------------------

def send_status_notification(
    order: Dict[str, Any],
    status: str,
    payment_link: Optional[str] = None,
) -> bool:
    """Dispatch the correct customer notification for an order status.

    Args:
        order: Order dict.
        status: One of ``received``, ``confirmed``, ``shipped``, ``delivered``
            or ``payment_pending``.
        payment_link: Payment URL, required when ``status`` is
            ``payment_pending``.

    Returns:
        True if a matching notification was sent, False otherwise (including
        unknown statuses or a missing payment link).
    """
    key = str(status or "").strip().lower()

    simple: Dict[str, Callable[[Dict[str, Any]], bool]] = {
        "received": notify_order_received,
        "confirmed": notify_order_confirmed,
        "shipped": notify_order_shipped,
        "delivered": notify_order_delivered,
    }

    if key in simple:
        return simple[key](order)

    if key in {"payment_pending", "payment"}:
        if not payment_link:
            logger.warning(
                "NOTIFY | payment_pending requested without a payment_link"
            )
            return False
        return notify_payment_pending(order, payment_link)

    logger.warning("NOTIFY | no notification mapped for status=%r", status)
    return False
