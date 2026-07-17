"""
tests/test_v10_1_stable.py
---------------------------
Regression tests for the v10.1 Stable Edition fixes:
    - unified canonical database path (one mehaat.db)
    - Shopify OAuth token persistence + validation (the shop_count=0 bug)
    - admin-DB unification migration (legacy mehaat_admin.db -> dash_* tables)
    - event pipeline populating the dashboard (products + AI history)
"""

from __future__ import annotations

import os
import sqlite3


def test_canonical_path_is_absolute_and_deterministic():
    from utils.dbpath import canonical_sqlite_path

    p1 = canonical_sqlite_path()
    p2 = canonical_sqlite_path()
    assert os.path.isabs(p1)
    assert p1 == p2  # cached / deterministic


def test_sqlite_url_parsing():
    from utils.dbpath import sqlite_path_from_url

    assert sqlite_path_from_url("sqlite:///mehaat.db") == "mehaat.db"
    assert sqlite_path_from_url("sqlite:////var/data/mehaat.db") == "/var/data/mehaat.db"
    assert sqlite_path_from_url("postgresql://x/y") is None


def test_oauth_token_persists_and_validates(monkeypatch):
    """A saved token must be retrievable and pass startup validation."""
    import commerce
    commerce.bootstrap()
    from shopify.auth import token_store, validate_and_recover_tokens

    shop = "unit-test-persist.myshopify.com"
    token_store.save(shop, "shpat_persist_1234567890")
    # Read back through a fresh lookup (not a cached object).
    assert token_store.get(shop) == "shpat_persist_1234567890"
    assert shop in token_store.list_shops()

    report = validate_and_recover_tokens()
    assert report["integrity"] == "ok"
    assert report["shop_count"] >= 1
    assert report["valid"] >= 1
    assert report["corrupted"] == []


def test_oauth_no_duplicate_shops():
    import commerce
    commerce.bootstrap()
    from shopify.auth import token_store

    shop = "unit-test-dedupe.myshopify.com"
    token_store.save(shop, "tok_aaaaaaa1")
    token_store.save(shop, "tok_bbbbbbb2")  # re-save must update, not duplicate
    shops = [s for s in token_store.list_shops() if s == shop]
    assert len(shops) == 1
    assert token_store.get(shop) == "tok_bbbbbbb2"


def test_admin_migration_merges_legacy_db(tmp_path, monkeypatch):
    """A legacy mehaat_admin.db is merged into the unified DB as dash_* tables."""
    import commerce
    commerce.bootstrap()
    from admin.db import init_db
    from utils.dbpath import canonical_sqlite_path
    from database.migrate_v10_1 import merge_admin_db

    init_db()
    legacy = tmp_path / "mehaat_admin.db"
    lc = sqlite3.connect(str(legacy))
    # Mirror the real (pre-v10.1) admin schema — same columns, old table names.
    lc.executescript(
        "CREATE TABLE customers (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "wa_number TEXT UNIQUE NOT NULL, profile_name TEXT, language TEXT, "
        "email TEXT, tags TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL);"
        "CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, order_name TEXT, "
        "wa_number TEXT, looked_up_at TEXT NOT NULL);"
    )
    lc.execute("INSERT INTO customers (wa_number, profile_name, first_seen_at, last_seen_at) "
               "VALUES ('919000migrate','Legacy','2026-01-01','2026-01-02')")
    lc.execute("INSERT INTO orders (order_name, wa_number, looked_up_at) "
               "VALUES ('#9001','919000migrate','2026-01-01')")
    lc.commit()
    lc.close()

    monkeypatch.setenv("ADMIN_DB_PATH", str(legacy))
    report = merge_admin_db()
    assert report["migrated"] is True

    conn = sqlite3.connect(canonical_sqlite_path())
    try:
        cust = conn.execute(
            "SELECT COUNT(*) FROM dash_customers WHERE wa_number='919000migrate'"
        ).fetchone()[0]
        # Commerce 'orders' table must remain the SQLAlchemy schema (order_number).
        cols = [r[1] for r in conn.execute("PRAGMA table_info(orders)")]
    finally:
        conn.close()
    assert cust >= 1
    assert "order_number" in cols  # not shadowed by the admin schema


def test_event_pipeline_populates_dashboard():
    """record_inbound/products_sent/ai/outbound must reach the dashboard reads."""
    import commerce
    commerce.bootstrap()
    from admin.db import init_db
    from admin import tracker, analytics

    init_db()
    wa = "919812349999"
    tracker.record_inbound(wa, "green cotton saree", "Tester", "english")
    tracker.record_products_sent(wa, "green cotton saree", [
        {"product_id": 42, "title": "Green Cotton Saree", "price": "999",
         "currency": "INR", "url": "https://x/42"}])
    tracker.record_ai(wa, "green cotton saree", "Great choice!",
                      model="gemini-2.5-flash", prompt_context="ctx",
                      latency_ms=90, fallback_used=False)
    tracker.record_outbound(wa, "Great choice!", latency_ms=90)

    stats = analytics.dashboard_stats()
    assert stats.get("products_sent", 0) >= 1
    assert stats.get("ai_replies", 0) >= 1
    assert len(analytics.ai_history(limit=5)) >= 1
    titles = [p.get("title") for p in analytics.popular_products(5)]
    assert any("Green Cotton" in (t or "") for t in titles)
