"""
tests/test_v10_approvals.py
----------------------------
Deterministic, offline tests for the v10.0 human-approval workflow
(:mod:`agents.approvals`).

A *dummy* high-risk tool (``test_danger``) is registered so the tests exercise
the approval gate end-to-end without triggering any real commerce side effects:
its handler simply records the args it was invoked with. The commerce DB (which
owns the ``approval_requests`` table) is bootstrapped through
:func:`commerce.bootstrap`.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

# config reads DATABASE_URL at import time, so set it before any project import.
os.environ.setdefault("DATABASE_URL", "sqlite:///test_v10_approvals.db")

import pytest  # noqa: E402

import commerce  # noqa: E402
from agents import approvals as approvals_mod  # noqa: E402
from agents import tools as t  # noqa: E402
from agents.approvals import (  # noqa: E402
    approve,
    gate,
    get_approval,
    install_gate,
    list_approvals,
    pending_count,
    reject,
)

# Records every invocation of the dummy tool's handler.
executed: list = []


@pytest.fixture(scope="module", autouse=True)
def _bootstrap() -> None:
    commerce.bootstrap()
    # Register the dummy high-risk tools and install the gate.
    t.register(
        "test_danger",
        "danger",
        lambda a: executed.append(a) or {"done": True},
        risk="high",
        category="test",
    )
    t.register(
        "test_danger2",
        "danger too",
        lambda a: executed.append(a) or {"done": True},
        risk="high",
        category="test",
    )
    install_gate()


@pytest.fixture(autouse=True)
def _deterministic_config(monkeypatch) -> None:
    """Force approval to be required (and stable thresholds) for every test."""
    cfg = SimpleNamespace(
        approval_required=True,
        approval_broadcast_over=50,
        approval_refund_over=0.0,
    )
    monkeypatch.setattr(approvals_mod, "config", cfg)
    monkeypatch.setattr(t, "config", cfg)
    executed.clear()


# --------------------------------------------------------------------------
# Gating: a high-risk call is queued, not executed
# --------------------------------------------------------------------------

def test_high_risk_call_is_queued_not_executed():
    result = t.call_tool("test_danger", {"x": 1}, actor="agent")

    assert result["status"] == "pending_approval"
    assert isinstance(result.get("approval_id"), int)
    assert result["ok"] is False
    # The tool must NOT have executed while pending.
    assert executed == []

    # A pending ApprovalRequest row exists for it.
    row = get_approval(result["approval_id"])
    assert row is not None
    assert row["status"] == "pending"
    assert row["action"] == "test_danger"
    assert row["payload"] == {"x": 1}
    assert row["requested_by"] == "agent"


# --------------------------------------------------------------------------
# Approving executes the tool
# --------------------------------------------------------------------------

def test_approve_executes_tool():
    queued = t.call_tool("test_danger", {"x": 2}, actor="agent")
    approval_id = queued["approval_id"]
    assert executed == []

    outcome = approve(approval_id)

    assert outcome["ok"] is True
    assert outcome["status"] == "executed"
    assert outcome["result"] == {"done": True}
    # The tool ran with the stored args.
    assert executed == [{"x": 2}]

    row = get_approval(approval_id)
    assert row["status"] == "executed"
    assert row["decided_at"] is not None


# --------------------------------------------------------------------------
# Rejecting does not execute the tool
# --------------------------------------------------------------------------

def test_reject_does_not_execute_tool():
    queued = t.call_tool("test_danger2", {"y": 9}, actor="agent")
    approval_id2 = queued["approval_id"]

    outcome = reject(approval_id2, reason="not allowed")

    assert outcome["ok"] is True
    assert outcome["status"] == "rejected"
    # The tool was never executed.
    assert executed == []

    row = get_approval(approval_id2)
    assert row["status"] == "rejected"
    assert row["decided_at"] is not None


# --------------------------------------------------------------------------
# pending_count reflects remaining work
# --------------------------------------------------------------------------

def test_pending_count_reflects_remaining():
    before = pending_count()

    q1 = t.call_tool("test_danger", {"n": 1}, actor="agent")
    q2 = t.call_tool("test_danger", {"n": 2}, actor="agent")
    assert pending_count() == before + 2

    approve(q1["approval_id"])
    assert pending_count() == before + 1

    reject(q2["approval_id"])
    assert pending_count() == before


# --------------------------------------------------------------------------
# gate() can also be invoked directly
# --------------------------------------------------------------------------

def test_gate_direct_queues_when_required():
    tool = t.get_tool("test_danger")
    result = gate(tool, {"direct": True}, actor="tester")

    assert result["status"] == "pending_approval"
    row = get_approval(result["approval_id"])
    assert row["requested_by"] == "tester"
    assert row["status"] == "pending"
    assert executed == []


def test_list_approvals_filters_by_status():
    t.call_tool("test_danger", {"z": 1}, actor="agent")
    pendings = list_approvals(status="pending", limit=100)
    assert pendings
    assert all(r["status"] == "pending" for r in pendings)
