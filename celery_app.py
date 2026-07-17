"""
celery_app.py
-------------
Celery application factory for ME-HAAT Fashion AI Bot v8.0.

v8.0 introduces an optional Celery + Redis backend for the durable background
job queue (``commerce/jobs.py``). This module constructs the shared
``celery_app`` object and its periodic ``beat_schedule``. It is intentionally
side-effect free at import time apart from constructing the app object:

* Constructing a :class:`~celery.Celery` instance does **not** open a connection
  to the broker (connections are lazy, established on first ``.delay()`` /
  worker boot), so importing this module never raises even if Redis is down or
  unreachable. The whole construction is additionally guarded.
* When neither ``config.celery_broker_url`` nor ``config.celery_result_backend``
  is configured, the app is put into *eager* mode (``task_always_eager``): tasks
  run inline in the calling thread. This is the safe default — the app works
  with no Redis at all — and makes the tasks testable without a broker.

Actual task definitions live in ``commerce/tasks.py`` (which imports the
``celery_app`` defined here). The bridge from ``commerce.jobs.run_async`` into
Celery is ``commerce.tasks.dispatch`` — this module only owns app construction.
"""

from __future__ import annotations

from celery import Celery

from config import config
from utils.logging import logger

# Base queue/routing config shared by both eager and broker-backed modes.
_BASE_CONF = dict(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_queue="mehaat",
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
)

# Periodic tasks (Celery beat). Names must match the @task names in
# ``commerce/tasks.py``.
BEAT_SCHEDULE = {
    "recover-abandoned-carts": {
        "task": "mehaat.recover_abandoned_carts",
        "schedule": 900.0,  # every 15 minutes
    },
    "refresh-shipment-tracking": {
        "task": "mehaat.refresh_shipment_tracking",
        "schedule": 3600.0,  # hourly
    },
}


def _build_celery() -> Celery:
    """Construct the shared Celery app, never raising.

    Reads the broker/result-backend from :data:`config`. When both are empty the
    app is configured for eager (inline) execution so tasks run without a broker.
    Construction does not connect to the broker, so this is safe to call at
    import time regardless of Redis availability.
    """
    broker = (getattr(config, "celery_broker_url", "") or "").strip()
    backend = (getattr(config, "celery_result_backend", "") or "").strip()

    app = Celery("mehaat", broker=broker or None, backend=backend or None)
    app.conf.update(**_BASE_CONF)
    app.conf.beat_schedule = BEAT_SCHEDULE

    if not broker and not backend:
        # No broker configured: run everything inline so the app is fully
        # functional (and testable) without Redis.
        app.conf.task_always_eager = True
        app.conf.task_eager_propagates = True
        logger.info("CELERY | no broker configured -> eager mode (inline tasks)")
    else:
        logger.info("CELERY | configured broker=%s backend=%s",
                    broker or "<none>", backend or "<none>")

    # Ensure the task module is imported so tasks register on the app.
    app.autodiscover_tasks(["commerce"], related_name="tasks")
    return app


try:
    celery_app = _build_celery()
except Exception as exc:  # noqa: BLE001 - importing must never raise
    logger.error("CELERY | app construction failed, falling back to eager: %s", exc)
    # A minimal, always-eager fallback so imports of this module still succeed.
    celery_app = Celery("mehaat")
    celery_app.conf.update(**_BASE_CONF)
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    celery_app.conf.beat_schedule = BEAT_SCHEDULE
