"""
tests/test_v6_order_flow.py
----------------------------
End-to-end test of the WhatsApp Commerce catalog-order path (v6.0):
POST a Meta ``type == "order"`` webhook and assert an order is persisted with a
generated internal order number, line items, and a computed total — with all
outbound network (WhatsApp/Shopify) mocked.
"""

from __future__ import annotations

import json


def test_catalog_order_webhook_creates_order(monkeypatch):
    import app as app_module
    import whatsapp.sender as sender
    import whatsapp.webhook as wh
    import commerce
    from commerce.service import order_service

    commerce.bootstrap()

    # Run order side effects synchronously (no worker-thread timing races).
    import commerce.jobs as cjobs
    cjobs.register_default_handlers()
    monkeypatch.setattr(cjobs, "config",
                        type("C", (), {"jobs_enabled": False, "jobs_workers": 1, "jobs_max_attempts": 3})())

    # No outbound network: stub WhatsApp send + read-receipt.
    sent = []
    monkeypatch.setattr(sender, "send_text_message", lambda to, text: sent.append((to, text)) or True)
    monkeypatch.setattr(sender, "mark_message_as_read", lambda mid: True)
    # No webhook signature configured -> verification skipped.
    monkeypatch.setattr(wh, "config", type("C", (), {"whatsapp_app_secret": ""})())

    wa_order_id = "wamid.ORDERTEST123"
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "contacts": [{"profile": {"name": "Test Buyer"}}],
                    "messages": [{
                        "from": "919812345670",
                        "id": wa_order_id,
                        "type": "order",
                        "timestamp": "1700000000",
                        "order": {
                            "catalog_id": "CATALOG123",
                            "text": "Please deliver fast",
                            "product_items": [
                                {"product_retailer_id": "SAREE-RED-1", "quantity": 2,
                                 "item_price": 1500.0, "currency": "INR"},
                                {"product_retailer_id": "SAREE-BLU-2", "quantity": 1,
                                 "item_price": 999.0, "currency": "INR"},
                            ],
                        },
                    }],
                }
            }]
        }]
    }

    client = app_module.app.test_client()
    resp = client.post("/webhook", data=json.dumps(payload),
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 200

    # Find the order by its WhatsApp order id via the service listing.
    orders = order_service.list_orders(limit=50)
    match = [o for o in orders if o["wa_order_id"] == wa_order_id]
    assert match, "order was not persisted from the catalog webhook"
    order = order_service.get_order(order_id=match[0]["id"], include_items=True)

    assert order["order_number"].startswith("MH-")
    assert order["customer_name"] == "Test Buyer"
    assert len(order["items"]) == 2
    assert order["total_amount"] == 2 * 1500.0 + 999.0  # 3999.0
    assert order["status"] == "received"
    # Customer received an "order received" acknowledgement.
    assert any("received" in text.lower() for _, text in sent)


def test_duplicate_order_webhook_is_ignored(monkeypatch):
    """Meta retries must not create a second order for the same message id."""
    import app as app_module
    import whatsapp.sender as sender
    import whatsapp.webhook as wh
    import commerce
    from commerce.service import order_service

    commerce.bootstrap()
    import commerce.jobs as cjobs
    cjobs.register_default_handlers()
    monkeypatch.setattr(cjobs, "config",
                        type("C", (), {"jobs_enabled": False, "jobs_workers": 1, "jobs_max_attempts": 3})())
    monkeypatch.setattr(sender, "send_text_message", lambda to, text: True)
    monkeypatch.setattr(sender, "mark_message_as_read", lambda mid: True)
    monkeypatch.setattr(wh, "config", type("C", (), {"whatsapp_app_secret": ""})())
    monkeypatch.setattr(wh, "_seen_message_ids", wh.OrderedDict())

    payload = {
        "entry": [{"changes": [{"value": {
            "contacts": [{"profile": {"name": "Dup Buyer"}}],
            "messages": [{
                "from": "919812345671", "id": "wamid.DUP1", "type": "order",
                "order": {"catalog_id": "C", "product_items": [
                    {"product_retailer_id": "X-1", "quantity": 1,
                     "item_price": 500.0, "currency": "INR"}]},
            }],
        }}]}]
    }
    client = app_module.app.test_client()
    body = json.dumps(payload)
    client.post("/webhook", data=body, headers={"Content-Type": "application/json"})
    client.post("/webhook", data=body, headers={"Content-Type": "application/json"})

    dup = [o for o in order_service.list_orders(limit=100) if o["wa_order_id"] == "wamid.DUP1"]
    assert len(dup) == 1
