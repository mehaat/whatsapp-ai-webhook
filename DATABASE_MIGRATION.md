# Database Migration — v10.1 Stable Edition

This document explains the database unification introduced in **v10.1**, why it
was needed, exactly what the automatic migration does, how to run it manually,
how your data is preserved, and how to roll back.

> **TL;DR** — v10.1 makes the OAuth token store, the admin dashboard and the
> SQLAlchemy commerce layer all use **one** deterministic, absolute
> `mehaat.db`. The dashboard's three colliding tables are renamed to `dash_*`
> so they can coexist in that single file. A safe, idempotent migration
> (`database/migrate_v10_1.py`) merges any legacy `mehaat_admin.db` into the
> unified database at startup. **No data is deleted.**

---

## 1. What was unified, and why

### The root cause — `installed_shops=1` then `shop_count=0`

Before v10.1, three subsystems each resolved their **own** SQLite file:

| Subsystem | File resolver (pre-v10.1) |
|---|---|
| Shopify OAuth token store (`shopify/auth.py`) | "first writable candidate wins" probing |
| Admin dashboard (`admin/config.py` → `admin/db.py`) | `mehaat_admin.db` |
| SQLAlchemy commerce (`database/db.py`) | `DATABASE_URL` |

The token store's probing logic could land on a **relative** sqlite path that
resolved against the process working directory. On Render that directory is
**ephemeral** — it is wiped on every restart/redeploy/container recycle. So a
store would install successfully (the OAuth callback returned
`installed_shops=1`), but after the next restart the token file was gone and
`shop_count` read back as `0`. The three layers could also simply disagree
about where "the database" was.

### The fix — one canonical path

v10.1 adds `utils/dbpath.py` `canonical_sqlite_path()`, the **single source of
truth** for the database location. It resolves **once**, is **cached**, and is
always made **absolute** so a changing working directory can never repoint the
database.

**Resolution priority (first match wins):**

1. `UNIFIED_DB_PATH` environment variable (explicit operator override).
2. A sqlite path parsed from `DATABASE_URL`.
3. A `mehaat.db` next to `TOKEN_STORE_PATH` (Render's mounted disk).
4. `/var/data/mehaat.db` when `/var/data` exists (Render default).
5. `./mehaat.db` in the current working directory (dev/tests fallback).

Every layer now calls this resolver:

- `shopify/auth.py` `_resolve_db_path()` → `canonical_sqlite_path()`
- `admin/config.py` `_default_db_path()` → `canonical_sqlite_path()`
  (unless `ADMIN_DB_PATH` is explicitly set)
- `database/db.py` `get_engine()` → `canonical_sqlite_url()` when the backend
  is sqlite

Result: tokens, admin dashboard data and commerce data all live in a single,
durable `mehaat.db`.

---

## 2. The `dash_*` table renames

The admin dashboard historically defined three tables whose names collide with
the SQLAlchemy commerce schema: `customers`, `conversations`, `orders`. To let
both coexist in one file, the **dashboard** copies were renamed:

| Old admin table | New admin table (v10.1) |
|---|---|
| `customers` | `dash_customers` |
| `conversations` | `dash_conversations` |
| `orders` | `dash_orders` |

The dashboard's remaining tables keep their original names because they do not
collide: `users`, `messages`, `ai_history`, `products`, `product_sends`.

The commerce layer's own `customers` / `conversations` / `orders` tables are
untouched — they now simply live alongside the `dash_*` tables in the same
`mehaat.db`.

---

## 3. What `merge_admin_db()` does

`database/migrate_v10_1.py` provides `merge_admin_db()`, run automatically at
startup from `app.py` (and available as a standalone command). Step by step:

1. **Locate a legacy file.** It looks for a pre-v10.1 `mehaat_admin.db` in
   these candidate locations (excluding the unified file itself):
   - `ADMIN_DB_PATH` (if set)
   - next to `TOKEN_STORE_PATH`
   - next to the unified `mehaat.db`
   - the current working directory
   If none exists (fresh install or already migrated), it is a **no-op**.
2. **Ensure the unified schema exists.** It calls `admin.db.init_db()` so the
   `dash_*` (and other) tables are present in `mehaat.db` before copying.
3. **Attach and copy.** It `ATTACH`es the legacy DB as `legacy`, opens a
   `BEGIN IMMEDIATE` transaction, and copies each legacy table into its unified
   target using this map:

   | Legacy table | Unified target |
   |---|---|
   | `users` | `users` |
   | `customers` | `dash_customers` |
   | `conversations` | `dash_conversations` |
   | `messages` | `messages` |
   | `ai_history` | `ai_history` |
   | `products` | `products` |
   | `product_sends` | `product_sends` |
   | `orders` | `dash_orders` |

   Copies use `INSERT OR IGNORE` over the **shared columns** of source and
   target, so it never overwrites or deletes rows already present. Row counts
   before/after are recorded in the report.
4. **Archive the legacy file.** On success the legacy `mehaat_admin.db` is
   renamed to `mehaat_admin.db.migrated-v10_1` (a timestamp suffix is added if
   that name already exists) so it is never reprocessed.
5. **Return a report** — `{"migrated", "source", "copied", "renamed_to"}`.

The whole operation is wrapped so a migration failure is logged and **never
crashes startup** (it rolls back and continues).

### Idempotency

Because copies use `INSERT OR IGNORE` and the legacy file is renamed after a
successful merge, running the migration repeatedly is safe: the second run
finds no legacy file and does nothing.

---

## 4. Running the migration manually

The migration runs automatically on the first v10.1 boot, but you can also run
it on demand:

```bash
python -m database.migrate_v10_1
```

This prints the migration report, e.g.:

```python
{'migrated': True,
 'source': '/var/data/mehaat_admin.db',
 'copied': {'customers->dash_customers': 12, 'messages->messages': 340, ...},
 'renamed_to': '/var/data/mehaat_admin.db.migrated-v10_1'}
```

A no-op run (nothing to migrate) returns:

```python
{'migrated': False, 'source': None, 'copied': {}, 'renamed_to': None}
```

You can point the migration at a specific unified file by exporting the same
env used by the app (`UNIFIED_DB_PATH` / `DATABASE_URL` / `TOKEN_STORE_PATH`)
before running it.

---

## 5. How data is preserved

- **Additive only.** Copies use `INSERT OR IGNORE`; existing rows are never
  overwritten or deleted.
- **Column-aware.** Only columns shared by the source and target tables are
  copied, so schema drift can't break the merge.
- **Legacy file archived, not removed.** The original `mehaat_admin.db` is
  renamed to `*.migrated-v10_1` and kept on disk.
- **Transactional.** The merge runs inside a `BEGIN IMMEDIATE` transaction and
  rolls back on any error.

---

## 6. Rollback notes

v10.1 does not destroy anything, so rollback is simple:

1. Stop the app.
2. The pre-v10.1 admin data is still on disk as
   `mehaat_admin.db.migrated-v10_1`. Rename it back to `mehaat_admin.db`.
3. Set `ADMIN_DB_PATH` back to that file (this forces the dashboard to use a
   separate file again, restoring pre-v10.1 behaviour for the admin data).
4. Redeploy the previous release.

Note that the unified `mehaat.db` retains the merged copy regardless — rolling
back just re-separates the admin dashboard's data source.

---

## 7. Before / after table layout

**Before v10.1 — two files:**

```
mehaat.db            (token store: oauth_states, shop_tokens; + commerce tables)
mehaat_admin.db      (users, customers, conversations, messages,
                      ai_history, products, product_sends, orders)
```

**After v10.1 — one file (`mehaat.db`):**

```
mehaat.db
├── oauth_states           (OAuth CSRF state — token store)
├── shop_tokens            (per-shop access tokens — token store)
├── users                  (admin dashboard)
├── dash_customers         (admin dashboard — renamed from customers)
├── dash_conversations     (admin dashboard — renamed from conversations)
├── dash_orders            (admin dashboard — renamed from orders)
├── messages               (admin dashboard)
├── ai_history             (admin dashboard)
├── products               (admin dashboard)
├── product_sends          (admin dashboard)
└── customers / conversations / orders / ...   (SQLAlchemy commerce tables)

mehaat_admin.db.migrated-v10_1   (archived legacy file — kept for rollback)
```

---

## 8. FAQ

**Will I lose any data?**
No. The migration only ever adds rows (`INSERT OR IGNORE`), never deletes them,
and it keeps the original admin file as `*.migrated-v10_1`.

**What about Postgres?**
Commerce data stays on Postgres exactly as before — set `DATABASE_URL` to your
Postgres URL and the SQLAlchemy commerce layer uses it directly. The OAuth
token store and the admin dashboard remain SQLite and use the canonical
`mehaat.db` file. In other words, a non-sqlite `DATABASE_URL` only affects the
commerce layer; the token store + dashboard still unify on the local
`mehaat.db`.

**Do I need to run anything by hand?**
No. `merge_admin_db()` runs automatically at startup. The
`python -m database.migrate_v10_1` command is only there if you want to run or
verify it manually.

**What if there's no `mehaat_admin.db`?**
Fresh installs (and already-migrated deployments) have nothing to merge — the
migration is a silent no-op.

**I set `ADMIN_DB_PATH` — will the dashboard still unify?**
No. An explicit `ADMIN_DB_PATH` forces the dashboard onto a separate file
(kept for backward compatibility). Remove it (or point it at the same
`mehaat.db`) to get unification. On Render, remove the old `ADMIN_DB_PATH` env
var.
</content>
</invoke>
