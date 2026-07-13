"""
commerce/apikeys.py
--------------------
Developer **API key** management for the ME-HAAT Fashion AI Bot v8.0.

A key is a single opaque string shaped like::

    mh_live_<prefix8>_<secret>

Only the ``prefix`` (a short public identifier) and a SHA-256 hash of the
*full* key are ever persisted (see :class:`database.models.ApiKey`). The
plaintext key is returned exactly once, at issue time, and can never be
recovered afterwards — losing it means minting a new one.

The public surface:

    * :func:`issue_key`      — mint a new key, returning the plaintext once.
    * :func:`verify_key`     — authenticate a presented key (constant-time).
    * :func:`check_rate_limit` — per-key in-memory sliding-window limiter.
    * :func:`list_keys`      — list stored keys (never any secret material).
    * :func:`get_key`        — fetch a single key by id (no secret).
    * :func:`revoke_key`     — deactivate a key.

Every helper is defensive: none of them raise to the caller. Failures are
logged and degrade to a safe value (``None`` / ``{}`` / ``[]`` / ``False``)
so key handling can never 500 the API or the admin dashboard.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

from database.db import session_scope
from database.models import ApiKey
from utils.logging import logger

# Human-visible product prefix on every key. ``mh_live_`` marks a live key.
_KEY_PREFIX = "mh_live_"
# Length (chars) of the short public prefix stored as ``ApiKey.prefix``.
_PREFIX_LEN = 8


# --------------------------------------------------------------------------
# Hashing / key material
# --------------------------------------------------------------------------

def _hash(full_key: str) -> str:
    """Return the hex SHA-256 digest of a full plaintext key."""
    return hashlib.sha256(full_key.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _parse_prefix(presented: str) -> Optional[str]:
    """Extract the 8-char public prefix from a presented key string.

    The secret tail may itself contain ``_`` (``token_urlsafe`` uses a
    URL-safe base64 alphabet), so the prefix is sliced positionally rather
    than by splitting on ``_``.

    Returns the prefix, or ``None`` when the string is not a well-formed key.
    """
    if not presented or not presented.startswith(_KEY_PREFIX):
        return None
    rest = presented[len(_KEY_PREFIX):]
    # rest == "<prefix8>_<secret>"
    if len(rest) < _PREFIX_LEN + 2 or rest[_PREFIX_LEN] != "_":
        return None
    return rest[:_PREFIX_LEN]


def _scopes_list(raw: Optional[str]) -> List[str]:
    """Normalize a CSV scopes string into a clean list."""
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


# --------------------------------------------------------------------------
# Audit (lazy, best-effort — never breaks key handling)
# --------------------------------------------------------------------------

def _audit(actor: str, action: str, entity_id: Any, detail: str) -> None:
    """Best-effort audit of a key mutation (never raises)."""
    try:
        from commerce.service import order_service

        order_service.audit(
            actor=actor or "system",
            action=action,
            entity="api_key",
            entity_id=str(entity_id) if entity_id is not None else None,
            detail=detail,
        )
    except Exception as exc:  # noqa: BLE001 - auditing must never break the op
        logger.debug("APIKEYS | audit %s failed: %s", action, exc)


# --------------------------------------------------------------------------
# Issue
# --------------------------------------------------------------------------

def issue_key(
    name: str,
    *,
    scopes: str = "read",
    tenant_id: Optional[int] = None,
    rate_limit_per_min: int = 120,
    created_by: str = "admin",
) -> Dict[str, Any]:
    """Mint and persist a new API key.

    A fresh key is shaped ``mh_live_<prefix8>_<secret>``. Only the prefix and
    ``sha256(full_key)`` are stored; the full plaintext key is returned **once**
    in the ``api_key`` field and is unrecoverable thereafter.

    Args:
        name: Human label for the key.
        scopes: CSV of scopes (default ``"read"``).
        tenant_id: Optional owning tenant id.
        rate_limit_per_min: Requests/min allowed for this key.
        created_by: Who issued the key (for auditing).

    Returns:
        ``{"api_key", "prefix", "id", "scopes", "rate_limit_per_min"}`` on
        success, or ``{}`` if issuance failed (never raises).
    """
    try:
        prefix = secrets.token_hex(_PREFIX_LEN // 2)  # 8 hex chars
        secret = secrets.token_urlsafe(32)
        full_key = f"{_KEY_PREFIX}{prefix}_{secret}"
        try:
            rate = int(rate_limit_per_min)
        except (TypeError, ValueError):
            rate = 120
        scopes_csv = (scopes or "read").strip() or "read"

        with session_scope() as db:
            record = ApiKey(
                prefix=prefix,
                key_hash=_hash(full_key),
                name=(name or "Unnamed key").strip()[:255],
                tenant_id=tenant_id,
                scopes=scopes_csv,
                rate_limit_per_min=rate,
                active=True,
                created_by=(created_by or "admin")[:128],
            )
            db.add(record)
            db.flush()  # populate record.id
            key_id = record.id

        _audit(created_by, "apikey.issue", key_id,
               f"Issued API key {name!r} prefix={prefix} scopes={scopes_csv}")
        logger.info("APIKEYS | issued key id=%s prefix=%s by=%s", key_id, prefix, created_by)
        return {
            "api_key": full_key,  # PLAINTEXT — shown once, never stored.
            "prefix": prefix,
            "id": key_id,
            "scopes": _scopes_list(scopes_csv),
            "rate_limit_per_min": rate,
        }
    except Exception as exc:  # noqa: BLE001 - never raise to the caller
        logger.error("APIKEYS | issue_key failed: %s", exc)
        return {}


# --------------------------------------------------------------------------
# Verify
# --------------------------------------------------------------------------

def verify_key(presented: str) -> Optional[Dict[str, Any]]:
    """Authenticate a presented API key.

    Parses the public prefix, looks up the matching *active* key, and compares
    ``sha256(presented)`` to the stored hash in constant time. On success the
    key's ``last_used_at`` is stamped.

    Args:
        presented: The full plaintext key as sent by the client.

    Returns:
        ``{"id","prefix","name","tenant_id","scopes":[...],"rate_limit_per_min"}``
        on a valid, active key; otherwise ``None`` (never raises).
    """
    try:
        prefix = _parse_prefix(presented)
        if prefix is None:
            return None
        presented_hash = _hash(presented)

        with session_scope() as db:
            record = (
                db.query(ApiKey)
                .filter(ApiKey.prefix == prefix, ApiKey.active.is_(True))
                .first()
            )
            if record is None:
                return None
            # Constant-time comparison to avoid a hash-timing oracle.
            if not hmac.compare_digest(record.key_hash or "", presented_hash):
                return None
            record.last_used_at = _utcnow()
            db.flush()
            return {
                "id": record.id,
                "prefix": record.prefix,
                "name": record.name,
                "tenant_id": record.tenant_id,
                "scopes": _scopes_list(record.scopes),
                "rate_limit_per_min": record.rate_limit_per_min,
            }
    except Exception as exc:  # noqa: BLE001 - a bad key must never 500 the API
        logger.debug("APIKEYS | verify_key failed: %s", exc)
        return None


# --------------------------------------------------------------------------
# Rate limiting (in-memory sliding window)
# --------------------------------------------------------------------------

# Per-prefix ring of recent request timestamps. This is process-local: in a
# multi-worker deployment each worker keeps its own window, so the effective
# limit is (limit * workers). Swap this for a shared Redis sliding window
# (e.g. a sorted set per prefix) when running more than one worker.
_hits: Dict[str, Deque[float]] = defaultdict(deque)
_hits_lock = threading.Lock()
_WINDOW_SECONDS = 60.0


def check_rate_limit(prefix: str, limit_per_min: int) -> bool:
    """Return True when a request for ``prefix`` is within its per-minute limit.

    Uses an in-memory sliding 60-second window keyed by the key prefix. Calling
    this records the current request when it is allowed.

    NOTE: The window lives in this process only. For multi-worker deployments
    swap this for a shared Redis-backed sliding window so the limit is global.

    Args:
        prefix: The key's public prefix.
        limit_per_min: Maximum requests permitted within the trailing minute.

    Returns:
        ``True`` if the request is allowed (and now counted), ``False`` if the
        limit is exceeded. A non-positive limit means "unlimited" (always True).
    """
    try:
        limit = int(limit_per_min)
    except (TypeError, ValueError):
        return True
    if limit <= 0:
        return True
    if not prefix:
        return True

    now = time.monotonic()
    cutoff = now - _WINDOW_SECONDS
    with _hits_lock:
        bucket = _hits[prefix]
        # Drop timestamps that have aged out of the trailing window.
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True


def _reset_rate_limits() -> None:
    """Clear all in-memory rate-limit windows (test/maintenance helper)."""
    with _hits_lock:
        _hits.clear()


# --------------------------------------------------------------------------
# Listing / lookup / revocation
# --------------------------------------------------------------------------

def _to_dict(record: ApiKey) -> Dict[str, Any]:
    """Serialize an :class:`ApiKey` to a plain dict (never any secret)."""
    return {
        "id": record.id,
        "prefix": record.prefix,
        "name": record.name,
        "tenant_id": record.tenant_id,
        "scopes": _scopes_list(record.scopes),
        "rate_limit_per_min": record.rate_limit_per_min,
        "active": bool(record.active),
        "created_by": record.created_by,
        "last_used_at": record.last_used_at.isoformat() if record.last_used_at else None,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def list_keys() -> List[Dict[str, Any]]:
    """Return every stored API key as a dict (newest first, no secrets)."""
    try:
        with session_scope() as db:
            rows = db.query(ApiKey).order_by(ApiKey.id.desc()).all()
            return [_to_dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.error("APIKEYS | list_keys failed: %s", exc)
        return []


def get_key(key_id: int) -> Optional[Dict[str, Any]]:
    """Return a single API key by id (no secret), or ``None`` if not found."""
    try:
        with session_scope() as db:
            record = db.get(ApiKey, key_id)
            return _to_dict(record) if record is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.error("APIKEYS | get_key #%s failed: %s", key_id, exc)
        return None


def revoke_key(key_id: int, actor: str) -> Dict[str, Any]:
    """Deactivate an API key so it can no longer authenticate.

    Args:
        key_id: The key's primary key.
        actor: Who is revoking the key (for auditing).

    Returns:
        ``{"ok": True, "id", "prefix"}`` on success, or
        ``{"ok": False, "error": ...}`` if it did not exist or errored.
        Never raises.
    """
    try:
        with session_scope() as db:
            record = db.get(ApiKey, key_id)
            if record is None:
                return {"ok": False, "error": "not_found"}
            record.active = False
            db.flush()
            prefix = record.prefix

        _audit(actor, "apikey.revoke", key_id, f"Revoked API key #{key_id} prefix={prefix}")
        logger.info("APIKEYS | revoked key id=%s prefix=%s by=%s", key_id, prefix, actor)
        return {"ok": True, "id": key_id, "prefix": prefix}
    except Exception as exc:  # noqa: BLE001
        logger.error("APIKEYS | revoke_key #%s failed: %s", key_id, exc)
        return {"ok": False, "error": "revoke_failed"}
