"""
tests/test_v10_agents.py
-------------------------
Deterministic, offline tests for the v10.0 AI orchestrator + specialist agents.

No Gemini key is configured in the test environment, so agent replies use the
deterministic fallback templates in :mod:`agents.base` — the assertions below
never touch the network. The commerce DB (which owns the ``agent_runs`` table)
is bootstrapped through :func:`commerce.bootstrap`.
"""

from __future__ import annotations

import os

# config reads DATABASE_URL at import time, so set it before any project import.
os.environ.setdefault("DATABASE_URL", "sqlite:///test_v10_agents.db")

import pytest  # noqa: E402

import commerce  # noqa: E402
from agents.base import AgentResponse  # noqa: E402
from agents.orchestrator import Orchestrator, orchestrator  # noqa: E402
from agents.specialists import SPECIALIST_NAMES, get_specialists  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _bootstrap() -> None:
    commerce.bootstrap()


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def test_classify_support():
    assert orchestrator.classify("where is my order") == "support"


def test_classify_sales():
    assert orchestrator.classify("show me red sarees") == "sales"


def test_classify_analytics():
    assert orchestrator.classify("give me the sales report") == "analytics"


def test_classify_inventory():
    assert orchestrator.classify("is this in stock") == "inventory"


def test_classify_defaults_to_sales():
    assert Orchestrator().classify("") == "sales"


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------

def test_route_sales_uses_search_products():
    resp = orchestrator.route("show me sarees", {"wa_number": "9198000000000"})
    assert isinstance(resp, AgentResponse)
    assert resp.agent == "sales"
    assert isinstance(resp.text, str) and resp.text
    assert "search_products" in resp.tools_used


def test_route_never_raises_on_garbage():
    resp = orchestrator.route("!!!", {"channel": "api"})
    assert isinstance(resp, AgentResponse)
    assert resp.text


# --------------------------------------------------------------------------
# Introspection
# --------------------------------------------------------------------------

def test_list_agents_returns_five():
    agents = orchestrator.list_agents()
    assert len(agents) == 5
    names = {a["name"] for a in agents}
    assert names == set(SPECIALIST_NAMES)
    for a in agents:
        assert a["tools"] is not None


def test_specialists_configured():
    specialists = get_specialists()
    assert set(specialists) == set(SPECIALIST_NAMES)
    assert specialists["sales"].default_tool == "search_products"
    assert specialists["support"].default_tool == "order_status"
