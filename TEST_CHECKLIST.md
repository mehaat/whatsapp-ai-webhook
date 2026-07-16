# QA Test Checklist — v10.1 Stable Edition

A practical checklist for verifying the v10.1 upgrade: the automated suite plus
manual checks for every fixed/added area. Work top-to-bottom; each box should be
tickable before you sign off a deployment.

---

## 1. Automated tests

- [ ] Run the full suite:

  ```bash
  pytest -q
  ```

- [ ] All tests are **green** (~361 passing, zero regressions).
- [ ] The v10.1-specific suites pass:

  ```bash
  pytest -q tests/test_v10_1_stable.py tests/test_v10_1_observability.py
  ```

- [ ] Canonical path is stable/absolute (`test_v10_1_stable.py` — repeated
      `canonical_sqlite_path()` calls return the same absolute path).
- [ ] Token store validation returns a well-formed report
      (`validate_and_recover_tokens()`).
- [ ] `merge_admin_db()` merges a legacy DB idempotently.
- [ ] `enforce_startup_validation()` warns by default and raises `SystemExit`
      only under `STRICT_STARTUP=true` with a critical var missing.

---

## 2. Database unification (one file)

- [ ] After boot, only **one** app database file exists on the mounted disk
      (`mehaat.db`); no live `mehaat_admin.db`.
- [ ] Startup log shows the canonical path:
      `DBPATH | Canonical unified SQLite database: <absolute path>`.
- [ ] The path is **absolute** and on the persistent/mounted disk (not a temp or
      CWD path).
- [ ] Inspect the tables — the `dash_*` tables exist alongside the commerce
      tables:

  ```bash
  sqlite3 /var/data/mehaat.db ".tables"
  ```

  Expect: `shop_tokens`, `oauth_states`, `users`, `dash_customers`,
  `dash_conversations`, `dash_orders`, `messages`, `ai_history`, `products`,
  `product_sends` (+ commerce tables).

- [ ] If a legacy `mehaat_admin.db` existed, it is archived as
      `mehaat_admin.db.migrated-v10_1` and startup logged
      `MIGRATE_v10_1 | Done. Copied={...}`.
- [ ] Manual migration re-run is a safe no-op:

  ```bash
  python -m database.migrate_v10_1
  # -> {'migrated': False, ...} on an already-migrated system
  ```

---

## 3. Shopify OAuth install + restart persistence (the core fix)

- [ ] Install a store:
      `https://<app-url>/shopify/install?shop=<store>.myshopify.com`.
- [ ] Callback returns `{"ok": true, ..., "installed_shops": 1}`.
- [ ] Log shows `TOKEN_SAVED | shop=... path=<mounted mehaat.db>` and
      `SHOP_INSTALLED | shop=...`.
- [ ] Log shows
      `OAUTH_DB | Token store validated ... integrity=ok shops=1 valid=1 corrupted=0`.
- [ ] `/shopify/status` lists the shop and `shop_count: 1`.
- [ ] **Restart / redeploy the service.**
- [ ] After restart, `/shopify/status` still shows `shop_count: 1` (the bug is
      fixed — it must **not** drop to 0).
- [ ] `/health` `oauth.token_count` is still ≥ 1 after restart.

---

## 4. Dashboard now updates (Products / AI / Orders)

- [ ] Send a product-search message to the WhatsApp bot (e.g. "show me sarees").
- [ ] Bot replies with product cards **and** a Gemini recommendation text.
- [ ] Admin dashboard **Products Sent** counter increments (auto-refreshes every
      7s via `/admin/api/stats`; or hit that endpoint directly).
- [ ] Admin dashboard **AI Replies** counter increments after the Gemini reply.
- [ ] Trigger an order lookup; the **Orders** view / `dash_orders` gets the row.
- [ ] Conversation appears in the inbox with the correct last message and
      direction.

---

## 5. Tracker errors are visible (no silent DB failures)

- [ ] With `DEV_MODE=true` (or `FLASK_ENV=development`), force a tracker DB
      failure (e.g. temporarily point at an unwritable path) and confirm a **full
      traceback** is logged (`ADMIN | tracker.<fn> FAILED`).
- [ ] In production mode (no DEV_MODE), the same failure logs at **ERROR** level
      (`ADMIN | tracker.<fn> failed (db issue?): ...`) — never silent DEBUG.

---

## 6. Shopify search parsing

Send each message and confirm the extracted filters / returned products make
sense:

- [ ] `799 wali saree` → `max_budget` ≈ 799.
- [ ] `under 3000` → `max_budget` = 3000.
- [ ] `3000 tak` → `max_budget` = 3000.
- [ ] `2000 se upar` → `min_budget` = 2000.
- [ ] `lal cotton saree` → color=red, fabric=cotton.
- [ ] `green cotton under 999` → color=green, fabric=cotton, max_budget=999.
- [ ] `shaadi ki saree` → occasion=wedding/party.
- [ ] A common typo (e.g. `cottn sari`) still resolves fabric=cotton via fuzzy
      matching.

---

## 7. Richer `/health`

Hit `/health` and confirm the new fields exist (existing keys preserved):

- [ ] `database` block with `path`, `size_bytes`, `size_mb`, `integrity`,
      `reachable`.
- [ ] `oauth` block with `token_count`, `shops`, `last_oauth`.
- [ ] `shopify` block with `configured` and `installed_shops`.
- [ ] `whatsapp` block with `configured`.
- [ ] `gemini` block with `configured` and `model`.
- [ ] `dashboard` block with `reachable: true` (a `dash*` table exists).
- [ ] `conversation_memory` block with `active_sessions`.
- [ ] Original keys still present: `status`, `service`, `version`,
      `shops_connected`, `components`.

---

## 8. Per-component log files

- [ ] After boot with traffic, `LOG_DIR` (default `logs/`) contains:
      `shopify.log`, `oauth.log`, `dashboard.log`, `ai.log`, `database.log`,
      `whatsapp.log`, `system.log`.
- [ ] `system.log` contains the combined stream (records also fan in from
      component loggers).
- [ ] Setting `LOG_DIR=/some/path` redirects the files there.
- [ ] On a read-only filesystem the app still starts (console-only logging, no
      crash).

---

## 9. Startup validation messages

- [ ] With all critical vars set, boot logs
      `STARTUP_VALIDATION | all checked configuration present`.
- [ ] With a critical var missing and `STRICT_STARTUP=false` (default), boot logs
      an ERROR line per issue and **continues**.
- [ ] With a critical var missing and `STRICT_STARTUP=true`, boot **aborts**
      (`SystemExit`) with a combined message naming the missing vars.
- [ ] Warning-level vars (e.g. `TOKEN_ENCRYPTION_KEY`, `DATABASE_URL`,
      `ADMIN_USERNAME`) log warnings but never abort boot.

---

## 10. Regression sanity (nothing broke)

- [ ] WhatsApp webhook verification (`GET /webhook`) still succeeds with
      `VERIFY_TOKEN`.
- [ ] Inbound WhatsApp message → bot reply round-trip works.
- [ ] Admin dashboard login works with `ADMIN_USERNAME` / `ADMIN_PASSWORD`.
- [ ] Commerce / orders / invoices flows behave as before.
- [ ] Agents / MCP / RAG / voice features unaffected (per config toggles).
- [ ] `pytest -q` still fully green after all manual testing.
</content>
