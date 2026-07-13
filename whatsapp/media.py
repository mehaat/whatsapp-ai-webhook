"""
whatsapp/media.py
------------------
Download inbound WhatsApp media (images) via the Meta Graph API so v9.0 visual
search can run on customer-sent photos. Two-step flow: resolve the media id to
a temporary URL, then download the bytes with the same Bearer token. Fully
guarded — returns None on any failure so the webhook never breaks.
"""

from __future__ import annotations

from typing import Optional

import requests

from config import config
from utils.logging import logger

_GRAPH = "https://graph.facebook.com"


def download_media(media_id: str, *, max_bytes: int = 8 * 1024 * 1024) -> Optional[bytes]:
    """Return the raw bytes for a WhatsApp media id, or None on failure."""
    if not media_id or not config.whatsapp_token:
        return None
    headers = {"Authorization": f"Bearer {config.whatsapp_token}"}
    try:
        meta = requests.get(
            f"{_GRAPH}/{config.whatsapp_api_version}/{media_id}",
            headers=headers, timeout=config.request_timeout_seconds,
        )
        if meta.status_code >= 400:
            logger.warning("MEDIA | lookup failed (%s): %s", meta.status_code, meta.text[:200])
            return None
        url = meta.json().get("url")
        if not url:
            return None
        resp = requests.get(
            url, headers=headers, timeout=config.request_timeout_seconds, stream=True
        )
        if resp.status_code >= 400:
            logger.warning("MEDIA | download failed (%s)", resp.status_code)
            return None
        content = resp.content
        if len(content) > max_bytes:
            logger.warning("MEDIA | media too large (%d bytes); skipping", len(content))
            return None
        return content
    except Exception as exc:  # noqa: BLE001 - media is best-effort
        logger.error("MEDIA | download error: %s", exc)
        return None
