"""
tests/test_v8_celery.py
-----------------------
Tests for the v8.0 Celery background-processing layer (``celery_app.py`` +
``commerce/tasks.py``).

These tests rely entirely on Celery's *eager* mode — no real Redis broker is
required. ``celery_app`` enters eager mode automatically when neither
``CELERY_BROKER_URL`` nor ``REDIS_URL`` is set, which is the case in the test
environment (conftest sets no broker). Tasks therefore run inline in the calling
thread, so ``.delay()`` / ``.apply()`` execute synchronously and we can assert on
side effects directly.
"""

from types import SimpleNamespace

import celery_app as celery_app_module
import commerce.jobs as jobs
import commerce.tasks as tasks


def test_celery_app_is_eager_without_broker():
    """With no broker configured, the app must run tasks inline (eager)."""
    assert celery_app_module.celery_app.conf.task_always_eager is True
    assert celery_app_module.celery_app.conf.task_eager_propagates is True


def test_beat_schedule_has_periodic_tasks():
    """Both periodic tasks are wired into the beat schedule."""
    schedule = celery_app_module.celery_app.conf.beat_schedule
    assert schedule["recover-abandoned-carts"]["task"] == "mehaat.recover_abandoned_carts"
    assert schedule["recover-abandoned-carts"]["schedule"] == 900.0
    assert schedule["refresh-shipment-tracking"]["task"] == "mehaat.refresh_shipment_tracking"
    assert schedule["refresh-shipment-tracking"]["schedule"] == 3600.0


def test_run_job_executes_registered_handler():
    """run_job resolves and runs the handler registered for a kind."""
    flag = {"ran": False, "payload": None}

    def _handler(payload):
        flag["ran"] = True
        flag["payload"] = payload
        return {"handled": True}

    jobs.register_handler("test.celery", _handler)

    # Eager mode: .delay executes inline and returns an EagerResult.
    result = tasks.run_job.delay("test.celery", {"x": 1})

    assert flag["ran"] is True
    assert flag["payload"] == {"x": 1}
    assert result.get() == {"ok": True, "kind": "test.celery", "result": {"handled": True}}


def test_run_job_missing_handler_returns_error():
    """An unknown kind yields a no_handler result rather than an infinite retry."""
    result = tasks.run_job.apply(args=("test.no_such_kind", {}))
    assert result.get()["ok"] is False
    assert result.get()["error"] == "no_handler"


def test_dispatch_returns_false_when_backend_not_celery(monkeypatch):
    """dispatch is a no-op (returns False) unless queue_backend == 'celery'."""
    monkeypatch.setattr(tasks, "config", SimpleNamespace(queue_backend="inprocess"))
    assert tasks.dispatch("x", {}) is False


def test_dispatch_routes_to_celery_when_enabled(monkeypatch):
    """When enabled, dispatch hands the job to Celery and returns True.

    In eager mode run_job.delay runs inline, so we also confirm the handler ran.
    """
    monkeypatch.setattr(tasks, "config", SimpleNamespace(queue_backend="celery"))

    flag = {"ran": False}

    def _handler(payload):
        flag["ran"] = True
        return None

    jobs.register_handler("test.dispatch", _handler)

    assert tasks.dispatch("test.dispatch", {"a": 2}) is True
    assert flag["ran"] is True


def test_celery_enabled_reflects_config(monkeypatch):
    """celery_enabled tracks config.queue_backend."""
    monkeypatch.setattr(tasks, "config", SimpleNamespace(queue_backend="celery"))
    assert tasks.celery_enabled() is True
    monkeypatch.setattr(tasks, "config", SimpleNamespace(queue_backend="inprocess"))
    assert tasks.celery_enabled() is False
