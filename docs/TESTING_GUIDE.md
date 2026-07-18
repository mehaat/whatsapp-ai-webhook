# Testing Guide — SQLite & PostgreSQL

The migration preserves 100% of existing behaviour and adds Postgres support.
This guide shows how to test **both backends** locally, since the whole point is
that the same code paths run on each.

---

## 0. Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

For Postgres tests you need a reachable Postgres (local Docker, or a Neon
branch). Example with Docker:

```bash
docker run --rm -d --name mehaat-pg -e POSTGRES_PASSWORD=pass -p 5432:5432 postgres:16
export PG_URL='postgresql://postgres:pass@localhost:5432/postgres'
```

---

## 1. Fast smoke test (both backends)

Run the whole app import + `/health` against each backend.

**SQLite:**

```bash
DATABASE_URL='sqlite:///./test.db' python - <<'PY'
import app as A
j = A.app.test_client().get('/health').get_json()
assert j['status'] == 'ok'
assert j['database']['reachable'] is True
print('SQLite health OK:', j['database']['backend'], j['database']['integrity'])
PY
```

**Postgres:**

```bash
DATABASE_URL="$PG_URL" bash -c '
alembic upgrade head
python - <<PY
import app as A
j = A.app.test_client().get("/health").get_json()
assert j["status"] == "ok" and j["database"]["backend"] == "postgresql"
print("Postgres health OK:", j["database"]["integrity"])
PY'
```

---

## 2. OAuth store (persistence + single-use state)

Verifies the exact production scenario (tokens/state survive restarts and are
shared across workers). Run against **both** `DATABASE_URL`s:

```bash
python - <<'PY'
from database.db import init_db, dispose_engine, backend_name
from shopify.auth import token_store, _state_manager, validate_and_recover_tokens
init_db()
token_store.save("a.myshopify.com", "shpat_AAAA_00001")
token_store.save("b.myshopify.com", "shpat_BBBB_00002")
assert token_store.get("a.myshopify.com") == "shpat_AAAA_00001"
assert token_store.list_shops() == ["a.myshopify.com", "b.myshopify.com"]
assert token_store.get_default_shop() == "a.myshopify.com"
s = _state_manager.issue("a.myshopify.com")
assert _state_manager.consume(s, "a.myshopify.com") is True     # first use
assert _state_manager.consume(s, "a.myshopify.com") is False    # replay blocked
rep = validate_and_recover_tokens()
assert rep["ok"] and rep["shop_count"] == 2
print(backend_name(), "OAuth store OK")
dispose_engine()
PY
```

---

## 3. Admin dashboard (writes + analytics reads)

Confirms the dashboard's raw SQL runs unchanged on both backends via the shim:

```bash
ADMIN_USERNAME=admin ADMIN_PASSWORD=secret python - <<'PY'
from database.db import init_db, dispose_engine, backend_name
from admin.db import init_db as admin_init, get_conn
from admin import tracker, analytics
init_db(); admin_init()
tracker.record_inbound("15550000001", "hi", profile_name="Sam", language="en")
tracker.record_outbound("15550000001", "hello", intent="greeting", latency_ms=40)
tracker.record_ai("15550000001", "hi", "hello", model="gemini-2.5-flash")
tracker.record_products_sent("15550000001", "sarees",
    [{"id": "s1", "title": "Silk Saree", "price": "2999", "currency": "INR"}])
st = analytics.dashboard_stats()
assert st["todays_messages"] >= 2 and st["products_sent"] >= 1
# both name and index row access must work on either backend
with get_conn() as c:
    r = c.execute("SELECT COUNT(*) AS n FROM messages WHERE wa_number = ?",
                  ("15550000001",)).fetchone()
    assert int(r["n"]) == 2 and r[0] == 2
print(backend_name(), "admin datastore OK:", st)
dispose_engine()
PY
```

---

## 4. Schema drift check

The generated migration must exactly match the models:

```bash
DATABASE_URL="$PG_URL" alembic upgrade head
DATABASE_URL="$PG_URL" alembic check      # -> "No new upgrade operations detected."
```

---

## 5. Data-copy tool (idempotency)

```bash
# populate a source sqlite (or use an existing mehaat.db), then:
DATABASE_URL="$PG_URL" python scripts/migrate_sqlite_to_postgres.py --source ./test.db
# run again — expect inserted=0, everything skipped:
DATABASE_URL="$PG_URL" python scripts/migrate_sqlite_to_postgres.py --source ./test.db
```

---

## 6. Existing project test suite

The repo ships tests under `tests/`. Run them with the default SQLite backend:

```bash
pip install -r requirements-dev.txt
pytest -q
```

> Note: a few legacy tests in `tests/test_v10_1_stable.py` construct a legacy
> **SQLite** `mehaat_admin.db` to exercise the one-time admin-merge path; those
> are inherently SQLite-only and unaffected by the Postgres work.

---

## Checklist before shipping

- [ ] `/health` returns `status: ok` on SQLite **and** Postgres
- [ ] OAuth save/get/list/default/state single-use pass on both
- [ ] Admin tracker writes + analytics reads pass on both
- [ ] `alembic upgrade head` then `alembic check` is clean on Postgres
- [ ] Data-copy tool second run reports `inserted=0`
- [ ] App boots with `DATABASE_URL` unset (SQLite fallback) — no regression
