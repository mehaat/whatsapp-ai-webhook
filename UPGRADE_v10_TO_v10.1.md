# Upgrade Guide: v10.0 → v10.1 Stable Edition

v10.1 is a **backward-compatible, zero-regression** release. It fixes the
Shopify OAuth token-persistence bug, unifies all local databases into one
`mehaat.db`, and adds observability (richer `/health`, per-component logs,
fail-fast startup validation). There are **no new Python dependencies** and no
breaking API changes.

This guide walks through the upgrade, the (small) environment changes, what to
watch on first boot, and how to verify the OAuth fix.

---

## 1. Pull the code

```bash
git pull            # or deploy the v10.1 release/tag
```

## 2. Install dependencies

```bash
pip install -r requirements.txt
```

No new dependencies were added in v10.1 — this step just keeps parity with your
release process.

---

## 3. Environment changes

v10.1 introduces four **optional** new env vars, all with safe defaults:

| Variable | Default | Purpose |
|---|---|---|
| `UNIFIED_DB_PATH` | (unset) | Force the exact absolute path of the unified `mehaat.db`. Highest-priority override. |
| `DEV_MODE` | `false` | When true (or `FLASK_ENV=development`), tracker/DB failures log full tracebacks. |
| `STRICT_STARTUP` | `false` | When true, boot aborts if a **critical** config var is missing. |
| `LOG_DIR` | `logs` | Directory for per-component log files. |

### Required changes on Render (to actually fix OAuth persistence)

The OAuth bug was caused by tokens landing on an ephemeral working-directory
file. To guarantee tokens persist, make sure the canonical path resolves to
your **mounted disk**:

1. **Remove the old `ADMIN_DB_PATH` env var** (or set it to the same
   `mehaat.db`). This lets the admin dashboard unify onto the canonical file;
   the automatic migration will merge the old `mehaat_admin.db` for you.
   Leaving `ADMIN_DB_PATH` set forces the dashboard onto a separate file.
2. **Set `DATABASE_URL` to an ABSOLUTE sqlite path on the mounted disk**, e.g.:

   ```
   DATABASE_URL=sqlite:////var/data/mehaat.db
   ```

   Note the **four** slashes — `sqlite:////var/data/mehaat.db` is absolute;
   `sqlite:///mehaat.db` (three slashes) is **relative** and ephemeral. An
   absolute path is what makes tokens survive restarts.

   Alternatively, set `UNIFIED_DB_PATH=/var/data/mehaat.db` directly.

3. Ensure your Render persistent disk is mounted (commonly at `/var/data`), and
   that `TOKEN_STORE_PATH` (if set) points there too.

> If you use **Postgres** for commerce, keep `DATABASE_URL` pointing at
> Postgres. In that case set `UNIFIED_DB_PATH` (or rely on `TOKEN_STORE_PATH` /
> `/var/data`) so the token store + dashboard get a durable sqlite `mehaat.db`
> on the mounted disk.

---

## 4. First boot behaviour

On the first v10.1 start, `app.py` runs two new startup steps (both fully
guarded so they can never crash boot):

1. **Automatic DB unification** — `merge_admin_db()` merges any legacy
   `mehaat_admin.db` into `mehaat.db` and archives the legacy file as
   `*.migrated-v10_1`. See `DATABASE_MIGRATION.md`.
2. **OAuth token validation** — `validate_and_recover_tokens()` runs a SQLite
   integrity check, counts persisted shops, and decodes each token.

### Logs to watch for

```
DBPATH   | Canonical unified SQLite database: /var/data/mehaat.db
MIGRATE_v10_1 | Found legacy admin DB at ...; merging into /var/data/mehaat.db
MIGRATE_v10_1 | Done. Copied={...} legacy_renamed_to=.../mehaat_admin.db.migrated-v10_1
OAUTH_DB | Using unified SQLite persistence at /var/data/mehaat.db
OAUTH_DB | Token store validated at /var/data/mehaat.db | integrity=ok shops=N valid=N corrupted=0
STARTUP_VALIDATION | all checked configuration present
```

If no shops are persisted yet you'll see:

```
OAUTH_DB | No Shopify shops persisted. If you installed a shop and see this
           after a restart, verify DATABASE_URL/TOKEN_STORE_PATH point at the
           SAME persistent path (v10.1 unifies them at /var/data/mehaat.db).
```

---

## 5. Verify OAuth persistence is fixed

1. Install a store:

   ```
   https://<your-app-url>/shopify/install?shop=<store>.myshopify.com
   ```

   The callback should return `{"ok": true, ..., "installed_shops": 1}`.

2. Check the token-store validation log line:

   ```
   OAUTH_DB | Token store validated at /var/data/mehaat.db | integrity=ok shops=1 valid=1 corrupted=0
   ```

3. Check `/health` — the new `oauth` block should show the shop:

   ```json
   {
     "oauth": { "token_count": 1, "shops": ["<store>.myshopify.com"], "last_oauth": ... },
     "database": { "path": "/var/data/mehaat.db", "integrity": "ok", "reachable": true }
   }
   ```

4. **Restart / redeploy the service**, then hit `/health` again (or
   `/shopify/status`). `oauth.token_count` should **still be 1** and
   `shop_count` should **not** drop back to 0. That is the fix.

### Verify-the-fix checklist

- [ ] `DATABASE_URL` uses an **absolute** sqlite path (`sqlite:////...`) or
      `UNIFIED_DB_PATH` is set to a mounted-disk path.
- [ ] Old `ADMIN_DB_PATH` env var removed (or set to the same `mehaat.db`).
- [ ] Startup logs show `DBPATH | Canonical unified SQLite database: <mounted path>`.
- [ ] Startup logs show `OAUTH_DB | Token store validated ... integrity=ok`.
- [ ] `/health` `database.path` points at the mounted disk (not a temp/CWD path).
- [ ] Install a shop → `installed_shops: 1`.
- [ ] **Restart** → `/health` `oauth.token_count` still ≥ 1 (no `shop_count=0`).
- [ ] Legacy `mehaat_admin.db` archived as `*.migrated-v10_1` (if one existed).

---

## 6. Enabling DEV_MODE / STRICT_STARTUP

- **`DEV_MODE=true`** (or `FLASK_ENV=development`) makes the admin tracker log
  **full tracebacks** (`logger.exception`) for tracking/DB failures instead of
  a terse one-line error. Use it while diagnosing dashboard/DB issues; leave it
  off in production (production still logs at ERROR level — failures are never
  silent).

- **`STRICT_STARTUP=true`** makes the app **abort boot** (exit) if a *critical*
  config var is missing (e.g. `SHOPIFY_APP_URL`, `WHATSAPP_TOKEN`,
  `GEMINI_API_KEY`, `ADMIN_PASSWORD`). With it off (default), missing vars only
  produce warnings so existing deployments and tests keep booting. Turn it on
  in production once your environment is fully configured to fail fast on
  misconfiguration.

---

## 7. Where the logs go

v10.1 writes structured per-component log files under `LOG_DIR` (default
`logs/`), in addition to console logging:

```
logs/
├── shopify.log
├── oauth.log
├── dashboard.log
├── ai.log
├── database.log
├── whatsapp.log
└── system.log     (combined stream of everything)
```

Files rotate at ~2 MB with 3 backups. On a read-only filesystem the app
silently falls back to console-only logging (it never crashes).

---

## 8. Rollback

If needed, roll back per `DATABASE_MIGRATION.md` §6: stop the app, rename
`mehaat_admin.db.migrated-v10_1` back to `mehaat_admin.db`, restore
`ADMIN_DB_PATH`, and deploy the previous release. v10.1 deletes no data, so
rollback is non-destructive.
</content>
