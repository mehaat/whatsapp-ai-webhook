"""
tests/test_v10_voice.py
------------------------
Offline (no-network) tests for the v10.0 Voice Agent (``agents/voice.py``).

These tests never hit the network: transcription is exercised only with an
empty Gemini key (which short-circuits to ``None``) or by monkeypatching
``agents.voice.transcribe`` directly.
"""

from __future__ import annotations

import types

import agents.voice as voice


def test_transcribe_returns_none_without_key(monkeypatch):
    """With voice enabled but no Gemini key, transcribe returns None and the
    voice agent reports itself unavailable (offline/test path)."""
    monkeypatch.setattr(
        voice,
        "config",
        types.SimpleNamespace(
            voice_enabled=True,
            gemini_api_key="",
            gemini_model="x",
            request_timeout_seconds=5,
        ),
    )
    assert voice.transcribe(b"abc") is None
    assert voice.voice_available() is False


def test_handle_voice_graceful_fallback(monkeypatch):
    """When transcription yields None, handle_voice returns a friendly,
    non-empty message asking the customer to type."""
    monkeypatch.setattr(
        voice,
        "config",
        types.SimpleNamespace(
            voice_enabled=True,
            gemini_api_key="",
            gemini_model="x",
            request_timeout_seconds=5,
        ),
    )
    reply = voice.handle_voice("9198000000", b"abc")
    assert isinstance(reply, str)
    assert reply
    assert "type" in reply.lower()


def test_handle_voice_routes_through_orchestrator(monkeypatch):
    """A successful transcript is routed through the orchestrator and produces
    a non-empty reply (deterministic fallback with no key)."""
    import commerce

    commerce.bootstrap()

    monkeypatch.setattr(voice, "transcribe", lambda *a, **k: "where is my order")

    reply = voice.handle_voice("9198000000", b"abc")
    assert isinstance(reply, str)
    assert reply.strip()
    assert "where is my order" in reply


def test_synthesize_speech_is_stub():
    """TTS is not configured; synthesize_speech returns None."""
    assert voice.synthesize_speech("hello") is None
