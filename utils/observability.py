"""
utils/observability.py
------------------------
v7.0 observability: optional Sentry error monitoring and a Prometheus-format
``/metrics`` endpoint. Both are dependency-light and fully guarded:

- Sentry initializes only when ``SENTRY_DSN`` is set *and* ``sentry-sdk`` is
  installed; otherwise it is a no-op.
- Metrics are emitted in the Prometheus text exposition format, hand-rolled so
  no ``prometheus-client`` dependency is required. A tiny in-process counter
  registry tracks request counts and a few commerce gauges.
"""

from __future__ import annotations

import threading
import time
from typing import Dict

from config import config
from utils.logging import logger

_start_time = time.time()

# --- simple thread-safe counter registry -------------------------------------
_lock = threading.Lock()
_counters: Dict[str, float] = {}


def incr(metric: str, amount: float = 1.0) -> None:
    """Increment a named counter (thread-safe, never raises)."""
    try:
        with _lock:
            _counters[metric] = _counters.get(metric, 0.0) + amount
    except Exception:  # noqa: BLE001
        pass


def init_sentry() -> bool:
    """Initialize Sentry if configured. Returns True when active."""
    if not config.sentry_dsn:
        return False
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=config.sentry_dsn,
            traces_sample_rate=float(0.1),
            release=f"mehaat@{config.version}",
        )
        logger.info("OBSERVABILITY | Sentry initialized")
        return True
    except Exception as exc:  # noqa: BLE001 - monitoring must never break startup
        logger.warning("OBSERVABILITY | Sentry unavailable: %s", exc)
        return False


def _db_gauges() -> Dict[str, float]:
    """Best-effort commerce gauges pulled from the database (never raises)."""
    gauges: Dict[str, float] = {}
    try:
        from database.db import session_scope
        from database.models import Order, Job

        with session_scope() as session:
            gauges["mehaat_orders_total"] = float(session.query(Order).count())
            gauges["mehaat_jobs_queued"] = float(
                session.query(Job).filter(Job.status == "queued").count()
            )
            gauges["mehaat_jobs_failed"] = float(
                session.query(Job).filter(Job.status == "failed").count()
            )
    except Exception:  # noqa: BLE001
        pass
    return gauges


def render_metrics() -> str:
    """Return the Prometheus text exposition for the current process."""
    lines = []
    uptime = time.time() - _start_time
    lines.append("# HELP mehaat_uptime_seconds Process uptime in seconds.")
    lines.append("# TYPE mehaat_uptime_seconds gauge")
    lines.append(f"mehaat_uptime_seconds {uptime:.1f}")

    with _lock:
        counters = dict(_counters)
    for name, value in sorted(counters.items()):
        safe = name.replace("-", "_").replace(".", "_")
        lines.append(f"# TYPE mehaat_{safe} counter")
        lines.append(f"mehaat_{safe} {value}")

    for name, value in sorted(_db_gauges().items()):
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value}")

    return "\n".join(lines) + "\n"
