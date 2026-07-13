"""
commerce/jobs.py
----------------
Durable, in-process background job queue for ME-HAAT Fashion AI Bot v6.1.

Order side effects (customer notifications, Shopify draft-order creation, PDF
invoice generation, inventory reservation) are slow and best-effort. Running
them inline would keep the WhatsApp webhook request open and couple its success
to third-party availability. This module lets callers *enqueue* that work so the
webhook returns immediately, while the effect runs on a small pool of daemon
worker threads.

Design goals
============
* **Durable + auditable** — every job is a row in the ``jobs`` table
  (status/attempts/result/error), so an interrupted deploy can resume and an
  admin can inspect what ran.
* **Crash recovery** — :func:`recover_pending` re-pushes any ``queued`` /
  ``running`` jobs on startup.
* **Retry with backoff** — a raising handler is retried up to ``max_attempts``
  with exponential backoff before being marked ``failed``.
* **Deployment-safe** — the target deployment is ``gunicorn --workers 1`` so a
  single-process in-memory :class:`queue.Queue` plus daemon threads is
  sufficient; no external broker (Redis/Celery) is required.
* **Never crashes the app** — every public entrypoint is fully guarded.

A handler is any callable taking a single ``payload: dict`` and returning a
JSON-serializable result (or ``None``). Raising an exception triggers a retry.

Because :class:`queue.Queue` only lives inside one process, it holds *job ids*,
not the work itself — the database row is the source of truth. This keeps the
in-memory queue tiny and lets recovery rebuild it from the table.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from config import config
from utils.logging import logger

# A handler takes a decoded payload dict and returns a JSON-serializable result.
Handler = Callable[[Dict[str, Any]], Any]

# --------------------------------------------------------------------------
# Module-level state (guarded by locks where mutated from multiple threads).
# --------------------------------------------------------------------------

#: Registered handlers, keyed by job ``kind``.
_HANDLERS: Dict[str, Handler] = {}
_registry_lock = threading.Lock()

#: In-memory FIFO of *job ids* awaiting execution (source of truth is the DB).
_queue: "queue.Queue[int]" = queue.Queue()

#: Worker threads + coordination.
_workers: List[threading.Thread] = []
_workers_lock = threading.Lock()
_stop_event = threading.Event()
_started = False

#: Base seconds for exponential retry backoff (run_at = now + base * 2**(n-1)).
#: Exposed as a module global so tests can set it to 0 for immediate retries.
_BACKOFF_BASE_SECONDS: float = 2.0

#: How long a worker naps after a no-op poll (future/duplicate job) to avoid a
#: busy spin when the only queued ids are not yet eligible.
_IDLE_SLEEP_SECONDS: float = 0.1


# --------------------------------------------------------------------------
# Time helpers (SQLite stores naive datetimes even for tz-aware columns, so
# comparisons must tolerate a mix of aware/naive values).
# --------------------------------------------------------------------------

def _now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def _is_future(dt: Optional[datetime]) -> bool:
    """Return True if ``dt`` is strictly in the future (naive-tolerant)."""
    if dt is None:
        return False
    now = _now()
    if dt.tzinfo is None:
        now = now.replace(tzinfo=None)
    return dt > now


# --------------------------------------------------------------------------
# Handler registry
# --------------------------------------------------------------------------

def register_handler(kind: str, fn: Handler) -> None:
    """Register (or replace) the handler for a job ``kind``.

    Args:
        kind: The job kind this handler services (e.g. ``"notify"``).
        fn: Callable taking a ``payload`` dict and returning a
            JSON-serializable result or ``None``. Raising triggers a retry.
    """
    with _registry_lock:
        _HANDLERS[kind] = fn
    logger.debug("JOBS | handler registered for kind=%s", kind)


def _get_handler(kind: str) -> Optional[Handler]:
    """Return the handler for ``kind`` if registered, else ``None``."""
    with _registry_lock:
        return _HANDLERS.get(kind)


# --------------------------------------------------------------------------
# Enqueue / synchronous dispatch
# --------------------------------------------------------------------------

def enqueue(
    kind: str,
    payload: Dict[str, Any],
    *,
    max_attempts: Optional[int] = None,
    delay_seconds: int = 0,
) -> Optional[int]:
    """Persist a job and push its id onto the in-memory queue.

    The job is inserted with status ``"queued"`` and ``run_at = now + delay``.
    A handler need not be registered yet; the worker resolves it at run time.

    Args:
        kind: Job kind (must eventually have a registered handler to run).
        payload: JSON-serializable dict passed to the handler.
        max_attempts: Retry ceiling; defaults to ``config.jobs_max_attempts``.
        delay_seconds: Delay before the job becomes eligible to run.

    Returns:
        The new job id, or ``None`` if persistence failed (never raises).
    """
    try:
        from database.db import session_scope
        from database.models import Job

        attempts_cap = int(max_attempts) if max_attempts is not None else int(
            getattr(config, "jobs_max_attempts", 3) or 3
        )
        run_at = _now() + timedelta(seconds=max(0, int(delay_seconds)))
        payload_json = json.dumps(payload or {}, default=str)

        with session_scope() as session:
            job = Job(
                kind=kind,
                payload=payload_json,
                status="queued",
                attempts=0,
                max_attempts=attempts_cap,
                run_at=run_at,
            )
            session.add(job)
            session.flush()  # populate job.id before the session closes
            job_id = int(job.id)

        _queue.put(job_id)
        logger.info("JOBS | enqueued kind=%s id=%s delay=%ss", kind, job_id, delay_seconds)
        return job_id
    except Exception as exc:  # noqa: BLE001 - enqueue must never raise
        logger.error("JOBS | enqueue failed for kind=%s: %s", kind, exc)
        return None


def run_async(kind: str, payload: Dict[str, Any]) -> None:
    """Run a job asynchronously, or synchronously when jobs are disabled.

    When ``config.jobs_enabled`` is true the work is enqueued for the worker
    pool. When disabled, the registered handler runs *inline right now*, so the
    observable behaviour is identical with workers switched off. Fully guarded.

    Args:
        kind: Job kind.
        payload: JSON-serializable payload dict for the handler.
    """
    try:
        if getattr(config, "jobs_enabled", True):
            enqueue(kind, payload)
            return

        handler = _get_handler(kind)
        if handler is None:
            logger.warning("JOBS | run_async(sync): no handler for kind=%s", kind)
            return
        try:
            handler(payload or {})
        except Exception as exc:  # noqa: BLE001 - inline effects are best-effort
            logger.error("JOBS | sync handler failed for kind=%s: %s", kind, exc)
    except Exception as exc:  # noqa: BLE001 - never propagate to the caller
        logger.error("JOBS | run_async failed for kind=%s: %s", kind, exc)


# --------------------------------------------------------------------------
# Core execution routine (shared by worker threads and process_next).
# --------------------------------------------------------------------------

def _mark(job_id: int, **fields: Any) -> None:
    """Apply column updates to a job row in its own transaction."""
    from database.db import session_scope
    from database.models import Job

    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            return
        for key, value in fields.items():
            setattr(job, key, value)


def _execute(job_id: int) -> bool:
    """Execute a single job by id, handling state transitions and retries.

    Loads the job, skips it if it is no longer ``queued`` or its ``run_at`` is
    still in the future (re-queuing the latter), otherwise marks it ``running``
    (incrementing ``attempts``), invokes the handler, and records the outcome:

    * success -> ``done`` with the JSON-encoded result;
    * failure with attempts remaining -> back to ``queued`` with exponential
      backoff and the recorded error (re-pushed onto the queue);
    * failure with no attempts remaining -> ``failed`` with the error.

    Each state transition uses its own :func:`session_scope`.

    Returns:
        True if the handler was actually attempted; False if the job was
        skipped (missing / not queued / not yet eligible).
    """
    try:
        from database.db import session_scope
        from database.models import Job

        # --- Load + eligibility check -------------------------------------
        with session_scope() as session:
            job = session.get(Job, job_id)
            if job is None:
                return False
            if job.status != "queued":
                return False
            if _is_future(job.run_at):
                # Not yet eligible: put it back for a later poll.
                _queue.put(job_id)
                return False
            kind = job.kind
            payload_raw = job.payload
            attempts = int(job.attempts or 0) + 1
            max_attempts = int(job.max_attempts or 1)
            # Claim the job for this run.
            job.status = "running"
            job.attempts = attempts

        # --- Decode payload -----------------------------------------------
        try:
            payload = json.loads(payload_raw) if payload_raw else {}
            if not isinstance(payload, dict):
                payload = {"_raw": payload}
        except Exception:  # noqa: BLE001 - corrupt payloads shouldn't wedge us
            payload = {}

        # --- Run the handler ----------------------------------------------
        handler = _get_handler(kind)
        try:
            if handler is None:
                raise LookupError(f"no handler registered for kind={kind!r}")
            result = handler(payload)
        except Exception as exc:  # noqa: BLE001 - expected retry trigger
            error_text = f"{type(exc).__name__}: {exc}"
            if attempts >= max_attempts:
                _mark(job_id, status="failed", error=error_text)
                logger.error(
                    "JOBS | job id=%s kind=%s failed permanently after %s attempts: %s",
                    job_id, kind, attempts, error_text,
                )
            else:
                backoff = _BACKOFF_BASE_SECONDS * (2 ** (attempts - 1))
                run_at = _now() + timedelta(seconds=backoff)
                _mark(job_id, status="queued", error=error_text, run_at=run_at)
                _queue.put(job_id)
                logger.warning(
                    "JOBS | job id=%s kind=%s attempt %s/%s failed, retrying in %ss: %s",
                    job_id, kind, attempts, max_attempts, backoff, error_text,
                )
            return True

        # --- Success ------------------------------------------------------
        try:
            result_json = json.dumps(result, default=str) if result is not None else None
        except Exception:  # noqa: BLE001 - store a best-effort string
            result_json = json.dumps(str(result))
        _mark(job_id, status="done", result=result_json, error=None)
        logger.info("JOBS | job id=%s kind=%s done", job_id, kind)
        return True
    except Exception as exc:  # noqa: BLE001 - the worker must survive anything
        logger.error("JOBS | _execute crashed for id=%s: %s", job_id, exc)
        return False


def process_next(timeout: float = 0.0) -> bool:
    """Synchronously process at most one job from the in-memory queue.

    Intended for tests and a no-thread execution mode. Reuses the same routine
    the worker threads run.

    Args:
        timeout: Seconds to wait for a job id. ``0`` polls without blocking.

    Returns:
        True if a job was processed; False if the queue was empty or the popped
        job was skipped.
    """
    try:
        if timeout and timeout > 0:
            job_id = _queue.get(timeout=timeout)
        else:
            job_id = _queue.get_nowait()
    except queue.Empty:
        return False
    try:
        return _execute(job_id)
    finally:
        _queue.task_done()


# --------------------------------------------------------------------------
# Worker pool
# --------------------------------------------------------------------------

def _worker_loop() -> None:
    """Block on the queue and execute jobs until :func:`stop_workers`."""
    while not _stop_event.is_set():
        try:
            job_id = _queue.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            processed = _execute(job_id)
            if not processed:
                # Job was skipped/re-queued; nap briefly to avoid a busy spin.
                time.sleep(_IDLE_SLEEP_SECONDS)
        except Exception as exc:  # noqa: BLE001 - keep the worker alive
            logger.error("JOBS | worker error on id=%s: %s", job_id, exc)
        finally:
            _queue.task_done()


def start_workers(n: Optional[int] = None) -> None:
    """Start the daemon worker pool (idempotent).

    Recovers any pending jobs, then spawns ``n`` daemon threads that block on
    the queue. Safe to call more than once; subsequent calls are no-ops while
    the pool is running.

    Args:
        n: Number of workers; defaults to ``config.jobs_workers`` (min 1).
    """
    global _started
    with _workers_lock:
        if _started and any(t.is_alive() for t in _workers):
            logger.debug("JOBS | start_workers: already running")
            return
        _stop_event.clear()
        _workers.clear()

        # Rebuild the queue from durable state before accepting new work.
        recovered = recover_pending()
        if recovered:
            logger.info("JOBS | recovered %s pending job(s) on startup", recovered)

        count = int(n if n is not None else getattr(config, "jobs_workers", 2) or 2)
        count = max(1, count)
        for i in range(count):
            thread = threading.Thread(
                target=_worker_loop, name=f"jobs-worker-{i}", daemon=True
            )
            thread.start()
            _workers.append(thread)
        _started = True
        logger.info("JOBS | started %s worker thread(s)", count)


def stop_workers() -> None:
    """Signal the worker pool to stop and wait briefly for threads to exit."""
    global _started
    with _workers_lock:
        _stop_event.set()
        for thread in _workers:
            try:
                thread.join(timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
        _workers.clear()
        _started = False
    logger.info("JOBS | workers stopped")


# --------------------------------------------------------------------------
# Recovery + stats
# --------------------------------------------------------------------------

def recover_pending() -> int:
    """Re-push ids of interrupted jobs onto the in-memory queue.

    Any job left ``queued`` or ``running`` (e.g. by a crash or a redeploy) is
    re-queued for execution. ``running`` jobs are reset to ``queued`` first so
    the eligibility guard in :func:`_execute` lets them run again.

    Returns:
        The number of jobs re-pushed (0 on error; never raises).
    """
    try:
        from database.db import session_scope
        from database.models import Job

        ids: List[int] = []
        with session_scope() as session:
            jobs = session.query(Job).filter(
                Job.status.in_(("queued", "running"))
            ).all()
            for job in jobs:
                if job.status == "running":
                    job.status = "queued"
                ids.append(int(job.id))

        for job_id in ids:
            _queue.put(job_id)
        return len(ids)
    except Exception as exc:  # noqa: BLE001 - recovery is best-effort
        logger.error("JOBS | recover_pending failed: %s", exc)
        return 0


def queue_stats() -> Dict[str, int]:
    """Return job counts grouped by status (for an admin widget).

    Returns:
        A dict with counts for each of ``queued``/``running``/``done``/
        ``failed`` plus ``total``. Returns zeros on error (never raises).
    """
    stats: Dict[str, int] = {
        "queued": 0, "running": 0, "done": 0, "failed": 0, "total": 0,
    }
    try:
        from sqlalchemy import func

        from database.db import session_scope
        from database.models import Job

        with session_scope() as session:
            rows = (
                session.query(Job.status, func.count(Job.id))
                .group_by(Job.status)
                .all()
            )
        for status, count in rows:
            stats[status] = int(count)
            stats["total"] += int(count)
    except Exception as exc:  # noqa: BLE001 - stats are best-effort
        logger.error("JOBS | queue_stats failed: %s", exc)
    return stats


# --------------------------------------------------------------------------
# Default handlers for the v6.1 order side effects.
# --------------------------------------------------------------------------

def _load_order(order_id: Any, *, include_tracking: bool = False) -> Optional[Dict[str, Any]]:
    """Load an order dict via the order service, tolerating failures."""
    if order_id is None:
        return None
    try:
        from commerce.service import order_service

        return order_service.get_order(
            order_id=order_id,
            include_items=True,
            include_tracking=include_tracking,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("JOBS | could not load order id=%s: %s", order_id, exc)
        return None


def _handle_notify(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Send an outbound status notification for an order."""
    order = _load_order(payload.get("order_id"), include_tracking=True)
    if order is None:
        logger.warning("JOBS | notify: order %s not found", payload.get("order_id"))
        return None
    from commerce.notifications import send_status_notification

    send_status_notification(order, payload.get("status"), payload.get("payment_link"))
    return {"notified": True, "order_id": payload.get("order_id")}


def _handle_draft_order(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Create a Shopify draft order for an order."""
    order = _load_order(payload.get("order_id"))
    if order is None:
        logger.warning("JOBS | draft_order: order %s not found", payload.get("order_id"))
        return None
    from commerce.draft_orders import create_draft_for_order

    result = create_draft_for_order(order)
    return {"draft": result}


def _handle_invoice(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Generate a PDF invoice for an order."""
    order = _load_order(payload.get("order_id"))
    if order is None:
        logger.warning("JOBS | invoice: order %s not found", payload.get("order_id"))
        return None
    from commerce.invoices import generate_invoice

    result = generate_invoice(order)
    return {"invoice": result}


def _handle_reserve(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Reserve inventory for an order (reservations module may be absent)."""
    order = _load_order(payload.get("order_id"))
    if order is None:
        logger.warning("JOBS | reserve: order %s not found", payload.get("order_id"))
        return None
    try:
        from commerce.reservations import reserve_for_order
    except ImportError as exc:
        logger.warning("JOBS | reserve: reservations module unavailable: %s", exc)
        return None

    result = reserve_for_order(order)
    return {"reserved": result}


def register_default_handlers() -> None:
    """Register the built-in v6.1 order side-effect handlers.

    Registers ``notify``, ``draft_order``, ``invoice`` and ``reserve``. Each
    handler lazily imports its dependencies and tolerates a missing order.
    """
    register_handler("notify", _handle_notify)
    register_handler("draft_order", _handle_draft_order)
    register_handler("invoice", _handle_invoice)
    register_handler("reserve", _handle_reserve)
    logger.info("JOBS | default handlers registered")
