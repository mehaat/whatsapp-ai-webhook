"""
tests/test_v7_tickets.py
-------------------------
Tests for the v7.0 Support-Ticket workflow (``commerce/tickets.py`` and
``admin/tickets_routes.py``).

No-network, no-mock: the commerce DB is bootstrapped through
:func:`commerce.bootstrap`, and the ticket lifecycle is asserted end-to-end —
mint (``TKT-``) + ``open`` status, first message, agent reply, status change to
``resolved``, and assignment. The blueprint is verified by registering it on a
throwaway Flask app.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite:///mehaat.db")

import pytest
from flask import Flask

import commerce
from commerce import tickets as ticket_service
from admin.tickets_routes import admin_tickets_bp


def _unique_wa() -> str:
    return "9197" + f"{uuid.uuid4().int % 10**8:08d}"


@pytest.fixture(scope="module", autouse=True)
def _bootstrap() -> None:
    commerce.bootstrap()


# --------------------------------------------------------------------------
# commerce/tickets.py
# --------------------------------------------------------------------------

def test_create_ticket_mints_tkt_and_opens():
    wa = _unique_wa()
    ticket = ticket_service.create_ticket(
        "Order not delivered", wa_number=wa, priority="high",
        body="Where is my order?", author="customer",
    )
    assert "error" not in ticket
    assert ticket["ticket_number"].startswith("TKT-")
    assert ticket["status"] == "open"
    assert ticket["priority"] == "high"
    assert ticket["wa_number"] == wa
    # The first body became a thread message.
    assert len(ticket["messages"]) == 1
    assert ticket["messages"][0]["body"] == "Where is my order?"

    # Retrievable by numeric id and ticket number (with the full thread).
    by_id = ticket_service.get_ticket(ticket["id"])
    assert by_id["ticket_number"] == ticket["ticket_number"]
    assert ticket_service.get_ticket(ticket["ticket_number"])["id"] == ticket["id"]

    # latest_ticket_for resolves the customer's newest ticket.
    latest = ticket_service.latest_ticket_for(wa)
    assert latest is not None and latest["id"] == ticket["id"]


def test_create_ticket_without_body_has_no_messages():
    ticket = ticket_service.create_ticket("Question", author="customer")
    assert "error" not in ticket
    assert ticket["messages"] == []


def test_add_message_appends_to_thread():
    ticket = ticket_service.create_ticket("Sizing help", body="Is M available?")
    msg = ticket_service.add_message(ticket["id"], "Yes, M is in stock.", author="agent")
    assert "error" not in msg
    assert msg["author"] == "agent"

    refreshed = ticket_service.get_ticket(ticket["id"])
    bodies = [m["body"] for m in refreshed["messages"]]
    assert "Is M available?" in bodies
    assert "Yes, M is in stock." in bodies


def test_add_message_reopens_resolved_ticket():
    ticket = ticket_service.create_ticket("Refund query", body="Any update?")
    ticket_service.set_status(ticket["id"], "resolved", actor="agent")
    ticket_service.add_message(ticket["id"], "Actually one more thing", author="agent")
    assert ticket_service.get_ticket(ticket["id"])["status"] == "pending"


def test_set_status_resolved_and_invalid():
    ticket = ticket_service.create_ticket("Payment failed", body="Card declined")
    resolved = ticket_service.set_status(ticket["id"], "resolved", actor="admin")
    assert resolved["status"] == "resolved"

    bad = ticket_service.set_status(ticket["id"], "bogus", actor="admin")
    assert bad.get("error") == "invalid_status"

    missing = ticket_service.set_status(99999999, "open", actor="admin")
    assert missing.get("error") == "ticket_not_found"


def test_assign_and_unassign():
    ticket = ticket_service.create_ticket("Exchange request", body="Wrong colour")
    assigned = ticket_service.assign(ticket["id"], "agent_priya", actor="admin")
    assert assigned["assigned_to"] == "agent_priya"

    cleared = ticket_service.assign(ticket["id"], "", actor="admin")
    assert cleared["assigned_to"] is None


def test_list_and_count_tickets():
    ticket_service.create_ticket("Listable ticket", body="hello")
    rows = ticket_service.list_tickets(limit=50)
    assert isinstance(rows, list) and rows
    open_rows = ticket_service.list_tickets(status="open", limit=50)
    assert all(r["status"] == "open" for r in open_rows)
    assert ticket_service.count_tickets(status="open") >= 1


# --------------------------------------------------------------------------
# admin/tickets_routes.py blueprint wiring
# --------------------------------------------------------------------------

def test_blueprint_registers_expected_routes():
    app = Flask(__name__)
    app.secret_key = "test-secret-key"
    app.register_blueprint(admin_tickets_bp)

    assert admin_tickets_bp.name == "admin_tickets"
    rules = {r.rule for r in app.url_map.iter_rules()}
    for expected in {
        "/admin/tickets/",
        "/admin/tickets/<int:tid>",
        "/admin/tickets/<int:tid>/reply",
        "/admin/tickets/<int:tid>/status",
        "/admin/tickets/<int:tid>/assign",
    }:
        assert expected in rules, f"missing route {expected}"
