"""
utils/cache.py
---------------
v9.0 cache + HA abstraction for ME-HAAT Fashion AI Bot.

This module provides a small, dependency-light cache facade with two
interchangeable backends:

* **Redis** — single node, Sentinel (HA failover) or Cluster (sharding),
  selected purely from :data:`config` (see :func:`backend_name`).
* **In-process memory** — a thread-safe dict with per-key expiry, used as a
  transparent fallback whenever Redis is disabled or unreachable.

Design rules:

* Nothing connects to Redis at import time. The client is built lazily on first
  use by :func:`_get_redis` and then cached for the process lifetime.
* Every public function is fully guarded and **never raises**. On any backend
  error we log once and degrade to the in-memory backend, so caching and rate
  limiting stay *functional but degraded* when Redis is down.

The same layer backs both response caching and fixed-window rate limiting
(:func:`incr_with_ttl` / :func:`rate_limit_allowed`).
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, Optional, Tuple

from config import config
from utils.logging import logger

# A module-level sentinel distinguishing "client never built" from
# "client built and deliberately None (memory fallback)".
_UNSET = object()

# Cached Redis client (or None once we've decided to use memory). Guarded by
# ``_client_lock`` so concurrent callers build it at most once.
_redis_client: Any = _UNSET
_client_lock = threading.Lock()

# Ensures the "Redis unavailable, falling back to memory" warning is logged
# only once per process rather than on every call.
_fallback_logged = False

# --- In-memory fallback store -------------------------------------------------
# Maps key -> (value, expiry_epoch_or_None). Guarded by ``_mem_lock``.
_mem_store: Dict[str, Tuple[str, Optional[float]]] = {}
_mem_lock = threading.Lock()


def backend_name() -> str:
    """Return the logical backend name selected by configuration.

    One of ``"redis"``, ``"redis-sentinel"``, ``"redis-cluster"`` or
    ``"memory"``. This reflects *intended* configuration; if Redis is
    unreachable at runtime the effective backend may still degrade to memory
    (see :func:`healthcheck`).

    Returns:
        The backend identifier string.
    """
    if not getattr(config, "cache_enabled", True):
        return "memory"
    if getattr(config, "redis_cluster", False) and getattr(config, "redis_url", ""):
        return "redis-cluster"
    if getattr(config, "redis_sentinels", ""):
        return "redis-sentinel"
    if getattr(config, "redis_url", ""):
        return "redis"
    return "memory"


def _parse_sentinels(raw: str) -> list:
    """Parse a ``"host:port,host:port"`` string into ``[(host, port), ...]``.

    Args:
        raw: Comma-separated ``host:port`` endpoints.

    Returns:
        A list of ``(host, int_port)`` tuples (invalid entries are skipped).
    """
    endpoints = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        host, _, port = chunk.partition(":")
        host = host.strip()
        if not host:
            continue
        try:
            endpoints.append((host, int(port) if port else 26379))
        except (TypeError, ValueError):
            continue
    return endpoints


def _build_redis() -> Any:
    """Build a Redis client from configuration, or ``None`` for memory.

    Selection order mirrors :func:`backend_name`. Any import or construction
    error is swallowed (logged once) so callers transparently fall back to the
    in-memory backend.

    Returns:
        A connected Redis-like client, or ``None`` to use the memory backend.
    """
    global _fallback_logged

    if not getattr(config, "cache_enabled", True):
        return None

    try:
        import redis  # noqa: F401  (installed per contract)
    except Exception:  # pragma: no cover - redis is installed per contract
        _log_fallback("redis package not importable")
        return None

    try:
        name = backend_name()
        if name == "redis-cluster":
            from redis.cluster import RedisCluster

            client = RedisCluster.from_url(config.redis_url, decode_responses=True)
        elif name == "redis-sentinel":
            from redis.sentinel import Sentinel

            sentinel = Sentinel(
                _parse_sentinels(config.redis_sentinels),
                socket_timeout=1.0,
            )
            client = sentinel.master_for(
                config.redis_sentinel_master,
                decode_responses=True,
            )
        elif name == "redis":
            client = redis.Redis.from_url(config.redis_url, decode_responses=True)
        else:
            return None

        # Actively verify connectivity so we fail fast into memory fallback.
        client.ping()
        logger.info("cache: using %s backend", name)
        return client
    except Exception as exc:  # broad: never let cache init break the app
        _log_fallback(f"Redis unavailable ({exc!r})")
        return None


def _log_fallback(reason: str) -> None:
    """Log the Redis->memory fallback reason exactly once per process."""
    global _fallback_logged
    if not _fallback_logged:
        _fallback_logged = True
        logger.warning("cache: falling back to in-memory backend: %s", reason)


def _get_redis() -> Any:
    """Return the lazily-built, cached Redis client (or ``None`` for memory).

    The client is constructed on first call and cached for the process
    lifetime. Thread-safe; never raises.

    Returns:
        The cached Redis client, or ``None`` when the memory backend is in use.
    """
    global _redis_client
    if _redis_client is not _UNSET:
        return _redis_client
    with _client_lock:
        if _redis_client is _UNSET:
            _redis_client = _build_redis()
        return _redis_client


def _reset_client() -> None:
    """Reset the cached client + fallback flag (used by tests / reconfig)."""
    global _redis_client, _fallback_logged
    with _client_lock:
        _redis_client = _UNSET
        _fallback_logged = False


# --- Memory-backend helpers ---------------------------------------------------
def _mem_get(key: str) -> Optional[str]:
    """Read a key from the in-memory store, honouring expiry."""
    now = time.time()
    with _mem_lock:
        item = _mem_store.get(key)
        if item is None:
            return None
        value, expiry = item
        if expiry is not None and expiry <= now:
            _mem_store.pop(key, None)
            return None
        return value


def _mem_set(key: str, value: str, ttl: Optional[int]) -> None:
    """Write a key to the in-memory store with an optional TTL (seconds)."""
    expiry = time.time() + ttl if ttl and ttl > 0 else None
    with _mem_lock:
        _mem_store[key] = (value, expiry)


def _mem_delete(key: str) -> None:
    """Delete a key from the in-memory store."""
    with _mem_lock:
        _mem_store.pop(key, None)


def _mem_incr(key: str, ttl: int) -> int:
    """Increment an integer counter in memory, (re)setting its TTL."""
    now = time.time()
    with _mem_lock:
        item = _mem_store.get(key)
        if item is not None:
            value, expiry = item
            if expiry is not None and expiry <= now:
                value = "0"
        else:
            value = "0"
        try:
            count = int(value) + 1
        except (TypeError, ValueError):
            count = 1
        expiry = now + ttl if ttl and ttl > 0 else None
        _mem_store[key] = (str(count), expiry)
        return count


def _default_ttl(ttl: Optional[int]) -> int:
    """Resolve a TTL, defaulting to ``config.cache_ttl_seconds``."""
    if ttl is None:
        try:
            return int(config.cache_ttl_seconds)
        except (TypeError, ValueError, AttributeError):
            return 300
    return int(ttl)


# --- Public API ---------------------------------------------------------------
def cache_get(key: str) -> Optional[str]:
    """Return the cached string for ``key``, or ``None`` if absent/expired.

    Args:
        key: The cache key.

    Returns:
        The stored string value, or ``None``. Never raises.
    """
    client = _get_redis()
    if client is not None:
        try:
            return client.get(key)
        except Exception as exc:  # degrade to memory on runtime error
            _log_fallback(f"get failed ({exc!r})")
    return _mem_get(key)


def cache_set(key: str, value: str, ttl: Optional[int] = None) -> None:
    """Store ``value`` under ``key`` with a TTL (seconds).

    Args:
        key: The cache key.
        value: The string value to store.
        ttl: Expiry in seconds; defaults to ``config.cache_ttl_seconds``.

    Returns:
        ``None``. Never raises.
    """
    ttl = _default_ttl(ttl)
    client = _get_redis()
    if client is not None:
        try:
            client.set(key, value, ex=ttl if ttl > 0 else None)
            return
        except Exception as exc:
            _log_fallback(f"set failed ({exc!r})")
    _mem_set(key, value, ttl)


def cache_delete(key: str) -> None:
    """Delete ``key`` from the cache (best-effort, both backends).

    Args:
        key: The cache key to remove.

    Returns:
        ``None``. Never raises.
    """
    client = _get_redis()
    if client is not None:
        try:
            client.delete(key)
            return
        except Exception as exc:
            _log_fallback(f"delete failed ({exc!r})")
    _mem_delete(key)


def cache_get_json(key: str) -> Optional[Any]:
    """Return the JSON-decoded value for ``key``, or ``None``.

    Args:
        key: The cache key.

    Returns:
        The decoded object, or ``None`` if missing or undecodable. Never raises.
    """
    raw = cache_get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def cache_set_json(key: str, obj: Any, ttl: Optional[int] = None) -> None:
    """JSON-encode ``obj`` and store it under ``key``.

    Args:
        key: The cache key.
        obj: A JSON-serialisable object.
        ttl: Expiry in seconds; defaults to ``config.cache_ttl_seconds``.

    Returns:
        ``None``. Never raises (unserialisable input is dropped).
    """
    try:
        raw = json.dumps(obj, default=str)
    except (TypeError, ValueError) as exc:
        logger.warning("cache: cannot JSON-encode value for %s: %r", key, exc)
        return
    cache_set(key, raw, ttl)


def incr_with_ttl(key: str, ttl: int) -> int:
    """Atomically increment a counter at ``key`` and (re)apply its TTL.

    In Redis this uses an ``INCR`` + ``EXPIRE`` pipeline; in memory it uses a
    lock. The TTL is applied on every call so the window is anchored to first
    write in Redis (``EXPIRE`` refreshes each call, giving a fixed window that
    is safe for rate limiting).

    Args:
        key: The counter key.
        ttl: Window / expiry in seconds.

    Returns:
        The new counter value (``1`` on the first increment). Never raises.
    """
    client = _get_redis()
    if client is not None:
        try:
            pipe = client.pipeline()
            pipe.incr(key)
            if ttl and ttl > 0:
                pipe.expire(key, ttl)
            results = pipe.execute()
            return int(results[0])
        except Exception as exc:
            _log_fallback(f"incr failed ({exc!r})")
    return _mem_incr(key, ttl)


def rate_limit_allowed(key: str, limit: int, window_seconds: int) -> bool:
    """Fixed-window rate limiter built on :func:`incr_with_ttl`.

    Increments the per-window counter for ``key`` and reports whether the caller
    is still under ``limit`` for the current window.

    Args:
        key: Logical identifier being limited (e.g. an API-key prefix).
        limit: Maximum permitted requests per window. ``<= 0`` means unlimited.
        window_seconds: Fixed-window length in seconds.

    Returns:
        ``True`` if the request is within the limit (and now counted), else
        ``False``. On any internal error it fails open (``True``). Never raises.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return True
    if limit <= 0:
        return True
    if not key:
        return True
    try:
        window = int(window_seconds) if window_seconds else 60
        count = incr_with_ttl(f"ratelimit:{key}:{window}", window)
        return count <= limit
    except Exception as exc:  # fail open: never block traffic on a bug
        logger.warning("cache: rate_limit_allowed error for %s: %r", key, exc)
        return True


def healthcheck() -> Dict[str, Any]:
    """Report the effective cache backend and its reachability.

    Returns:
        ``{"backend": <name>, "ok": <bool>}`` where ``backend`` is the effective
        backend (``"memory"`` if Redis was configured but unreachable) and
        ``ok`` is whether that backend responded. Never raises.
    """
    client = _get_redis()
    if client is None:
        return {"backend": "memory", "ok": True}
    try:
        client.ping()
        return {"backend": backend_name(), "ok": True}
    except Exception as exc:
        _log_fallback(f"healthcheck ping failed ({exc!r})")
        return {"backend": "memory", "ok": True}
