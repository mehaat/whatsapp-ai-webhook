# ME-HAAT Fashion AI Bot — Admin Dashboard (v4.2, additive module)

A login-protected admin console mounted at **`/admin`**. It is a self-contained
Flask blueprint plus a best-effort event tracker. **Nothing in the existing
WhatsApp webhook, Shopify OAuth, product search, Gemini AI, health checks, or
any existing route was rewritten or changed in behaviour.** Every integration
point is additive and exception-guarded, so the bot runs exactly as before even
if the dashboard's database is unavailable or the admin credentials are unset.

---

## 1. How data reaches the dashboard (important design note)

The existing conversation store (`memory/store.py`) is intentionally ephemeral
(last 10 turns per user, 1-hour expiry, no timestamps/intents), and the optional
SQLAlchemy layer only persists AI logs when `USE_DATABASE=true`. Neither is a
suitable source of truth for an inbox / history / analytics dashboard.

So the dashboard adds its **own** durable datastore using the Python standard
library `sqlite3` — **no dependency on `USE_DATABASE`**. A handful of guarded
"tracker" hooks in `app.py` record live traffic (inbound messages, bot replies,
products shown, AI generations) into that store. On Render the database lives on
the mounted persistent disk (`/var/data/mehaat_admin.db`) so it survives restarts
and is shared across Gunicorn workers.

Consequence: immediately after deploy the dashboard is empty (no dummy data, as
required) and populates as real WhatsApp messages arrive.

---

## 2. New files (all under `admin/`)

| File | Purpose |
|------|---------|
| `admin/__init__.py` | `init_admin(app)` — configures sessions/cookies and registers the blueprint. |
| `admin/config.py` | Reads `ADMIN_*` env vars into an immutable config (safe defaults). |
| `admin/db.py` | Stdlib-sqlite3 datastore: schema (`users, customers, messages, conversations, orders, products, ai_history`) + WAL + connection helper. |
| `admin/tracker.py` | Best-effort, fully guarded event recorder called from `app.py`. |
| `admin/security.py` | Password hashing/verify, `login_required`, CSRF, login rate-limit, session-timeout, cookie config. |
| `admin/analytics.py` | Read-only query layer (dashboard, inbox, chat, AI history, analytics, search, customers). |
| `admin/exporter.py` | CSV (stdlib) + Excel (openpyxl) + PDF (reportlab) export, deps guarded. |
| `admin/shopify_lookup.py` | Thin adapter over `shopify/orders.py` for **live** order lookup (degrades gracefully). |
| `admin/routes.py` | All `/admin` pages + JSON APIs + export endpoint. |
| `admin/static/dashboard.css` | Light/dark theme, sidebar, cards, inbox, responsive. |
| `admin/static/dashboard.js` | Theme toggle, CSRF-fetch, auto-refresh, notifications (badge + optional sound). |
| `admin/templates/admin/*.html` | `base, login, dashboard, inbox, chat, ai_history, orders, customers, customer_detail, analytics, search` (+ `_conv_items` partial). |

## 3. Modified files (additive only)

### `app.py`
1. **Guarded import** of the admin module (falls back to no-ops if it can't
   import, so startup is never affected):
   ```python
   try:
       from admin import init_admin
       from admin import tracker as admin_tracker
   except Exception as exc:
       logger.error("ADMIN | Dashboard unavailable, continuing without it: %s", exc)
       def init_admin(_app): return None
       class _NoopTracker: ...
       admin_tracker = _NoopTracker()
   ```
2. **One line** after `register_middleware(app)`:
   ```python
   init_admin(app)   # mounts /admin, registers session config
   ```
3. **Five guarded tracker hooks** at existing reply points (each cannot raise):
   - `record_inbound(...)` after the inbound turn is stored in `handle_customer_message`.
   - `record_outbound(...)` inside `_finalize_reply` (captures every text reply).
   - `record_products_sent(...)` inside `_handle_product_search` and `_send_next_product_page`.
   - `record_ai(...)` inside `generate_ai_reply` (with latency + fallback flag).

   *Reason:* these are the only points that observe real traffic; without them
   the dashboard could only show fake data. Each call is wrapped so a tracking
   failure is logged and swallowed — the reply path is untouched.

### `requirements.txt`
Added `openpyxl>=3.1.2` and `reportlab>=4.0.0` (Excel/PDF export). Both are
imported lazily; CSV export works without them. *Reason:* export feature.

### `render.yaml`
Added `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ADMIN_PASSWORD_HASH`,
`ADMIN_SECRET_KEY` (all `sync: false`), `ADMIN_DB_PATH=/var/data/mehaat_admin.db`
(persistent disk), and `ADMIN_SESSION_TIMEOUT_MIN=60`. *Reason:* configure the
dashboard on Render. The existing `startCommand`/`healthCheckPath` are unchanged.

### `.env.example`
Documented the new `ADMIN_*` variables. *Reason:* local/dev configuration.

> `config.py` and all other existing files are **unchanged**.

---

## 4. Environment variables

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `ADMIN_USERNAME` | yes (for login) | `admin` | Login username. |
| `ADMIN_PASSWORD` | yes* | — | Plaintext password. |
| `ADMIN_PASSWORD_HASH` | optional | — | Werkzeug hash; takes precedence over `ADMIN_PASSWORD`. |
| `ADMIN_SECRET_KEY` | recommended | derived | Session-signing secret (stable across workers). |
| `ADMIN_DB_PATH` | optional | next to `TOKEN_STORE_PATH` | Dashboard SQLite path. |
| `ADMIN_SESSION_TIMEOUT_MIN` | optional | `60` | Idle-session timeout (minutes). |
| `ADMIN_LOGIN_MAX_ATTEMPTS` / `ADMIN_LOGIN_WINDOW_SEC` | optional | `5` / `300` | Per-IP login throttle. |

\* Either `ADMIN_PASSWORD` or `ADMIN_PASSWORD_HASH` must be set for login to work.
If neither is set, the app still starts and the bot is unaffected; the login page
simply returns "credentials not configured" (503) until you set them.

Generate a strong secret / hash:
```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"                       # ADMIN_SECRET_KEY
python -c "from werkzeug.security import generate_password_hash as g; print(g('YOUR_PASSWORD'))"  # ADMIN_PASSWORD_HASH
```

---

## 5. Deploy on Render

1. Commit the new `admin/` package and the modified files; push to the branch
   Render builds.
2. In Render → **Environment**, set `ADMIN_USERNAME`, `ADMIN_PASSWORD` (or
   `ADMIN_PASSWORD_HASH`) and `ADMIN_SECRET_KEY`. `ADMIN_DB_PATH` and the disk are
   already declared in `render.yaml`.
3. Deploy (build runs `pip install -r requirements.txt`, which now includes
   openpyxl + reportlab).
4. Visit **`https://<your-app>.onrender.com/admin/login`** and sign in.

No change to the Shopify OAuth flow, WhatsApp webhook, or health checks is
required.

---

## 6. Feature coverage

- **Secure login** — env-var credentials, Flask session, idle timeout, logout,
  per-IP login throttle, every page protected (`admin/security.py`).
- **Dashboard home** — cards (Total Customers, Today's Messages, Total
  Conversations, Products Sent, AI Replies, Shopify Orders) + Daily Messages,
  Top Customers and Popular Products charts, auto-refreshing.
- **Live inbox** — messenger UI, name/phone/last-message/unread/time, search,
  unread filter, click-to-open chat in the right pane, **auto-refresh every 5s**
  with no page reload.
- **Customer chat history** — full timeline, timestamps, language/intent,
  AI response times, in-conversation search, CSV/PDF export.
- **AI response history** — prompt context, response, latency, fallback flag,
  errors, with search + date-range + fallback-only filters and Excel export.
- **Shopify order lookup** — live query by order number / phone (and customer by
  phone) via `shopify/orders.py`; payment/fulfilment/tracking; "Open in Shopify".
- **Customer search / global search** — customers, messages, products, orders.
- **Filters** — today / yesterday / last 7 / last 30 / custom range / unread /
  fallback-only.
- **Export** — CSV, Excel (.xlsx), PDF for messages, chat, customers, AI history,
  orders (with date-range where relevant).
- **Notifications** — sidebar unread badge, bell dot, optional sound, live poll.
- **Analytics** — messages, AI accuracy, avg response time, products, orders,
  active customers, conversion rate, top customers/products, volume chart.
- **Customer details** — profile, counts, products recommended, orders.
- **Database** — auto-creates SQLite (`users, customers, messages, conversations,
  orders, products, ai_history`) regardless of `USE_DATABASE`.
- **UI** — Bootstrap 5, responsive (desktop/tablet/mobile), dark + light mode,
  sidebar + top nav, icons, animations.
- **Security** — CSRF on state-changing POST, session protection + timeout,
  login-required, login rate-limiting, input validation, XSS-safe rendering
  (Jinja auto-escaping + JS `esc()`), secure cookies (HttpOnly, SameSite, Secure
  under HTTPS).
- **API** — `/admin/login`, `/admin/logout`, `/admin/dashboard`, `/admin/messages`
  (inbox), `/admin/customers`, `/admin/orders`, `/admin/history` (AI), `/admin/search`,
  `/admin/export`, plus JSON APIs under `/admin/api/*`.

---

## 7. Verification performed

A full smoke test (run against `app:app`, the same entry point Gunicorn uses)
confirmed:

- All existing routes intact: `/`, `/health`, `/shopify/status`, `/shopify/install`,
  `/shopify/callback`, `/webhook`.
- All `/admin` pages present and returning 200 when authenticated.
- Unauthenticated dashboard → redirect to login; unauthenticated API → 401.
- Wrong password → 401; correct password → session established.
- Tracker records real inbound/outbound/AI/product events; stats, inbox, chat,
  AI history, search and analytics APIs reflect them.
- CSV / XLSX / PDF export for every dataset returns a valid attachment.
- CSRF: state-changing POST without a token → 400; with the session token → 200.
- App boots and the bot works with **no** `ADMIN_*` variables set.
- The real `handle_customer_message` orchestration path runs unchanged with the
  new guarded hooks (greeting + AI paths both produce replies and are recorded).
