# Changelog

All notable changes to the **ME-HAAT Fashion AI Bot** are documented in this
file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project aims to follow semantic-ish versioning.

## [10.1.0] - 2026-07-16

**Stable Edition.** A backward-compatible, zero-regression release focused on
fixing Shopify OAuth token persistence, unifying the local databases into a
single file, and adding observability. No new dependencies; ~361 tests pass.

### Fixed

- **Shopify OAuth token persistence (the `shop_count=0` bug).** Previously the
  token store could resolve a *relative* sqlite path that landed on an
  **ephemeral** working-directory file, so a store installed successfully
  (`installed_shops=1`) but read back as `shop_count=0` after a restart/redeploy.
  Tokens now persist to the single canonical, absolute `mehaat.db`. `save()`
  uses a `BEGIN IMMEDIATE` transaction with read-back verification, logs the
  resolved path, and uses `ON CONFLICT(shop)` to prevent duplicate shops.
  (`shopify/auth.py`)
- **Silent dashboard counters.** The admin tracker no longer swallows database
  errors at DEBUG level — the historical cause of the dashboard "Products Sent"
  / "AI Replies" counters silently not updating. Failures are now always
  surfaced. (`admin/tracker.py`)
- **Fragmented database paths.** The OAuth token store, admin dashboard and
  SQLAlchemy commerce layer no longer each guess their own file; all resolve
  the same absolute `mehaat.db`, so they can never disagree about "the
  database". (`utils/dbpath.py`, `shopify/auth.py`, `admin/config.py`,
  `database/db.py`)

### Changed

- **Unified database path.** New `utils/dbpath.py` `canonical_sqlite_path()` is
  the single source of truth for the SQLite location — resolved once, cached,
  and always **absolute**. Priority: `UNIFIED_DB_PATH` env > sqlite path from
  `DATABASE_URL` > next to `TOKEN_STORE_PATH` > `/var/data/mehaat.db` >
  `./mehaat.db`.
- **Admin dashboard tables renamed.** To coexist with the commerce schema in one
  file, the dashboard's three colliding tables were renamed to `dash_customers`,
  `dash_conversations` and `dash_orders`. Non-colliding tables (`users`,
  `messages`, `ai_history`, `products`, `product_sends`) keep their names.
  (`admin/db.py`, `admin/tracker.py`)
- **Improved Shopify search parsing.** `extract_search_filters()` now handles
  more price phrasings ("799 wali", "under 3000", "3000 tak", ranges,
  "2000 se upar"), plus color/fabric/occasion in Hindi/Hinglish and common
  typos. Existing dict keys and semantics are unchanged. (`shopify/search.py`)
- **Verified event pipeline.** The end-to-end flow is confirmed and instrumented:
  `record_inbound` → search → `record_products_sent` → send cards → Gemini
  recommendation → `record_ai` → send text → `record_outbound`. Combined with
  the unified DB and non-silent tracker, the dashboard's Products Sent / AI
  Replies / counters now populate (the dashboard already auto-refreshes every
  7s via `/admin/api/stats`).

### Added

- **Startup DB migration** — `database/migrate_v10_1.py` `merge_admin_db()`
  runs at startup to merge any legacy `mehaat_admin.db` into the unified
  `mehaat.db` using `INSERT OR IGNORE` (idempotent, never deletes), then
  archives the legacy file as `*.migrated-v10_1`. Also available standalone via
  `python -m database.migrate_v10_1`.
- **OAuth self-check** — `validate_and_recover_tokens()` runs at startup: SQLite
  `integrity_check`, counts persisted shops, decodes each token, and logs a
  clear report (`OAUTH_DB | Token store validated ... shops=N`), making the old
  `shop_count=0` failure visible at boot instead of silently at request time.
  (`shopify/auth.py`)
- **Non-silent tracker logging** — `admin/tracker.py` `_safe` now logs
  `logger.exception` in development (`DEV_MODE` / `FLASK_ENV=development`) and
  `logger.error` in production. New `dev_mode` config flag. (`config.py`)
- **Richer `/health`** — adds `database` (path/size/integrity), `oauth`
  (token_count/shops/last_oauth), `shopify`/`whatsapp`/`gemini` configured
  flags, `dashboard` reachability and `conversation_memory`. All existing
  top-level keys are preserved; all new probes are cheap and guarded.
  (`utils/health.py`)
- **Structured per-component logs** — `utils/logging.py` `get_component_logger()`
  writes `shopify.log`, `oauth.log`, `dashboard.log`, `ai.log`, `database.log`,
  `whatsapp.log` and a combined `system.log` under `LOG_DIR` (default `logs/`),
  with rotation. Falls back to console-only on a read-only filesystem.
- **Fail-fast startup validation** — `config.py` `validate_startup()` +
  `enforce_startup_validation()` check `SHOPIFY_APP_URL`,
  `TOKEN_ENCRYPTION_KEY`, `VERIFY_TOKEN`, `PHONE_NUMBER_ID`, `WHATSAPP_TOKEN`,
  `DATABASE_URL`, `ADMIN_SECRET_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`,
  `GEMINI_API_KEY`. Boot is aborted only when `STRICT_STARTUP=true` and a
  critical var is missing; otherwise issues are logged as warnings.
- **New environment variables** — `UNIFIED_DB_PATH`, `DEV_MODE`,
  `STRICT_STARTUP`, `LOG_DIR` (all optional, safe defaults). See
  `UPGRADE_v10_TO_v10.1.md`.

### Migration & deployment notes

- On Render, **remove the old `ADMIN_DB_PATH`** env var (or set it to the same
  `mehaat.db`) so admin data unifies; the migration merges the old
  `mehaat_admin.db` automatically.
- On Render, set `DATABASE_URL` to an **absolute** sqlite path on the mounted
  disk (e.g. `sqlite:////var/data/mehaat.db`) so tokens persist. See
  `UPGRADE_v10_TO_v10.1.md` and `DATABASE_MIGRATION.md`.

### Preserved

All prior functionality is fully preserved and non-regressed — WhatsApp Cloud
API messaging, Shopify OAuth, Gemini AI replies and recommendations, the admin
dashboard, conversation memory, product recommendations/visual search/AI
stylist, the multi-agent orchestrator, RAG knowledge base, MCP tool server,
voice agent, commerce (orders/payments/invoices/shipping), background jobs,
multi-tenancy and the developer portal. Approximately **361 tests pass**.

---

### Previous versions

This changelog begins at 10.1.0. For the history of earlier releases (v4.0
through v10.0 — WhatsApp, Shopify OAuth, Gemini, admin dashboard, commerce,
enterprise scale, AI agents/MCP), see the `README` and the per-version notes in
the module docstrings.
</content>
