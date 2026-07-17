# High Availability, Caching & Sentry (v9.0)

ME-HAAT Fashion AI Bot v9.0 introduces a shared **cache / HA layer**
(`utils/cache.py`) that backs both response caching and rate limiting, plus a
deep **Sentry** integration (`utils/sentry_ext.py`). Both are designed to be
*optional and self-degrading*: if Redis or Sentry are absent or misconfigured,
the app keeps running with reduced functionality rather than failing.

---

## 1. Cache backends

The cache facade picks one backend at first use, based purely on configuration.
It **never connects at import time** and **never raises** — any Redis error is
logged once and the layer transparently falls back to an in-process memory
store.

| Backend         | `backend_name()`  | When selected                                   | Purpose                        |
| --------------- | ----------------- | ----------------------------------------------- | ------------------------------ |
| Single Redis    | `redis`           | `REDIS_URL` set                                 | Simple shared cache / limits   |
| Redis Sentinel  | `redis-sentinel`  | `REDIS_SENTINELS` set                           | **HA failover** (auto master)  |
| Redis Cluster   | `redis-cluster`   | `REDIS_CLUSTER=true` **and** `REDIS_URL` set    | **Sharding / horizontal scale**|
| In-process      | `memory`          | caching disabled, no Redis, or Redis unreachable| Fallback (single-process only) |

### Selection order

1. If `CACHE_ENABLED=false` → always `memory`.
2. Else if `REDIS_CLUSTER=true` and `REDIS_URL` is set → `redis-cluster`
   (`redis.cluster.RedisCluster.from_url`).
3. Else if `REDIS_SENTINELS` is set → `redis-sentinel`
   (`redis.sentinel.Sentinel(...).master_for(REDIS_SENTINEL_MASTER)`).
4. Else if `REDIS_URL` is set → `redis` (`redis.Redis.from_url`).
5. Else → `memory`.

### Environment variables

| Variable                 | Default      | Meaning                                                        |
| ------------------------ | ------------ | -------------------------------------------------------------- |
| `CACHE_ENABLED`          | `true`       | Master switch. `false` forces the in-memory backend.           |
| `CACHE_TTL_SECONDS`      | `300`        | Default TTL for `cache_set` / `cache_set_json`.                |
| `REDIS_URL`              | *(empty)*    | Single-node or cluster URL, e.g. `redis://host:6379/0`.        |
| `REDIS_SENTINELS`        | *(empty)*    | Comma-separated `host:port,host:port` Sentinel endpoints.      |
| `REDIS_SENTINEL_MASTER`  | `mymaster`   | Sentinel master group name to resolve to the current primary.  |
| `REDIS_CLUSTER`          | `false`      | When `true` (with `REDIS_URL`), use Redis Cluster.             |

### Single Redis

```bash
REDIS_URL=redis://cache.internal:6379/0
```

Good for a single-region deployment. If this node dies the layer degrades to
per-process memory until it recovers.

### Sentinel (HA failover)

Sentinel provides automatic failover: clients ask the Sentinels for the current
master, so a primary outage is handled without changing config.

```bash
REDIS_SENTINELS=sentinel-a:26379,sentinel-b:26379,sentinel-c:26379
REDIS_SENTINEL_MASTER=mymaster
```

The layer calls `Sentinel(...).master_for("mymaster")` and always writes to the
promoted primary.

### Cluster (sharding)

For datasets/throughput beyond a single node, Redis Cluster shards keys across
nodes:

```bash
REDIS_CLUSTER=true
REDIS_URL=redis://cluster-node-1:6379/0
```

---

## 2. What uses this layer (and the fallback contract)

* **Caching** — `cache_get` / `cache_set` / `cache_get_json` /
  `cache_set_json` / `cache_delete`.
* **Rate limiting** — `rate_limit_allowed(key, limit, window_seconds)`, a
  fixed-window limiter built on the atomic `incr_with_ttl(key, ttl)` (Redis
  `INCR` + `EXPIRE` pipeline; a lock in memory).

**Degraded-but-functional guarantee:** when Redis is unreachable, every call
falls back to the thread-safe in-memory store. Caching still works within each
process and rate limiting still enforces per-process limits — you lose the
*shared/global* view across workers, not correctness of a single process.
Errors are logged once, never raised, and rate limiting *fails open* (allows
traffic) on unexpected internal errors so a cache bug can never block users.

Check the effective backend at runtime via `healthcheck()` →
`{"backend": ..., "ok": ...}` (reports `memory` if Redis was configured but is
currently unreachable).

---

## 3. Celery + Redis HA

Celery uses Redis as broker/result backend (see `celery_app.py`). For HA, point
Celery at Sentinel using the `sentinel://` transport:

```bash
# Sentinel broker (Celery)
CELERY_BROKER_URL=sentinel://sentinel-a:26379;sentinel://sentinel-b:26379;sentinel://sentinel-c:26379
CELERY_RESULT_BACKEND=sentinel://sentinel-a:26379;sentinel://sentinel-b:26379
```

and configure the master group in Celery's transport options:

```python
app.conf.broker_transport_options = {"master_name": "mymaster"}
app.conf.result_backend_transport_options = {"master_name": "mymaster"}
```

For single-node or cluster deployments a plain `redis://` URL is used instead.
The Celery integration is also reported to Sentry (below) so task failures are
captured automatically.

---

## 4. Sentry

Deep error + performance monitoring via `utils/sentry_ext.py`. The `sentry-sdk`
import is guarded, so the app runs unchanged when it is not installed.

### Environment variables

| Variable                     | Default      | Meaning                                       |
| ---------------------------- | ------------ | --------------------------------------------- |
| `SENTRY_DSN`                 | *(empty)*    | Enables Sentry when set. Empty = disabled.    |
| `SENTRY_ENVIRONMENT`         | `production` | Environment tag (e.g. `staging`, `production`).|
| `SENTRY_TRACES_SAMPLE_RATE`  | `0.1`        | Performance-tracing sample rate (0.0–1.0).    |

### Behaviour

`init_sentry(app)`:

* Returns `False` (and logs) when `SENTRY_DSN` is empty or `sentry-sdk` is not
  importable.
* Otherwise initialises with:
  * **Integrations** — Flask and Celery, each guarded so only the importable
    ones are enabled.
  * `environment` = `SENTRY_ENVIRONMENT`.
  * `traces_sample_rate` = `SENTRY_TRACES_SAMPLE_RATE`.
  * `release` = `mehaat@<config.version>` (e.g. `mehaat@9.0`).
  * `send_default_pii=False` — no PII is attached to events.
* Returns `True` when active. Never raises.

Helpers: `capture(exc)` (reports to Sentry or logs when inactive),
`add_breadcrumb(message, category, level)`, and `sentry_active()`.
