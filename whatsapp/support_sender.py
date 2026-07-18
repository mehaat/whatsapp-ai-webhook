"""
whatsapp/support_sender.py
--------------------------
Outbound WhatsApp Cloud API helpers for the Admin Support Console (v10.2).

These complement — and never modify — the existing :mod:`whatsapp.sender`. The
difference is that every send here returns the **WhatsApp message id (wamid)**
so the console can track delivery/read status, and media (image / PDF / voice)
is uploaded to Meta's ``/media`` endpoint first and then sent by ``media_id``
(no public file hosting required).

Public API
    upload_media(content, mime_type, filename) -> media_id | None
    send_text(to, text)                         -> wamid | None
    send_image(to, media_id, caption)           -> wamid | None
    send_document(to, media_id, filename, cap)  -> wamid | None
    send_audio(to, media_id)                    -> wamid | None
    send_media_upload(to, content, mime, ...)   -> (wamid, media_id) convenience

All functions are exception-guarded and return ``None`` on failure (with a
logged reason), so a send failure can never crash the console request.
"""

from __future__ import annotations

import mimetypes
from typing import Optional, Tuple

import requests

from config import config
from utils.logging import logger

# WhatsApp Graph API bases (built from the same config the sender uses).
_GRAPH = "https://graph.facebook.com"


def _base_url() -> str:
    return f"{_GRAPH}/{config.whatsapp_api_version}/{config.phone_number_id}"


def _auth_header() -> dict:
    return {"Authorization": f"Bearer {config.whatsapp_token}"}


def _configured() -> bool:
    if not config.whatsapp_token or not config.phone_number_id:
        logger.error("WHATSAPP | Missing WHATSAPP_TOKEN/PHONE_NUMBER_ID; cannot send")
        return False
    return True


def _timeout() -> float:
    return float(getattr(config, "request_timeout_seconds", 15) or 15)


def _extract_wamid(payload: dict) -> Optional[str]:
    """Pull the message id out of a successful send response."""
    try:
        messages = payload.get("messages") or []
        if messages and isinstance(messages, list):
            return messages[0].get("id")
    except Exception:  # noqa: BLE001
        pass
    return None


def _post_message(body: dict) -> Optional[str]:
    """POST a /messages body and return the wamid (or None on failure)."""
    if not _configured():
        return None
    headers = {**_auth_header(), "Content-Type": "application/json"}
    try:
        resp = requests.post(
            f"{_base_url()}/messages", headers=headers, json=body, timeout=_timeout()
        )
    except requests.RequestException as exc:
        logger.error("WHATSAPP | send request failed: %s", exc)
        return None
    if resp.status_code >= 400:
        logger.error("WHATSAPP | send failed (%d): %s", resp.status_code, resp.text[:500])
        return None
    wamid = _extract_wamid(resp.json() if resp.content else {})
    if not wamid:
        logger.warning("WHATSAPP | send ok but no wamid in response")
    return wamid


# --------------------------------------------------------------------------- #
# Media upload
# --------------------------------------------------------------------------- #
def upload_media(content: bytes, mime_type: str, filename: str = "upload") -> Optional[str]:
    """Upload media bytes to Meta and return the resulting ``media_id``.

    Args:
        content: Raw file bytes.
        mime_type: MIME type, e.g. ``image/jpeg``, ``application/pdf``,
            ``audio/ogg``.
        filename: A filename hint for the multipart part.

    Returns:
        The Meta ``media_id`` string, or ``None`` on failure.
    """
    if not _configured():
        return None
    if not content:
        logger.error("WHATSAPP | upload_media called with empty content")
        return None
    files = {
        "file": (filename, content, mime_type),
        "messaging_product": (None, "whatsapp"),
        "type": (None, mime_type),
    }
    try:
        resp = requests.post(
            f"{_base_url()}/media", headers=_auth_header(), files=files, timeout=_timeout() * 2
        )
    except requests.RequestException as exc:
        logger.error("WHATSAPP | media upload failed: %s", exc)
        return None
    if resp.status_code >= 400:
        logger.error("WHATSAPP | media upload error (%d): %s", resp.status_code, resp.text[:500])
        return None
    media_id = (resp.json() if resp.content else {}).get("id")
    if not media_id:
        logger.error("WHATSAPP | media upload returned no id")
        return None
    logger.info("WHATSAPP | media uploaded id=%s (%s)", media_id, mime_type)
    return media_id


def guess_mime(filename: str, fallback: str = "application/octet-stream") -> str:
    """Best-effort MIME type from a filename."""
    mime, _ = mimetypes.guess_type(filename)
    return mime or fallback


# --------------------------------------------------------------------------- #
# Typed sends (each returns a wamid)
# --------------------------------------------------------------------------- #
def send_text(to_number: str, text: str) -> Optional[str]:
    """Send a plain text message; returns the wamid."""
    if not text:
        return None
    body = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "text",
        "text": {"preview_url": True, "body": text[:4096]},
    }
    return _post_message(body)


def send_image(to_number: str, media_id: str, caption: str = "") -> Optional[str]:
    """Send an image by uploaded ``media_id`` (optional caption)."""
    image: dict = {"id": media_id}
    if caption:
        image["caption"] = caption[:1024]
    body = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "image",
        "image": image,
    }
    return _post_message(body)


def send_document(
    to_number: str, media_id: str, filename: str = "document.pdf", caption: str = ""
) -> Optional[str]:
    """Send a document (e.g. PDF) by uploaded ``media_id``."""
    document: dict = {"id": media_id, "filename": filename}
    if caption:
        document["caption"] = caption[:1024]
    body = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "document",
        "document": document,
    }
    return _post_message(body)


def send_audio(to_number: str, media_id: str) -> Optional[str]:
    """Send a voice note / audio by uploaded ``media_id``."""
    body = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "audio",
        "audio": {"id": media_id},
    }
    return _post_message(body)


def send_media_upload(
    to_number: str,
    content: bytes,
    mime_type: str,
    *,
    kind: str,
    filename: str = "upload",
    caption: str = "",
) -> Tuple[Optional[str], Optional[str]]:
    """Upload media then send it in one step.

    Args:
        kind: One of ``"image"``, ``"document"``, ``"audio"``.

    Returns:
        ``(wamid, media_id)`` — either may be ``None`` on failure.
    """
    media_id = upload_media(content, mime_type, filename)
    if not media_id:
        return None, None
    if kind == "image":
        wamid = send_image(to_number, media_id, caption)
    elif kind == "audio":
        wamid = send_audio(to_number, media_id)
    else:  # document / pdf / anything else
        wamid = send_document(to_number, media_id, filename, caption)
    return wamid, media_id


__all__ = [
    "upload_media",
    "guess_mime",
    "send_text",
    "send_image",
    "send_document",
    "send_audio",
    "send_media_upload",
]
