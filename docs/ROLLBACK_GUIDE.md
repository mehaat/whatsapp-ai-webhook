# Rollback Guide

This migration is designed to be **low-risk and reversible**. The backend is
chosen entirely by `DATABASE_URL`, so reverting is largely an environment
change, not a code change. Choose the level of rollback that fits your
situation.

---

## Level 0 — Instant: switch the backend back (no redeploy of old code)

Because the same code runs on SQLite and Postgres, you can move off Postgres
without reverting any code:

* Set `DATABASE_URL=sqlite:////var/data/mehaat.db` (or any writable path), **or**
  point `DATABASE_URL` at a different/previous Postgres database.
* Restart the service.

The app will use that backend immediately. (On Render Free, note SQLite is not
durable without a disk — this is a temporary escape hatch, not a Free-tier
production target.)

> If you had only ever run on Postgres, there may be no local SQLite data to
> fall back to — in that case prefer Level 1 or keep using Postgres.

---

## Level 1 — Roll back the schema (Alembic)

If a schema change misbehaves, step the database back one revision:

```bash
export DATABASE_URL='postgresql://…?sslmode=require'
alembic downgrade -1        # undo the latest migration
# or to a specific revision:
alembic downgrade <revision_id>
# or remove all app tables:
alembic downgrade base
```

The **initial** migration is `initial unified schema (commerce + admin
dashboard + oauth)`. `alembic downgrade base` drops every app table it created.

> Take a Neon backup/branch first (see Level 3) — `downgrade base` is
> destructive.

---

## Level 2 — Revert the code

To return to the previous (pre-migration) code entirely:

```bash
git checkout <previous-commit-or-tag>
# redeploy
```

Then set `DATABASE_URL` back to the SQLite value the old code expected
(`sqlite:////var/data/mehaat.db`) and, on Render, re-add the persistent disk the
old `render.yaml` declared (`mountPath: /var/data`). The old code will read the
same SQLite file it used before.

> Nothing in this migration deletes or rewrites your old SQLite file — the
> data-copy tool only reads it. So the previous code + previous SQLite file
> continue to work if you roll back to them.

---

## Level 3 — Data safety (do this BEFORE any destructive step)

Neon makes rollback safe via **branches** and **point-in-time restore**:

1. **Branch before migrating:** in the Neon console, create a branch of your
   database. If anything goes wrong, repoint `DATABASE_URL` at the branch (or
   restore from it) — zero data loss.
2. **Point-in-time restore:** Neon retains history; you can restore the database
   to a timestamp just before the change.
3. **SQLite source is untouched:** the copy tool (`scripts/
   migrate_sqlite_to_postgres.py`) never writes to SQLite, so your original
   `mehaat.db` remains a valid rollback source.

---

## Decision table

| Situation | Action |
|---|---|
| Postgres URL/credentials wrong, app won't boot | Fix `DATABASE_URL`; restart (Level 0) |
| One migration broke something | `alembic downgrade -1` (Level 1) |
| Need to abandon Postgres temporarily | Point `DATABASE_URL` at SQLite/other DB (Level 0) |
| Need to fully revert to old release | `git checkout` old code + restore disk + SQLite URL (Level 2) |
| Data corruption / bad data copy | Restore Neon branch / PITR (Level 3); re-run copy tool (idempotent) |

---

## Re-running the data copy safely

The copy tool is **idempotent**: it skips rows whose primary key already exists.
If a copy was interrupted, just run it again — it will fill in only the missing
rows and re-sync sequences. No duplicates are created.
