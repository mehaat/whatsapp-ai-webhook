# Migration Guide — SQLite → PostgreSQL (Neon)

This project uses **one** SQLAlchemy engine and **one** declarative `Base` for
everything: the commerce ORM models, the Shopify OAuth token/state store, and
the Admin Dashboard datastore. The backend is selected purely by `DATABASE_URL`.
Migrating to Postgres therefore means (a) creating the schema on Postgres and
(b) optionally copying your existing rows across.

---

## What changed (architecture)

Before, three subsystems opened their own `sqlite3` connections:

* `shopify/auth.py` — OAuth `state` + per-shop tokens
* `admin/db.py` (+ `admin/tracker.py`, `admin/analytics.py`, `admin/routes.py`)
  — the dashboard datastore
* `utils/health.py` — health probes

All of them now go through the shared engine in `database/db.py`:

* OAuth state/tokens are the ORM models `OAuthState` / `ShopToken`.
* The dashboard's hand-written SQL is unchanged but runs through a **portable
  DBAPI shim** (`database/compat.py`) that translates `?` placeholders and row
  access so the same SQL works on SQLite *and* Postgres.
* Health probes are backend-aware (PRAGMA on SQLite, `SELECT 1` on Postgres).

The dashboard/OAuth tables are now first-class ORM models
(`database/models_admin.py`), so a single Alembic migration and a single
`create_all` cover the entire schema on any backend.

There is **no `sqlite3.connect()`** left in the runtime path. (`database/
migrate_v10_1.py` still references `sqlite3` for a legacy SQLite→SQLite admin
merge, but it is guarded to no-op on non-SQLite backends.)

---

## Step 1 — Create the schema on Postgres

Point `DATABASE_URL` at Neon and run Alembic:

```bash
export DATABASE_URL='postgresql://user:pass@ep-xxx.aws.neon.tech/neondb?sslmode=require'
alembic upgrade head
```

This creates all ~50 tables (commerce + `users`, `dash_*`, `messages`,
`ai_history`, `products`, `product_sends`, `oauth_states`, `shop_tokens`).
It is idempotent. (The app also calls `create_all` on boot as an additive safety
net, but Alembic is the source of truth for versioned schema.)

> A bare `postgres://` URL is auto-normalized to `postgresql+psycopg://`
> (psycopg 3), so paste Neon's string as-is.

---

## Step 2 — Copy existing data (optional)

If you have a populated `mehaat.db` you want to preserve, use the idempotent
copy tool:

```bash
export DATABASE_URL='postgresql://…neon…?sslmode=require'   # target
python scripts/migrate_sqlite_to_postgres.py --source /path/to/mehaat.db
```

What it does:

* Ensures the schema exists on the target.
* Copies every known table in FK-safe order, **skipping rows whose primary key
  already exists** on the target (so re-running is safe — no duplicates).
* Resets Postgres `SERIAL` sequences to `MAX(id)` afterwards so new inserts
  don't collide with copied ids.
* Only ever **reads** SQLite and **writes** Postgres — the source is untouched.

Useful flags:

| Flag | Purpose |
|---|---|
| `--source URL|path` | source SQLite (default: canonical `mehaat.db`) |
| `--target URL` | target Postgres (default: `$DATABASE_URL`) |
| `--only t1,t2` / `--skip t1,t2` | restrict / exclude tables |
| `--batch N` | insert batch size (default 500) |
| `--dry-run` | report what would be copied, write nothing |
| `--no-create` | assume schema already exists (skip `create_all`) |

Example dry run first:

```bash
python scripts/migrate_sqlite_to_postgres.py --source /var/data/mehaat.db --dry-run
```

---

## Step 3 — Verify

```bash
# Schema is in sync with the models on the live DB:
alembic check          # -> "No new upgrade operations detected."

# App health:
curl -s https://<app>/health | jq '.database, .oauth.token_count'
# -> backend "postgresql", integrity "ok", and your installed-shop count
```

---

## Behavioural difference: foreign-key enforcement

SQLite does **not** enforce declared foreign keys by default, and this project
historically relied on that (the engine leaves `PRAGMA foreign_keys` at its
default OFF, unchanged by this migration). **PostgreSQL always enforces declared
foreign keys.**

In practice this only matters if some code sets a foreign-key column to a value
with no matching parent row (for example, marking a cart converted against an
order id that was never inserted into `orders`). That silently succeeds on
SQLite but raises an `IntegrityError` on Postgres. Real production flows insert
the parent first, so this is not expected to surface — but if you have custom
code that writes placeholder foreign keys, insert the parent row first (or make
the column nullable) before deploying on Postgres.

---

## Everyday schema changes (going forward)

1. Edit/add an ORM model in `database/models.py` or `database/models_admin.py`.
2. Autogenerate a migration:

   ```bash
   alembic revision --autogenerate -m "describe your change"
   ```
3. Review the generated file in `alembic/versions/`.
4. Apply it: `alembic upgrade head` (runs automatically on Render deploy).

Additive column changes are also picked up on boot by the legacy
`ensure_columns` safety net, but **Alembic is the reviewed, authoritative path**
for anything non-trivial.
