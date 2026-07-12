"""
tests/test_v51_hardening.py
----------------------------
Tests for the v5.1 Production Edition hardening:
    - WhatsApp webhook X-Hub-Signature-256 verification
    - Inbound message-id de-duplication (no double replies on Meta retries)
    - PostgreSQL DATABASE_URL scheme normalization
    - create_draft_order line-item safety

All pure logic — no real network calls.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import types


# --------------------------------------------------------------------------
# Webhook signature verification
# --------------------------------------------------------------------------

def _signed_headers(secret: str, body: bytes) -> dict:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}


def test_webhook_rejects_forged_signature(monkeypatch):
    import app as app_module
    import whatsapp.webhook as wh

    # Configure a secret so verification is enforced (config is a frozen
    # dataclass, so swap the whole reference for a shim).
    monkeypatch.setattr(wh, "config", types.SimpleNamespace(whatsapp_app_secret="test-secret"))
    client = app_module.app.test_client()

    body = json.dumps({"entry": []}).encode("utf-8")
    # Wrong signature -> 403
    r = client.post(
        "/webhook", data=body,
        headers={"X-Hub-Signature-256": "sha256=deadbeef", "Content-Type": "application/json"},
    )
    assert r.status_code == 403


def test_webhook_accepts_valid_signature(monkeypatch):
    import app as app_module
    import whatsapp.webhook as wh

    monkeypatch.setattr(wh, "config", types.SimpleNamespace(whatsapp_app_secret="test-secret"))
    client = app_module.app.test_client()

    body = json.dumps({"entry": []}).encode("utf-8")
    r = client.post("/webhook", data=body, headers=_signed_headers("test-secret", body))
    assert r.status_code == 200


def test_webhook_skips_verification_when_no_secret(monkeypatch):
    import app as app_module
    import whatsapp.webhook as wh

    # No secret configured -> backward compatible, unsigned payloads accepted.
    monkeypatch.setattr(wh, "config", types.SimpleNamespace(whatsapp_app_secret=""))
    client = app_module.app.test_client()

    body = json.dumps({"entry": []}).encode("utf-8")
    r = client.post("/webhook", data=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 200


# --------------------------------------------------------------------------
# Message de-duplication
# --------------------------------------------------------------------------

def test_message_dedup_skips_duplicates(monkeypatch):
    import whatsapp.webhook as wh

    # Isolate the dedupe window for this test.
    monkeypatch.setattr(wh, "_seen_message_ids", wh.OrderedDict())

    dispatched = []
    monkeypatch.setattr(wh, "_message_handler", lambda n, t, p: dispatched.append((n, t)))
    monkeypatch.setattr(wh, "_mark_read_best_effort", lambda mid: None)

    value = {
        "contacts": [{"profile": {"name": "Aditya"}}],
        "messages": [{"from": "919999999999", "id": "wamid.ABC", "type": "text",
                      "text": {"body": "show saree"}}],
    }
    wh._process_messages(value)
    wh._process_messages(value)  # Meta retry with same id

    assert dispatched == [("919999999999", "show saree")]  # processed exactly once


def test_dedupe_window_is_bounded(monkeypatch):
    import whatsapp.webhook as wh
    monkeypatch.setattr(wh, "_seen_message_ids", wh.OrderedDict())
    monkeypatch.setattr(wh, "_DEDUPE_CAPACITY", 10)
    for i in range(50):
        wh._already_processed(f"id-{i}")
    assert len(wh._seen_message_ids) <= 10


# --------------------------------------------------------------------------
# Postgres URL normalization
# --------------------------------------------------------------------------

def test_normalize_postgres_scheme():
    from database.db import normalize_database_url
    assert normalize_database_url("postgres://u:p@h:5432/db") == \
        "postgresql+psycopg2://u:p@h:5432/db"
    assert normalize_database_url("postgresql://u:p@h/db") == \
        "postgresql+psycopg2://u:p@h/db"
    # sqlite untouched
    assert normalize_database_url("sqlite:///mehaat.db") == "sqlite:///mehaat.db"


# --------------------------------------------------------------------------
# Draft order safety
# --------------------------------------------------------------------------

def test_create_draft_order_skips_items_without_variant(monkeypatch):
    import shopify.orders as orders

    captured = {}

    class _Client:
        def post(self, path, json_body=None):
            captured["payload"] = json_body
            return {"draft_order": {"id": 1, "invoice_url": "https://x"}}

    monkeypatch.setattr(orders, "get_client_for_shop", lambda shop=None: _Client())
    # One valid line, one missing variant_id -> only the valid one is sent.
    orders.create_draft_order([{"variant_id": 111, "quantity": 2}, {"quantity": 1}])
    items = captured["payload"]["draft_order"]["line_items"]
    assert items == [{"variant_id": 111, "quantity": 2}]
