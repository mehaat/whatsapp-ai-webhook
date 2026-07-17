"""
tests/test_v9_cache.py
-----------------------
v9.0 tests for the cache/HA layer and Sentry helpers.

No real Redis is used: we pin ``utils.cache.config`` to a SimpleNamespace with
``cache_enabled=False`` so every call resolves through the in-memory fallback
path, and reset the module's cached client between configurations.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from utils import cache, sentry_ext


def _memory_config() -> SimpleNamespace:
    """Return a config namespace that forces the in-memory backend."""
    return SimpleNamespace(
        cache_enabled=False,
        cache_ttl_seconds=300,
        redis_url="",
        redis_sentinels="",
        redis_cluster=False,
        redis_sentinel_master="mymaster",
    )


@pytest.fixture(autouse=True)
def _force_memory_backend(monkeypatch):
    """Swap cache.config for a memory-only namespace and reset cached state."""
    monkeypatch.setattr(cache, "config", _memory_config())
    cache._reset_client()
    with cache._mem_lock:
        cache._mem_store.clear()
    yield
    cache._reset_client()
    with cache._mem_lock:
        cache._mem_store.clear()


def test_backend_name_is_memory():
    assert cache.backend_name() == "memory"


def test_cache_set_get_roundtrip():
    cache.cache_set("greeting", "namaste")
    assert cache.cache_get("greeting") == "namaste"


def test_cache_get_missing_returns_none():
    assert cache.cache_get("does-not-exist") is None


def test_cache_set_get_json_roundtrip():
    payload = {"sku": "SAREE-01", "qty": 3, "tags": ["silk", "red"]}
    cache.cache_set_json("prod:1", payload)
    assert cache.cache_get_json("prod:1") == payload


def test_cache_delete():
    cache.cache_set("temp", "value")
    assert cache.cache_get("temp") == "value"
    cache.cache_delete("temp")
    assert cache.cache_get("temp") is None


def test_incr_with_ttl_increments():
    assert cache.incr_with_ttl("counter", 60) == 1
    assert cache.incr_with_ttl("counter", 60) == 2
    assert cache.incr_with_ttl("counter", 60) == 3


def test_rate_limit_allowed_under_then_over():
    key = "client-a"
    limit = 3
    window = 60
    # First `limit` calls are allowed.
    assert cache.rate_limit_allowed(key, limit, window) is True
    assert cache.rate_limit_allowed(key, limit, window) is True
    assert cache.rate_limit_allowed(key, limit, window) is True
    # The next one exceeds the limit.
    assert cache.rate_limit_allowed(key, limit, window) is False


def test_rate_limit_unlimited_when_non_positive():
    for _ in range(10):
        assert cache.rate_limit_allowed("client-b", 0, 60) is True


def test_healthcheck_reports_memory():
    result = cache.healthcheck()
    assert result == {"backend": "memory", "ok": True}


def test_sentry_init_returns_false_without_dsn(monkeypatch):
    monkeypatch.setattr(
        sentry_ext,
        "config",
        SimpleNamespace(
            sentry_dsn="",
            sentry_environment="test",
            sentry_traces_sample_rate=0.0,
            version="9.0",
        ),
    )
    assert sentry_ext.init_sentry() is False
    assert sentry_ext.sentry_active() is False
