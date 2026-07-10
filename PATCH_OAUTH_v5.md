# v5 Production Patch — Persistent Shopify OAuth (multi-worker fix)

## The bug

`/shopify/callback` returned:

```json
{ "error": "Invalid or expired OAuth state" }
```

The OAuth `state` (and the access tokens) lived in **process memory**
(`StateManager._states = {}` and an in-memory `TokenStore`). Under Gunicorn with
more than one worker, the worker that issues the state on `/shopify/install` is
usually not the worker that receives `/shopify/callback`, so the callback
worker's memory has no record of the state → the state check fails. In-memory
tokens also disappeared on every restart/deploy.

## The fix (implementation-only)

**Only the storage implementation inside `shopify/auth.py` was replaced.** Both
the state store and the token store now persist to a shared **SQLite** database
using the Python standard library (`sqlite3` — no new dependency). Every
Gunicorn worker reads and writes the same durable tables, so state survives
across workers, restarts, deploys and Render container recycles.

Nothing else changed. These public objects are byte-for-byte compatible:

- `shopify_auth_bp`, `token_store`, `TokenStore`, `StateManager`, `_state_manager`
- `is_valid_shop_domain`, `verify_hmac`, `build_authorization_url`,
  `exchange_code_for_token`, `_normalise_shop`, and all `_get_*` helpers
- Routes `/shopify/install`, `/shopify/callback`, `/shopify/status`
- `__all__` is unchanged

`app.py`, the webhook, Gemini, WhatsApp, memory, conversation history, product
search, FAQ and the logging API were **not touched**. No other file was
modified.

### Compatibility note (important)

`shopify/client.py` and `shopify/search.py` resolve tokens via
`token_store.get_token(shop)`. The previous class only defined `get()`. The new
`TokenStore` provides **both** `get()` and `get_token()` (an alias), so every
existing call site keeps working.

## Storage design

Database location (in priority order):

1. **`DATABASE_URL`** when it is a `sqlite://` URL (reused as requested) — e.g.
   `sqlite:////var/data/mehaat.db`.
2. Otherwise **`/var/data/mehaat.db`** (Render's mounted persistent disk).
3. Fallbacks for local/dev: the `TOKEN_STORE_PATH` directory, then the working
   directory. Tables are created automatically (`CREATE TABLE IF NOT EXISTS`) —
   no migration tools.

Tables:

```sql
CREATE TABLE oauth_states (
    state      TEXT PRIMARY KEY,
    shop       TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE shop_tokens (
    shop         TEXT PRIMARY KEY,
    access_token TEXT NOT NULL,
    installed_at REAL NOT NULL,
    updated_at   REAL NOT NULL
);
```

Concurrency & safety:

- **WAL journalling** + a 30s **busy timeout** on every connection → many
  readers run concurrently with a single writer; workers wait for a lock
  instead of erroring.
- A connection is opened per operation (sqlite3 objects are not thread-safe to
  share); a process-local write lock serialises writers within a worker while
  SQLite's file lock serialises writers across workers.
- **`consume()` is a single atomic `DELETE ... RETURNING`** — a state can be
  used at most once even if two workers race the same callback (verified: 8
  concurrent consumers of one state → exactly one winner).

## Security

- Cryptographically random state (`secrets.token_urlsafe(32)`).
- **10-minute expiry** (`STATE_TTL_SECONDS = 600`) enforced on consume.
- **Single-use** delete-on-lookup → replay protection.
- Shop binding compared with **`secrets.compare_digest`** (constant time); HMAC
  compared with `hmac.compare_digest`.
- Strict `*.myshopify.com` validation (unchanged).
- All errors are JSON only (`400/401/403/500/502`), never HTML (unchanged).

## Structured logging

`OAUTH_INSTALL`, `OAUTH_CALLBACK`, `STATE_CREATED`, `STATE_VALIDATED`,
`STATE_EXPIRED`, `STATE_INVALID`, `STATE_MISMATCH`, `STATE_CLEANUP`,
`TOKEN_SAVED`, `SHOP_INSTALLED`, `SHOP_REMOVED`, `OAUTH_DB`.

## Verification performed

- **Public API**: every symbol/method present, including `get` **and**
  `get_token`; `__all__` unchanged.
- **Original multi-worker bug**: state issued in one OS process (worker A) is
  successfully consumed in a **separate** process (worker B) via the shared DB.
- **Replay**: consuming the same state twice → second attempt rejected (403
  `Invalid or expired OAuth state`).
- **Expiry**: a 1-second-TTL state is rejected after expiry.
- **Shop binding**: mismatched shop rejected (and state still consumed).
- **Restart**: a fresh process reads tokens written by a previous process.
- **Full callback route**: valid state + correct HMAC → `200` and token
  persisted (token exchange mocked in the test); bad state → `403`; missing
  shop → `400`; `/install` → `302`; `/status` → `200`.
- **Concurrency**: 8 workers × 60 issue+consume = 480/480 succeed, no
  "database is locked"; concurrent double-consume of one state → exactly one
  winner.
- **Downstream imports**: `shopify.client`, `shopify.search`, `utils.health`,
  `admin.shopify_lookup` and `app` all import and boot; `/`, `/health`,
  `/shopify/*`, `/webhook` all present.

## Deploy on Render

No config change is required if you already set `DATABASE_URL` (the existing
`render.yaml` uses `sqlite:////var/data/mehaat.db` on the mounted disk) — the
patch reuses it. Just deploy; tables are created on first request. Works with 1,
2 or 8 workers.
