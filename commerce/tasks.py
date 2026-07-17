"""
commerce/tasks.py
------------------
Celery task definitions for ME-HAAT Fashion AI Bot v8.0.

These tasks are the Celery-backed counterparts to the in-process job queue in
``commerce/jobs.py``. When ``config.queue_backend == "celery"`` the bridge
:func:`dispatch` routes a job kind onto Celery (via :func:`run_job`), which runs
the *exact same handler* the in-process worker would run — the handlers are the
single source of truth, so behaviour is identical across backends.

Design rules (mirroring ``commerce/jobs.py``):
* Every task lazily imports its dependencies inside the task body, so importing
  this module (and therefore ``celery_app``) never pulls in the database, the
  WhatsApp sender, etc., and never touches the network.
* Every task returns a small JSON-serializable summary dict/number.
* :func:`dispatch` is fully guarded and returns ``False`` on any problem, so the
  caller (``jobs.run_async``) can transparently fall back to the in-process
  queue.
"""

from __future__ import annotations

from typing import Any, Dict

from celery_app import celery_app

from config import config
from utils.logging import logger

# Statuses that mean a shipment no longer needs tracking refreshes.
_TERMINAL_SHIPMENT_STATUSES = {"delivered", "returned", "cancelled"}


def _ensure_handlers() -> None:
    """Register the default + broadcast job handlers in this worker process.

    A Celery worker is a separate process from the web app, so the handler
    registry in ``commerce.jobs`` starts empty here. This (idempotent) call
    populates it before :func:`run_job` looks a handler up. Best-effort.
    """
    try:
        from commerce.jobs import register_default_handlers

        register_default_handlers()
    except Exception as exc:  # noqa: BLE001
        logger.error("TASKS | register_default_handlers failed: %s", exc)
    try:
        from commerce.broadcast import register_broadcast_handler

        register_broadcast_handler()
    except Exception as exc:  # noqa: BLE001
        logger.error("TASKS | register_broadcast_handler failed: %s", exc)


@celery_app.task(
    name="mehaat.run_job",
    bind=True,
    max_retries=3,
    default_retry_delay=10,
)
def run_job(self, kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Execute the registered job handler for ``kind`` inside a Celery worker.

    Ensures the handler registry is populated in this process, resolves the
    handler for ``kind`` from ``commerce.jobs._HANDLERS`` (the same registry the
    in-process queue uses), and runs it with ``payload``. On any exception the
    task retries with backoff (up to ``max_retries``).

    Returns:
        ``{"ok": True, "kind": kind, "result": <handler result>}`` on success.
    """
    _ensure_handlers()
    try:
        from commerce.jobs import _HANDLERS

        handler = _HANDLERS.get(kind)
        if handler is None:
            # No handler is a permanent error — retrying won't help.
            logger.error("TASKS | run_job: no handler registered for kind=%s", kind)
            return {"ok": False, "kind": kind, "error": "no_handler"}

        result = handler(payload or {})
        logger.info("TASKS | run_job kind=%s done", kind)
        return {"ok": True, "kind": kind, "result": result}
    except Exception as exc:  # noqa: BLE001 - trigger a Celery retry
        logger.warning("TASKS | run_job kind=%s failed (attempt %s): %s",
                       kind, getattr(self.request, "retries", 0), exc)
        raise self.retry(exc=exc)


@celery_app.task(name="mehaat.recover_abandoned_carts")
def recover_abandoned_carts_task() -> Any:
    """Periodic task: send reminders for abandoned carts. Returns count sent."""
    try:
        from commerce.carts import recover_abandoned_carts

        return recover_abandoned_carts()
    except Exception as exc:  # noqa: BLE001
        logger.error("TASKS | recover_abandoned_carts_task failed: %s", exc)
        return 0


@celery_app.task(name="mehaat.refresh_shipment_tracking")
def refresh_shipment_tracking_task() -> Dict[str, Any]:
    """Periodic task: refresh courier tracking for all in-flight shipments.

    Iterates every shipment whose status is not terminal
    (``delivered``/``returned``/``cancelled``) and refreshes its tracking via
    :func:`shipping.service.track_shipment`. Never raises.

    Returns:
        ``{"refreshed": int, "checked": int}``.
    """
    checked = 0
    refreshed = 0
    try:
        from shipping.service import list_shipments, track_shipment

        for shipment in list_shipments(limit=500):
            status = (shipment.get("status") or "").strip().lower()
            if status in _TERMINAL_SHIPMENT_STATUSES:
                continue
            awb = shipment.get("awb")
            if not awb:
                continue
            checked += 1
            try:
                result = track_shipment(awb=awb)
                if result.get("ok"):
                    refreshed += 1
            except Exception as exc:  # noqa: BLE001 - one bad AWB shouldn't stop the batch
                logger.debug("TASKS | tracking refresh failed for awb=%s: %s", awb, exc)
    except Exception as exc:  # noqa: BLE001
        logger.error("TASKS | refresh_shipment_tracking_task failed: %s", exc)

    if refreshed:
        logger.info("TASKS | refreshed tracking for %s/%s shipment(s)", refreshed, checked)
    return {"refreshed": refreshed, "checked": checked}


# --------------------------------------------------------------------------
# Bridge from the in-process queue into Celery.
# --------------------------------------------------------------------------

def celery_enabled() -> bool:
    """Return True when the queue backend is configured as ``"celery"``."""
    try:
        return (getattr(config, "queue_backend", "inprocess") or "").strip().lower() == "celery"
    except Exception:  # noqa: BLE001
        return False


def dispatch(kind: str, payload: Dict[str, Any]) -> bool:
    """Route a job onto Celery when enabled; return whether it was dispatched.

    This is the bridge ``commerce.jobs.run_async`` calls first: if it returns
    ``True`` the job is now Celery's responsibility; if it returns ``False`` the
    caller falls back to the in-process queue. Fully guarded — any error (Celery
    misconfigured, broker unreachable at enqueue time, etc.) yields ``False``.

    Args:
        kind: The job kind (must have a registered handler in the worker).
        payload: JSON-serializable payload dict for the handler.

    Returns:
        ``True`` if the job was handed to Celery, else ``False``.
    """
    if not celery_enabled():
        return False
    try:
        run_job.delay(kind, payload or {})
        logger.info("TASKS | dispatched kind=%s to Celery", kind)
        return True
    except Exception as exc:  # noqa: BLE001 - fall back to in-process on any error
        logger.error("TASKS | Celery dispatch failed for kind=%s: %s", kind, exc)
        return False
