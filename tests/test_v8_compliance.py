"""
tests/test_v8_compliance.py
----------------------------
Tests for the v8.0 enterprise compliance layer:

    * :mod:`commerce.audit_chain` — the tamper-evident audit hash chain
      (``apply_chain`` / ``verify_chain``).
    * :mod:`commerce.compliance` — data-subject export + erasure.

No network / no mocks: the commerce schema (which now includes the hash-chain
columns and the compliance tables via ``Base.metadata``) is bootstrapped through
:func:`commerce.bootstrap`, then the services are exercised directly.
"""

from __future__ import annotations

import json
import os
import uuid

# Self-contained SQLite DB for the run (config reads DATABASE_URL at import time,
# so it must be set before any project module is imported).
os.environ.setdefault("DATABASE_URL", "sqlite:///test_v8_compliance.db")

import pytest  # noqa: E402

import commerce  # noqa: E402
from commerce import compliance  # noqa: E402
from commerce.audit_chain import apply_chain, verify_chain  # noqa: E402
from database.db import session_scope  # noqa: E402
from database.models import (  # noqa: E402
    AuditLog,
    Customer,
    Order,
    OrderItem,
)


@pytest.fixture(scope="module", autouse=True)
def _bootstrap() -> None:
    """Ensure the commerce + compliance schema exists once for the module."""
    commerce.bootstrap()


# --------------------------------------------------------------------------
# (1) Audit hash chain
# --------------------------------------------------------------------------

def test_audit_chain_verifies_then_detects_tampering() -> None:
    """Chained rows verify OK; corrupting one row breaks the chain."""
    marker = "chain-" + uuid.uuid4().hex[:8]

    with session_scope() as session:
        for i in range(3):
            row = AuditLog(
                actor=marker,
                action="test.chain",
                entity="thing",
                entity_id=str(i),
                detail=f"detail-{i}",
            )
            session.add(row)
            apply_chain(session, row)

    ok_result = verify_chain()
    assert ok_result["ok"] is True
    assert ok_result["count"] >= 3
    assert ok_result["broken_at"] is None

    # Tamper with the detail of one of our rows directly in the DB.
    with session_scope() as session:
        target = (
            session.query(AuditLog)
            .filter_by(actor=marker)
            .order_by(AuditLog.id.asc())
            .first()
        )
        assert target is not None
        target.detail = "TAMPERED"

    broken_result = verify_chain()
    assert broken_result["ok"] is False
    assert broken_result["broken_at"] is not None


# --------------------------------------------------------------------------
# (2) Data-subject export + erasure
# --------------------------------------------------------------------------

def test_export_writes_file_and_erase_redacts_pii() -> None:
    """Export produces a real JSON file containing the order; erase redacts names."""
    suffix = uuid.uuid4().hex[:10]
    wa = "9199" + suffix[:8]
    order_number = "MH-TEST-" + suffix

    with session_scope() as session:
        session.add(Customer(wa_number=wa, profile_name="Priya Sharma"))
        order = Order(
            order_number=order_number,
            wa_number=wa,
            customer_name="Priya Sharma",
            currency="INR",
            subtotal=1200,
            total_amount=1200,
            status="received",
            payment_status="pending",
            fulfillment_status="unfulfilled",
        )
        session.add(order)
        session.flush()
        session.add(
            OrderItem(
                order_id=order.id,
                product_name="Silk Saree",
                quantity=1,
                unit_price=1200,
                line_total=1200,
            )
        )

    # --- export ---
    result = compliance.data_subject_export(wa, actor="tester")
    assert result["ok"] is True
    path = result["path"]
    assert os.path.exists(path), "export must write a real file to disk"

    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    assert payload["subject_wa_number"] == wa
    assert result["summary"]["orders"] >= 1
    order_numbers = [o.get("order_number") for o in payload["orders"]]
    assert order_number in order_numbers
    assert payload["customer"]["profile_name"] == "Priya Sharma"

    # --- erase ---
    erased = compliance.erase_customer(wa, actor="tester")
    assert erased["ok"] is True

    with session_scope() as session:
        customer = session.query(Customer).filter_by(wa_number=wa).first()
        assert customer is not None
        assert customer.profile_name == "[erased]"
        order = session.query(Order).filter_by(order_number=order_number).first()
        assert order is not None
        assert order.customer_name == "[erased]"
