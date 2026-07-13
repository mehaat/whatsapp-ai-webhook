"""
tests/test_v61_jobs.py
----------------------
Unit tests for the v6.1 durable background job queue (``commerce/jobs.py``).

These run with no network and no worker threads: jobs are driven synchronously
via :func:`commerce.jobs.process_next`, so the tests are deterministic. A local
SQLite database backs the ``jobs`` table (created by ``commerce.bootstrap()``).
"""

from __future__ import annotations

import os
from types import SimpleNamespace

# Point at a dedicated throwaway SQLite DB before any app import reads config.
os.environ.setdefault("DATABASE_URL", "sqlite:///test_v61_jobs.db")

import commerce  # noqa: E402
import commerce.jobs as jobs  # noqa: E402
from database.db import session_scope  # noqa: E402
from database.models import Job  # noqa: E402

commerce.bootstrap()


def _job_status(job_id: int) -> tuple[str, int]:
    """Return ``(status, attempts)`` for a job id straight from the DB."""
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job is not None
        return job.status, int(job.attempts)


def test_process_next_runs_handler_and_marks_done():
    """A successful handler runs its side effect and the job ends ``done``."""
    ran: list[dict] = []
    jobs.register_handler("test.ok", lambda payload: ran.append(payload) or {"ok": True})

    job_id = jobs.enqueue("test.ok", {"n": 42})
    assert job_id is not None

    assert jobs.process_next() is True

    # Side effect ran with the exact payload.
    assert ran == [{"n": 42}]

    status, attempts = _job_status(job_id)
    assert status == "done"
    assert attempts == 1

    # Result was persisted as JSON.
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.result is not None
        assert "ok" in job.result


def test_failing_handler_retries_then_marks_failed():
    """A raising handler retries up to ``max_attempts`` then ends ``failed``."""
    calls: list[int] = []

    def _boom(payload):
        calls.append(1)
        raise RuntimeError("kaboom")

    jobs.register_handler("test.boom", _boom)

    # Retry immediately (no backoff delay) so process_next re-runs the job.
    original_backoff = jobs._BACKOFF_BASE_SECONDS
    jobs._BACKOFF_BASE_SECONDS = 0.0
    try:
        job_id = jobs.enqueue("test.boom", {"x": 1}, max_attempts=2)
        assert job_id is not None

        # Attempt 1: fails, re-queued (attempts < max).
        assert jobs.process_next() is True
        status, attempts = _job_status(job_id)
        assert status == "queued"
        assert attempts == 1

        # Attempt 2: fails again, now exhausted -> failed.
        assert jobs.process_next() is True
        status, attempts = _job_status(job_id)
        assert status == "failed"
        assert attempts == 2
    finally:
        jobs._BACKOFF_BASE_SECONDS = original_backoff

    assert len(calls) == 2


def test_run_async_executes_synchronously_when_jobs_disabled(monkeypatch):
    """With jobs disabled, ``run_async`` runs the handler inline immediately."""
    ran: list[dict] = []
    jobs.register_handler("test.sync", lambda payload: ran.append(payload))

    fake_config = SimpleNamespace(
        jobs_enabled=False, jobs_workers=1, jobs_max_attempts=3
    )
    monkeypatch.setattr(jobs, "config", fake_config)

    jobs.run_async("test.sync", {"hello": "world"})

    # Ran inline (no worker, no process_next needed).
    assert ran == [{"hello": "world"}]

    # And nothing was persisted as a queued job for this kind.
    with session_scope() as session:
        count = session.query(Job).filter_by(kind="test.sync").count()
    assert count == 0
