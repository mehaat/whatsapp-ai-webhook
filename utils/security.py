"""
utils/security.py
------------------
Security helpers shared across the app:
    - Input sanitization for messages sent to the LLM
    - Prompt-injection pattern detection
    - Shopify HMAC validation (OAuth callback + webhooks)
    - CSRF-style OAuth `state` token generation/validation
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from typing import Dict, Mapping, Tuple

from utils.logging import logger

# --------------------------------------------------------------------------
# Prompt injection defense
# --------------------------------------------------------------------------

_INJECTION_PATTERNS = [
    r"ignore (all )?(previous|above) instructions",
    r"you are now",
    r"system prompt",
    r"reveal (your|the) (prompt|instructions|api key|token|secret)",
    r"print (your|the) (env|environment|secrets|api key)",
    r"act as (?!a sales)",
    r"jailbreak",
    r"disregard (all )?(rules|policies)",
    r"developer mode",
    r"pretend (you|to) (are|be)",
]
_INJECTION_REGEX = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)

_MAX_MESSAGE_LENGTH = 1500


def sanitize_input(text: str) -> str:
    """Sanitize incoming user text before it reaches the LLM.

    Args:
        text: Raw text received from WhatsApp.

    Returns:
        Cleaned text safe to embed into an LLM prompt.
    """
    if not text:
        return ""

    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    cleaned = cleaned.strip()

    if len(cleaned) > _MAX_MESSAGE_LENGTH:
        cleaned = cleaned[:_MAX_MESSAGE_LENGTH]

    if _INJECTION_REGEX.search(cleaned):
        logger.warning("SECURITY | Potential prompt-injection attempt detected")

    return cleaned


def contains_injection_attempt(text: str) -> bool:
    """Return True if the text matches known prompt-injection patterns."""
    return bool(_INJECTION_REGEX.search(text or ""))


# --------------------------------------------------------------------------
# Shopify HMAC validation
# --------------------------------------------------------------------------

def verify_shopify_hmac(query_params: Mapping[str, str], secret: str) -> bool:
    """Validate the HMAC signature on a Shopify OAuth callback request.

    Args:
        query_params: The full set of query string parameters from the
            callback request (including ``hmac``).
        secret: The app's Shopify API secret.

    Returns:
        True if the signature is valid, False otherwise.
    """
    if not secret:
        logger.error("SECURITY | Cannot verify HMAC: missing SHOPIFY_API_SECRET")
        return False

    params = dict(query_params)
    provided_hmac = params.pop("hmac", None)
    if not provided_hmac:
        return False

    # Shopify requires params sorted and joined as key=value pairs
    message = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
    computed = hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    is_valid = hmac.compare_digest(computed, provided_hmac)
    if not is_valid:
        logger.warning("SECURITY | Shopify OAuth HMAC validation failed")
    return is_valid


def verify_shopify_webhook_hmac(raw_body: bytes, provided_signature: str, secret: str) -> bool:
    """Validate the HMAC-SHA256 signature Shopify sends on webhook requests.

    Shopify sends this as the base64-encoded ``X-Shopify-Hmac-Sha256`` header.

    Args:
        raw_body: The raw (unparsed) request body bytes.
        provided_signature: Value of the ``X-Shopify-Hmac-Sha256`` header.
        secret: The app's Shopify webhook secret (or API secret).

    Returns:
        True if the signature is valid, False otherwise.
    """
    if not secret or not provided_signature:
        return False

    computed = base64.b64encode(
        hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    ).decode("utf-8")

    is_valid = hmac.compare_digest(computed, provided_signature)
    if not is_valid:
        logger.warning("SECURITY | Shopify webhook HMAC validation failed")
    return is_valid


def is_valid_shop_domain(shop: str) -> bool:
    """Validate that a `shop` query parameter looks like a genuine myshopify.com domain.

    This prevents open-redirect / SSRF-style abuse of the install endpoint.
    """
    if not shop:
        return False
    return bool(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9\-]*\.myshopify\.com", shop))


# --------------------------------------------------------------------------
# OAuth CSRF `state` token handling
# --------------------------------------------------------------------------

_STATE_TTL_SECONDS = 600  # 10 minutes
_pending_states: Dict[str, float] = {}


def generate_oauth_state() -> str:
    """Generate and register a single-use, time-limited CSRF state token."""
    token = secrets.token_urlsafe(32)
    _pending_states[token] = time.time()
    _cleanup_expired_states()
    return token


def validate_oauth_state(token: str) -> bool:
    """Validate and consume a CSRF state token from the OAuth callback.

    A token can only be used once and expires after ``_STATE_TTL_SECONDS``.
    """
    _cleanup_expired_states()
    issued_at = _pending_states.pop(token, None)
    if issued_at is None:
        logger.warning("SECURITY | OAuth state token invalid or already used")
        return False
    if time.time() - issued_at > _STATE_TTL_SECONDS:
        logger.warning("SECURITY | OAuth state token expired")
        return False
    return True


def _cleanup_expired_states() -> None:
    """Remove expired pending OAuth state tokens."""
    now = time.time()
    expired = [tok for tok, ts in _pending_states.items() if now - ts > _STATE_TTL_SECONDS]
    for tok in expired:
        _pending_states.pop(tok, None)


# --------------------------------------------------------------------------
# PII masking (v4.0) — used before writing customer text to logs / storage
# --------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]*(@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
_PHONE_RE = re.compile(r"(?<!\d)(\+?\d[\d\s\-]{7,}\d)(?!\d)")


def mask_pii(text: str) -> str:
    """Mask emails and phone numbers in free text for safe logging/storage.

    Examples:
        "call me on +91 98765 43210" -> "call me on +91 *****3210"
        "a@b.com"                    -> "a****@b.com"
    """
    if not text:
        return text or ""

    def _mask_email(match: "re.Match") -> str:
        return f"{match.group(1)}****{match.group(2)}"

    def _mask_phone(match: "re.Match") -> str:
        digits = re.sub(r"\D", "", match.group(1))
        if len(digits) < 4:
            return "****"
        return "*****" + digits[-4:]

    masked = _EMAIL_RE.sub(_mask_email, text)
    masked = _PHONE_RE.sub(_mask_phone, masked)
    return masked


# --------------------------------------------------------------------------
# Optional token encryption at rest (v4.0)
# --------------------------------------------------------------------------
#
# When ``TOKEN_ENCRYPTION_KEY`` (a urlsafe base64 Fernet key) is configured and
# the ``cryptography`` package is installed, access tokens can be encrypted
# before persistence. When either is absent, these helpers pass the value
# through unchanged, preserving the exact v3.0 plaintext behaviour.

try:  # pragma: no cover - exercised only when cryptography is installed
    from cryptography.fernet import Fernet, InvalidToken  # type: ignore

    _CRYPTO_AVAILABLE = True
except Exception:  # noqa: BLE001
    Fernet = None  # type: ignore
    InvalidToken = Exception  # type: ignore
    _CRYPTO_AVAILABLE = False

_ENC_PREFIX = "enc::"


def _get_cipher():
    """Return a Fernet cipher if a key is configured and crypto is available."""
    if not _CRYPTO_AVAILABLE:
        return None
    try:
        from config import config

        key = config.token_encryption_key
    except Exception:  # noqa: BLE001
        key = ""
    if not key:
        return None
    try:
        return Fernet(key.encode("utf-8") if isinstance(key, str) else key)
    except Exception as exc:  # noqa: BLE001
        logger.error("SECURITY | Invalid TOKEN_ENCRYPTION_KEY: %s", exc)
        return None


def generate_encryption_key() -> str:
    """Generate a new Fernet key (utility for provisioning)."""
    if not _CRYPTO_AVAILABLE:
        raise RuntimeError("cryptography is not installed")
    return Fernet.generate_key().decode("utf-8")


def encrypt_token(value: str) -> str:
    """Encrypt a token for storage. Returns plaintext unchanged if disabled."""
    if not value:
        return value
    cipher = _get_cipher()
    if cipher is None:
        return value
    token = cipher.encrypt(value.encode("utf-8")).decode("utf-8")
    return _ENC_PREFIX + token


def decrypt_token(value: str) -> str:
    """Decrypt a stored token. Handles both plaintext and encrypted values."""
    if not value or not value.startswith(_ENC_PREFIX):
        return value
    cipher = _get_cipher()
    if cipher is None:
        logger.error("SECURITY | Encrypted token found but no cipher available")
        return value
    try:
        return cipher.decrypt(value[len(_ENC_PREFIX):].encode("utf-8")).decode("utf-8")
    except InvalidToken:
        logger.error("SECURITY | Failed to decrypt token (invalid key?)")
        return value
