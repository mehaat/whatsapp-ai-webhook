"""
ai/gemini.py
------------
Google Gemini 2.5 Flash integration for ME-HAAT Fashion AI Bot v3.0.

Wraps the Gemini `generateContent` REST endpoint with retry/backoff,
timeout handling, and safe fallback messages so the WhatsApp layer never
has to deal with raw API failures.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

import requests

from ai.prompts import assemble_system_prompt, build_conversation_prompt
from config import config
from utils.logging import log_execution_time, logger

GEMINI_API_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

FALLBACK_MESSAGE = "I don't have confirmed information. Please contact our support team."
ERROR_MESSAGE = (
    "Sorry, something went wrong on our end. Please try again in a moment, "
    "or contact our support team for immediate help."
)
QUOTA_MESSAGE = (
    "We're experiencing high demand right now. Please try again shortly, "
    "or contact our support team."
)


@log_execution_time
def generate_reply(
    history: List[Dict[str, str]],
    customer_name: str,
    language: str,
    verified_context: str,
    user_message: str,
) -> str:
    """Generate a grounded AI reply using Gemini 2.5 Flash.

    Args:
        history: Conversation history (list of {"role", "text"} dicts).
        customer_name: WhatsApp profile name.
        language: Detected language.
        verified_context: Verified grounding data (FAQ / Shopify results).
        user_message: Sanitized current user message.

    Returns:
        The AI-generated reply, or a safe fallback message on failure.
    """
    if not config.gemini_api_key:
        logger.error("GEMINI | Missing GEMINI_API_KEY; cannot generate AI reply")
        return ERROR_MESSAGE

    prompt_text = build_conversation_prompt(
        history=history,
        customer_name=customer_name,
        language=language,
        verified_context=verified_context,
        user_message=user_message,
    )

    request_body = {
        "system_instruction": {"parts": [{"text": assemble_system_prompt()}]},
        "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 512,
            "topP": 0.9,
        },
    }

    url = GEMINI_API_URL_TEMPLATE.format(model=config.gemini_model)
    response = _post_with_retries(url, request_body)

    if response is None:
        return ERROR_MESSAGE
    if response == "QUOTA_EXCEEDED":
        return QUOTA_MESSAGE

    return _extract_text(response) or FALLBACK_MESSAGE


def _post_with_retries(url: str, body: Dict) -> Optional[object]:
    """POST to the Gemini API with retry/backoff. Returns parsed JSON, a
    sentinel string on quota errors, or None on unrecoverable failure.
    """
    last_error: Optional[str] = None

    for attempt in range(1, config.max_retries + 1):
        try:
            response = requests.post(
                url,
                params={"key": config.gemini_api_key},
                json=body,
                timeout=config.request_timeout_seconds,
            )
        except requests.exceptions.Timeout:
            last_error = "timeout"
            logger.warning("GEMINI | Timeout on attempt %d/%d", attempt, config.max_retries)
            _backoff(attempt)
            continue
        except requests.exceptions.RequestException as exc:
            last_error = str(exc)
            logger.warning(
                "GEMINI | Request error on attempt %d/%d: %s", attempt, config.max_retries, exc
            )
            _backoff(attempt)
            continue

        if response.status_code == 429:
            logger.error("GEMINI | Quota exceeded")
            return "QUOTA_EXCEEDED"

        if response.status_code == 200:
            try:
                return response.json()
            except ValueError:
                logger.error("GEMINI | Invalid JSON in successful response")
                return None

        if 500 <= response.status_code < 600:
            logger.warning(
                "GEMINI | Server error %d on attempt %d/%d",
                response.status_code, attempt, config.max_retries,
            )
            _backoff(attempt)
            continue

        logger.error(
            "GEMINI | API returned status %d: %s", response.status_code, response.text[:500]
        )
        return None

    logger.error("GEMINI | Exhausted retries: %s", last_error)
    return None


def _extract_text(data: Dict) -> str:
    """Extract the generated text from a Gemini `generateContent` response."""
    try:
        candidates = data.get("candidates", [])
        if not candidates:
            logger.warning("GEMINI | No candidates returned")
            return ""

        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts).strip()
        return text
    except (AttributeError, KeyError, IndexError) as exc:
        logger.error("GEMINI | Failed to parse response: %s", exc)
        return ""


def _backoff(attempt: int) -> None:
    """Exponential backoff sleep between retries."""
    time.sleep(min(2 ** attempt * 0.25, 4.0))
