"""
agents/voice.py
----------------
The v10.0 Voice Agent: inbound WhatsApp voice notes -> transcription ->
orchestrator.

WhatsApp voice notes arrive as ``type == "audio"`` messages. The webhook layer
downloads the raw bytes via :func:`whatsapp.media.download_media` and hands them
here. :func:`transcribe` turns the audio into text using Gemini's multimodal
``generateContent`` endpoint (mirroring :mod:`ai.gemini`), and
:func:`handle_voice` routes that transcript through the multi-agent
:data:`agents.orchestrator.orchestrator`.

The module degrades gracefully: when voice is disabled, no Gemini key is
configured, or transcription fails for any reason, the caller still receives a
friendly "please type your question" reply instead of an error. Nothing here
raises to the caller.
"""

from __future__ import annotations

import base64
from typing import Optional

import requests

from config import config
from utils.logging import logger

# Same REST surface as :mod:`ai.gemini` — Gemini's multimodal generateContent
# endpoint accepts inline audio parts alongside a text instruction.
GEMINI_API_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

_TRANSCRIBE_INSTRUCTION = "Transcribe this audio to text. Return only the transcript."

# Friendly fallback shown when we receive a voice note but cannot transcribe it.
GRACEFUL_FALLBACK = (
    "🎤 I got your voice note but couldn't process the audio right now. "
    "Please type your question and I'll help right away."
)


def voice_available() -> bool:
    """Return True when voice transcription is configured and enabled.

    Both :attr:`config.voice_enabled` and :attr:`config.gemini_api_key` must be
    set for real transcription to be possible.

    Returns:
        True if the voice agent can attempt transcription, else False.
    """
    return bool(config.voice_enabled and config.gemini_api_key)


def transcribe(audio_bytes: bytes, *, mime: str = "audio/ogg") -> Optional[str]:
    """Transcribe audio bytes to text using Gemini, or return None.

    This is fully guarded and never raises. When voice is disabled, no Gemini
    key is configured, or the audio is empty, it short-circuits to ``None`` (the
    offline/test path). Otherwise it POSTs the base64-encoded audio as an
    ``inline_data`` part to the Gemini ``generateContent`` endpoint and returns
    the parsed transcript.

    Args:
        audio_bytes: The raw audio payload (e.g. from
            :func:`whatsapp.media.download_media`).
        mime: The audio MIME type (WhatsApp voice notes are ``audio/ogg``).

    Returns:
        The stripped transcript, or ``None`` if transcription is unavailable,
        fails, or yields empty text.
    """
    if not config.voice_enabled or not config.gemini_api_key or not audio_bytes:
        return None

    try:
        encoded = base64.b64encode(audio_bytes).decode("ascii")
        request_body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"inline_data": {"mime_type": mime, "data": encoded}},
                        {"text": _TRANSCRIBE_INSTRUCTION},
                    ],
                }
            ],
            "generationConfig": {"temperature": 0.0},
        }
        url = GEMINI_API_URL_TEMPLATE.format(model=config.gemini_model)
        response = requests.post(
            url,
            params={"key": config.gemini_api_key},
            json=request_body,
            timeout=config.request_timeout_seconds,
        )
        if response.status_code != 200:
            logger.warning(
                "VOICE | transcription API returned status %d: %s",
                response.status_code,
                response.text[:300],
            )
            return None

        transcript = _extract_text(response.json())
        return transcript or None
    except Exception as exc:  # noqa: BLE001 - transcription must never crash
        logger.error("VOICE | transcription failed: %s", exc)
        return None


def handle_voice(wa_number: str, audio_bytes: bytes, *, mime: str = "audio/ogg") -> str:
    """Handle an inbound WhatsApp voice note end to end; never raises.

    Transcribes the audio and, when successful, routes the transcript through
    the multi-agent orchestrator, returning a reply that echoes what was heard
    followed by the agent's answer. When transcription is unavailable it returns
    a friendly message asking the customer to type instead.

    Args:
        wa_number: The customer's WhatsApp number (E.164 digits, no ``+``).
        audio_bytes: The raw downloaded audio payload.
        mime: The audio MIME type (default ``audio/ogg`` for voice notes).

    Returns:
        A non-empty reply string safe to send back to the customer.
    """
    try:
        transcript = transcribe(audio_bytes, mime=mime)
    except Exception as exc:  # noqa: BLE001 - defensive; transcribe already guards
        logger.error("VOICE | handle_voice transcription error: %s", exc)
        transcript = None

    if not transcript:
        return GRACEFUL_FALLBACK

    try:
        from agents.orchestrator import orchestrator

        resp = orchestrator.route(
            transcript, {"channel": "whatsapp", "wa_number": wa_number}
        )
        reply = (resp.text or "").strip()
        if not reply:
            return GRACEFUL_FALLBACK
        return f"🎤 I heard: {transcript}\n\n{reply}"
    except Exception as exc:  # noqa: BLE001 - routing must never crash the webhook
        logger.error("VOICE | orchestrator routing failed: %s", exc)
        return GRACEFUL_FALLBACK


def synthesize_speech(text: str) -> Optional[bytes]:
    """Convert reply text to speech audio (TTS) — stub, returns None.

    Text-to-speech is not configured in this build. To enable outbound voice
    replies, plug a TTS provider in here (e.g. Google Cloud Text-to-Speech,
    ElevenLabs, or Gemini TTS): synthesize ``text`` into audio bytes, upload
    them to WhatsApp via the media API, and send an ``audio`` message. Until
    then this returns ``None`` so callers fall back to a text reply.

    Args:
        text: The reply text that would be spoken.

    Returns:
        ``None`` (TTS not configured).
    """
    return None


def _extract_text(data: dict) -> str:
    """Extract the transcript text from a Gemini ``generateContent`` response."""
    try:
        candidates = data.get("candidates", [])
        if not candidates:
            return ""
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(part.get("text", "") for part in parts).strip()
    except (AttributeError, KeyError, IndexError) as exc:
        logger.error("VOICE | failed to parse transcription response: %s", exc)
        return ""
