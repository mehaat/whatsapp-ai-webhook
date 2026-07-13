# ME-HAAT Fashion AI Bot v10.0 Enterprise Edition

Production-ready WhatsApp **Commerce Platform** for **ME-HAAT Fashion** (Premium Sarees & Ethnic Wear) — Shopify OAuth, ordering, payments, invoices, tracking, coupons/returns/shipping, CRM, RBAC/2FA, Celery/Redis, monitoring, multi-tenant, compliance, Kubernetes/Helm, Advanced AI Commerce, and a **multi-agent AI orchestrator** with a RAG knowledge base, an MCP tool server, and a human-approval workflow.

## What's New in v10.0 — AI-First Architecture

Additive over v9.0 — all prior features preserved; every agent surface is guarded and feature-flagged. This release shifts from features to an **agentic architecture**.

- **AI Orchestrator + specialist agents** — one orchestrator classifies each message and routes it to the right specialist: **Sales**, **Customer Support**, **Inventory**, **Marketing**, **Analytics**, and **Voice**. Each agent works over a shared **tool registry** (search, order status, returns, tickets, recommendations, stylist, stock, analytics, coupons, broadcasts, refunds…). Exposed via `POST /api/agent`, an `/admin/agents` console, and — behind `AGENTS_WHATSAPP` — the live WhatsApp chat. Agents phrase replies with Gemini when a key is set and a clean deterministic fallback otherwise (so they run fully offline).
- **RAG Knowledge Base** — ingest policies/FAQs/product docs (`/admin/knowledge`); an **offline TF-IDF retriever** grounds answers in your documents (Gemini answer-composition when a key is present). Surfaced as a `knowledge_search` agent tool.
- **MCP (Model Context Protocol) tool server** — a JSON-RPC endpoint at **`POST /mcp`** (`initialize`, `tools/list`, `tools/call`, `ping`) exposes the store's tools to external MCP clients (Claude Desktop, IDEs). High-risk tools remain approval-gated; `docs/MCP.md` has the details.
- **Voice Agent** — inbound WhatsApp voice notes are downloaded and transcribed (Gemini audio when configured), then routed through the orchestrator; graceful "please type" fallback when transcription is unavailable.
- **Human Approval Workflow** — sensitive actions (refunds, coupons, large broadcasts) are held as **approval requests** an admin reviews at `/admin/approvals` before execution; small broadcasts auto-approve under a configurable threshold. Wired directly into the tool registry so agents *and* MCP callers are both gated.
- **Visual Product Search** — carried forward from v9.0 (send a photo → similar products).

## What's New in v9.0

## What's New in v9.0

Additive over v8.0 — all prior features preserved; every new surface is guarded and feature-flagged.

- **Advanced AI Commerce** — **Visual Product Search** (send a photo on WhatsApp → visually similar products, via an offline color-histogram + perceptual-hash matcher with a Gemini-Vision upgrade path), an **AI Stylist** (complete-the-look + occasion outfit suggestions for Indian ethnic wear), a **Personal Shopping Assistant** (guided budget/occasion/colour conversation), and a **Recommendation Engine** (frequently-bought-together, trending, and personalized picks from order history). Exposed on WhatsApp and via `/api/visual-search`, `/api/recommendations/*`, `/api/stylist/*`, plus an `/admin/ai` console and a Recommendations insights page.
- **Developer Portal 2.0** — a comprehensive OpenAPI spec covering every endpoint, an enhanced `/developers` portal with curl/Python/JS code samples and webhook docs, and **per-key usage analytics** at `/admin/developer/analytics`.
- **Deep Sentry + Redis cache/HA** — Sentry with Flask **and Celery** integrations, tracing, and release tagging; a Redis **cache abstraction** supporting single-node, **Sentinel (HA failover)**, and **Cluster**, with automatic in-memory fallback; and **shared Redis-backed API rate limiting** across workers.
- **Kubernetes + Helm** — a full Helm chart (`deploy/helm/mehaat`) for web + Celery worker + beat, with Service, Ingress, HPA autoscaling, ConfigMap/Secret, PVC, PodDisruptionBudget, and an Alembic migration hook — plus raw `deploy/k8s/` manifests and `docs/KUBERNETES.md`.

## What's New in v8.0

## What's New in v8.0

Additive over v7.0 — all prior features preserved. The five enterprise-scale areas:

- **Redis + Celery background processing** — set `QUEUE_BACKEND=celery` + `REDIS_URL` and every background job (notifications, invoices, draft orders, reservations, broadcasts) runs on a Celery worker pool, with a **Celery beat** schedule for abandoned-cart recovery and shipment-tracking refresh. `run_async` transparently routes to Celery when enabled and falls back to the in-process queue otherwise. `docker-compose` ships `redis`, `worker`, and `beat` services.
- **Prometheus + Grafana + Sentry monitoring** — a Prometheus **`/metrics`** endpoint (requests, latency, 5xx, orders, payments, revenue, notifications, job queue depth), a ready-to-import **Grafana dashboard** with auto-provisioned datasource, a Prometheus scrape config, and optional **Sentry** error monitoring (`SENTRY_DSN`). Compose ships `prometheus` + `grafana`.
- **OpenAPI/Swagger Developer Portal** — a public **`/developers`** portal, interactive Swagger at **`/api/docs`**, and DB-backed **API keys** (`mh_live_…`, hashed at rest, per-key scopes + rate limits) managed at `/admin/developer`. Keys authenticate the REST API via `X-API-Key`.
- **Multi-store / multi-tenant architecture** — a `Tenant` model with resolution by WhatsApp `phone_number_id`, Shopify domain, host, or `X-Tenant` header; a request-scoped tenant context; orders tagged with `tenant_id`; and a **Stores** admin console (`/admin/tenants`) with a "view-as" switcher. Off by default (single implicit default tenant) so existing deployments are unaffected.
- **Enterprise audit logs & compliance** — a **tamper-evident audit hash chain** with an integrity verifier, **GDPR/DPDP data-subject export** and **right-to-erasure**, PII access logging, retention purge, and a **Compliance** admin console (`/admin/compliance`) with an audit-log viewer + export.

## What's New in v7.0

## What's New in v7.0

Additive over v6.1 — all prior features preserved. See **`docs/V7_ROADMAP.md`** for the full status of all 20 enterprise categories (done / foundation / planned).

- **Commerce depth** — coupon & discount engine, gift cards, bundle products, wishlist, abandoned-cart recovery, and a return/refund/exchange (RMA) workflow. Admin consoles under `/admin/promos`, `/admin/catalog`, `/admin/returns`; conversational return & support intents on WhatsApp.
- **Fulfilment & shipping** — a courier adapter (`shipping/`) with Shiprocket, Delhivery and an always-works Manual provider; shipment lifecycle, tracking, ReportLab **shipping labels + packing slips**, and pickup scheduling at `/admin/shipping`.
- **Admin & ops** — support tickets, a Settings UI, Payments and Employee dashboards, a WhatsApp **broadcast manager** (consent-aware), and GST/Sales/Inventory/Customer/Product **reports** with CSV/Excel/PDF export.
- **Security** — admin **2FA (TOTP)**, IP allowlist, and login history, on top of the existing JWT/RBAC/API-keys/audit/CSRF/rate-limiting.
- **Platform & ops** — **Dockerfile + docker-compose + GitHub Actions CI + Nginx** sample, **Alembic** migrations (alongside the boot-time auto-migrate), order **soft-delete**, optional **Sentry**, and a Prometheus **`/metrics`** endpoint.

## What's New in v6.1

## What's New in v6.1

Additive over v6.0 — all existing features preserved and backward compatible; every v6.1 capability degrades gracefully when disabled.

- **Customer CRM** — a `/admin/commerce/crm` console: customer list with lifetime value, order count and last-order date; per-customer profile with full order history, free-text notes, tags, and auto-suggested segments (new / repeat / vip).
- **Multi-user Admin Roles (RBAC)** — a `admin_users` table and `/admin/users` management console with five roles (viewer, staff, manager, admin, owner). Named users log in with their own password and role; the env `ADMIN_USERNAME`/`ADMIN_PASSWORD` remains a built-in **owner** superuser. A `role_required(...)` decorator gates privileged routes (e.g. user management requires admin/owner).
- **Background Jobs & Queue** — a durable `jobs` table plus an in-process worker pool. Order side effects (Shopify draft order, invoice, notifications, inventory reservation) run asynchronously so the webhook acks fast; jobs retry with backoff and recover after a restart. Set `JOBS_ENABLED=false` to run everything inline.
- **Inventory Reservation** — a reservation ledger that reserves stock when an order is placed, releases it on cancel/refund, and commits it on fulfilment. Optionally mirrors reservations to Shopify inventory (`INVENTORY_SYNC_ENABLED`, needs `write_inventory`).
- **REST API Documentation** — an OpenAPI 3.0 spec at `/api/openapi.json` and interactive **Swagger UI at `/api/docs`**, plus a written `docs/API_REFERENCE.md`, covering the order/tracking/payment API.

## What's New in v6.0 (Enterprise Commerce Edition)

## What's New in v6.0 (Enterprise Commerce Edition)

Everything from v5.1 is preserved and backward compatible. The whole commerce surface is additive and controlled by `COMMERCE_ENABLED` (default on); set it to `false` and the app behaves exactly like v5.1.

- **WhatsApp catalog orders** — the webhook now handles `message.type == "order"`. Each order is parsed (customer, catalog id, retailer ids, quantities, unit prices, currency, total), persisted, and assigned an internal number like `MH-2026-000001`. Meta webhook retries are de-duplicated so an order is never created twice.
- **Automatic Shopify draft orders** — a Shopify draft order is created for each catalog order (best-effort), storing the draft id, checkout and invoice URLs back on the order.
- **Customer notifications** — bilingual (Hindi/English) WhatsApp messages for order received, confirmed, payment pending, shipped, and delivered.
- **Live order tracking** — a JSON API (`GET /orders`, `GET /orders/<id>`, `POST /orders/update`, `GET /tracking/<id>`) plus a conversational path: "where is my order" / "track my order" (and Hindi equivalents) returns the latest order's status pipeline.
- **Admin Orders dashboard** — a full Orders module at `/admin/commerce/orders`: filterable/searchable table, per-order detail with a tracking timeline, one-click actions (confirm, cancel, mark packed/shipped/delivered, refund, generate invoice, generate payment link), and CSV/Excel/PDF export. Plus an **Order Analytics** page (today/monthly/pending/delivered/cancelled, revenue, AOV, conversion rate, top products/customers, sales by state/city, daily/monthly/yearly charts).
- **Payment links** — a provider-adapter system supporting **Razorpay, Stripe, Cashfree, PhonePe, and Manual UPI**, with signature-verified payment webhooks (`POST /payments/webhook/<provider>`). Manual UPI works with no gateway account; the others activate when their credentials are set.
- **PDF invoices** — professional ReportLab invoices with logo, GST/business identity, line items, discount/shipping/tax/grand total, a QR code, and a unique invoice number.
- **AI intent detection** — bilingual recognition of browse, order, track, payment, return, refund, cancel, delivery-time, invoice, coupon, stock, support, human-agent and escalation intents.
- **Enterprise data model + automatic migrations** — new `orders`, `order_items`, `payments`, `tracking`, `invoices`, `notifications`, `audit_logs`, `analytics` tables on SQLite **or** PostgreSQL, created and additively migrated on startup (no manual DB steps).
- **Security** — JWT/API-key auth on the order API, Meta webhook signature verification (v5.1), Shopify HMAC (existing), per-provider payment webhook signature checks, CSRF on admin actions, parameterized SQL throughout.
- **Tests** — 78 passing, including an end-to-end catalog-order webhook test.

## What's New in v5.1 (Production Edition)

Security and reliability hardening. Everything from v5 and earlier is preserved and backward compatible — all v5.1 additions are guarded so an existing `.env` keeps working unchanged.

- **Inbound webhook signature verification** — when `WHATSAPP_APP_SECRET` (your Meta App Secret) is set, every inbound `POST /webhook` is validated against Meta's `X-Hub-Signature-256` header; forged or unsigned calls are rejected with `403`. Left unset, verification is skipped and a warning is logged (unchanged behaviour).
- **Webhook de-duplication** — Meta re-delivers webhooks until it gets a `200`; inbound message IDs are now tracked in a bounded window so the bot never replies to the same message twice.
- **Read receipts** — inbound messages are best-effort marked as read (blue ticks).
- **Access-token encryption at rest** — Shopify OAuth tokens in the SQLite store are transparently encrypted when `TOKEN_ENCRYPTION_KEY` is set (existing plaintext tokens still read correctly).
- **PostgreSQL, genuinely ready** — Render/Heroku `postgres://` URLs are normalized to the `postgresql+psycopg2` dialect automatically, `psycopg2-binary` ships in `requirements.txt`, and the engine uses tuned connection pooling (`pool_size`, `max_overflow`, `pool_recycle`).
- **Config fixes** — `DEFAULT_SHOP_DOMAIN` now correctly pins the default shop (previously only the undocumented `SHOPIFY_DEFAULT_SHOP` worked); `create_draft_order` safely skips line items missing a `variant_id`.
- **Tests** — 35 passing (7 new for the v5.1 hardening). Two stale v4 tests were corrected.

## What's New in v4.0 (Phase 1)

- **Live product cards fix** — messages like "Show saree", "blue silk saree under 3000", "party wear" now trigger a live Shopify search and reply with real WhatsApp **product cards** (title, price, currency, availability, category, variants, short description, product URL — max 5). The static catalogue link is used only as a fallback when no products are found or Shopify is unavailable.
- **Pagination** — reply **"more"** / **"next"** to page through additional results (state kept in conversation memory).
- **Native WhatsApp catalog** — when `WHATSAPP_CATALOG_ID` is set, native Product Messages are sent, with automatic fallback to formatted text cards.
- **Enterprise foundations (opt-in, backward compatible)** — SQLAlchemy persistence layer (`USE_DATABASE`), structured JSON logging + request trace ids (`LOG_FORMAT=json`), PII masking, optional token encryption at rest (`TOKEN_ENCRYPTION_KEY`), security headers, and `/health` + `/health/live` + `/health/ready` probes.

See **`docs/V4.0_RELEASE_NOTES.md`** for the full file-by-file changelog, deployment steps, verification checklist, and rollback plan, and **`docs/V4.0_ROADMAP.md`** for the remaining enterprise phases. Everything from v3.0 below is preserved and carried forward unchanged.

## What's New in v3.0

- **Shopify OAuth 2.0** replaces the static `SHOPIFY_ACCESS_TOKEN`. New `/shopify/install` and `/shopify/callback` routes implement the full authorization-code flow with HMAC + CSRF `state` validation, and access tokens are persisted per-shop.
- **Modular architecture**: code is now split into `shopify/`, `whatsapp/`, `ai/`, `memory/`, and `utils/` packages instead of flat top-level modules.
- **Expanded Shopify features**: collections, product details, variant selection, order status lookup, draft orders, checkout/cart links, customer lookup, and smarter multi-filter product search.
- **Expanded WhatsApp features**: interactive reply buttons, list messages, formatted product cards, and order status replies, in addition to plain text.
- **Retry + timeout handling** with exponential backoff on all outbound HTTP calls (Shopify, Gemini, WhatsApp).
- **Stronger security**: HMAC validation for both the OAuth callback and Shopify webhooks, CSRF-style OAuth `state` tokens, shop-domain validation, secret redaction in logs, plus the existing input sanitization and prompt-injection detection.

All existing functionality from v2.0 (conversation memory, FAQ engine, greeting detection, language detection, rate limiting, health check, Render deployment) is preserved and carried forward.

## Project Structure

```
app.py                  Flask app: wiring, orchestration, health check
config.py               Central environment variable configuration

shopify/
    auth.py             OAuth install/callback routes + token storage
    client.py           Retrying Shopify Admin API HTTP client
    search.py           Product search, collections, variants, smart filters
    orders.py           Order status, draft orders, checkout/cart links, customers
    inventory.py        Variant/product inventory checks

whatsapp/
    webhook.py          Webhook verification (GET) + incoming events (POST)
    sender.py           Text, buttons, list messages, product cards, order replies

ai/
    gemini.py           Gemini 2.5 Flash integration (retry, parsing, fallbacks)
    prompts.py          System / business / sales / safety prompts
    faq.py              Verified FAQ answers and intent matching

memory/
    store.py            Per-customer conversation memory with expiry

utils/
    logging.py          Structured logging + execution-time decorator + redaction
    security.py         Sanitization, prompt-injection detection, HMAC, OAuth state
    ratelimit.py         Sliding-window rate limiter
    language.py         Hindi / English / Hinglish detection + greetings

requirements.txt
runtime.txt
render.yaml
.env.example
```

## 1. Installation

```bash
git clone <your-repo-url> mehaat-bot
cd mehaat-bot
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# then edit .env with your real credentials
```

Run locally:

```bash
python app.py
# or, closer to production:
gunicorn app:app --bind 0.0.0.0:5000
```

## 2. Environment Variables

| Variable | Description |
|---|---|
| `VERIFY_TOKEN` | Any string you choose; must match the token entered in Meta's webhook setup |
| `WHATSAPP_TOKEN` | Permanent access token for the WhatsApp Cloud API |
| `PHONE_NUMBER_ID` | Your WhatsApp Business phone number ID |
| `GEMINI_API_KEY` | Google Gemini API key |
| `SHOPIFY_API_KEY` | Client ID of your Shopify app (Dev Dashboard) |
| `SHOPIFY_API_SECRET` | Client secret of your Shopify app |
| `SHOPIFY_APP_URL` | Public HTTPS base URL of this deployment, e.g. `https://mehaat-bot.onrender.com` |
| `SHOPIFY_SCOPES` | Comma-separated OAuth scopes requested at install |
| `SHOPIFY_WEBHOOK_SECRET` | Secret used to verify incoming Shopify webhooks |
| `DEFAULT_SHOP_DOMAIN` | (Optional) pin a single shop for single-store deployments |
| `TOKEN_STORE_PATH` | File path for persisted OAuth tokens (point at a persistent disk in production) |

## 3. Shopify OAuth Setup (New Dev Dashboard Flow)

This app no longer uses a manually copied Admin API access token. Instead it implements
the standard OAuth authorization-code flow:

1. In the [Shopify Dev Dashboard](https://shopify.dev/), create a new app.
2. Under **API credentials**, copy the **Client ID** into `SHOPIFY_API_KEY` and the
   **Client secret** into `SHOPIFY_API_SECRET`.
3. Set the app's **Allowed redirection URL(s)** to:
   `https://<your-deployed-app>/shopify/callback`
4. Set `SHOPIFY_APP_URL` to your deployed app's base URL (no trailing slash).
5. Deploy the app (see Render steps below) so the callback URL is publicly reachable.
6. To install the app on a store, visit:
   `https://<your-deployed-app>/shopify/install?shop=<store>.myshopify.com`
   You'll be redirected to Shopify's consent screen; after approval, Shopify redirects
   back to `/shopify/callback`, which validates the request and exchanges the
   authorization code for a permanent access token.
7. The access token is stored (by default in a local JSON file at `TOKEN_STORE_PATH`,
   keyed by shop domain) and used automatically for all subsequent Admin API calls —
   no more static token in your `.env`.

### How the OAuth flow is protected

- **CSRF `state` token**: generated on `/install`, single-use, expires after 10 minutes, validated on `/callback`.
- **HMAC validation**: the callback's query string is HMAC-verified against `SHOPIFY_API_SECRET` before any code exchange happens.
- **Shop domain validation**: only well-formed `*.myshopify.com` domains are accepted, preventing open-redirect abuse.

### Multi-store vs. single-store

The token store supports multiple installed shops. For a single-store deployment (like
ME-HAAT Fashion), either install once and let the bot auto-detect the first installed
shop, or set `DEFAULT_SHOP_DOMAIN` explicitly once installation is complete.

> **Production note:** the default `ShopTokenStore` is a local JSON file — fine for a
> single Render instance with a persistent disk, but not safe across multiple instances/workers.
> For real multi-tenant or multi-instance production use, replace `ShopTokenStore` in
> `shopify/auth.py` with a database-backed implementation (the `get_token` / `set_token`
> interface is intentionally small to make that swap easy).

## 4. WhatsApp Cloud API Setup

1. Create a Meta App at [developers.facebook.com](https://developers.facebook.com) and add the **WhatsApp** product.
2. Note your **Phone Number ID** and generate a permanent **access token** (via a System User in Business Settings).
3. In the Meta App Dashboard → WhatsApp → Configuration:
   - **Callback URL**: `https://<your-deployed-app>/webhook`
   - **Verify Token**: same value as your `VERIFY_TOKEN` env var
4. Subscribe to the `messages` webhook field.
5. Interactive messages (buttons/lists) and product cards work out of the box via `whatsapp/sender.py` — no extra Meta configuration needed beyond standard messaging permissions.

## 5. Gemini Setup

1. Get an API key from [Google AI Studio](https://aistudio.google.com/apikey).
2. Set it as `GEMINI_API_KEY`. The bot uses the `gemini-2.5-flash` model via the `generateContent` REST endpoint, with retry/backoff and quota-aware fallback messaging.

## 6. Deploying to Render

This repo includes a ready-to-use `render.yaml` (with a persistent disk for the OAuth token store):

1. Push this repository to GitHub.
2. In the Render dashboard, choose **New → Blueprint** and point it at your repo (Render will read `render.yaml`).
3. Fill in the environment variable values when prompted (marked `sync: false`, so Render asks for them securely).
4. Deploy. Render installs dependencies from `requirements.txt` (Python version pinned via `runtime.txt` / `PYTHON_VERSION`) and starts the app with Gunicorn.
5. Use the deployed URL's `/webhook` path for the WhatsApp Cloud API callback, and `/shopify/callback` as your Shopify app's redirect URL.
6. Complete the Shopify install flow once via `/shopify/install?shop=<store>.myshopify.com`.
7. `/health` reports service status and how many shops are currently connected.

## 7. Security Notes

- Never commit `.env` or the token store file — only commit `.env.example`.
- All secrets are read from environment variables via `config.py`, never hardcoded.
- OAuth callback requests are HMAC-verified and CSRF-state-checked before any token exchange.
- Shopify webhook payloads can be verified with `utils.security.verify_shopify_webhook_hmac` using `SHOPIFY_WEBHOOK_SECRET` if you add webhook endpoints (e.g. `app/uninstalled`) beyond the OAuth flow.
- The Gemini system prompt explicitly forbids revealing prompts, API keys, or environment variables; input is sanitized against common prompt-injection phrasing, and detected attempts are flagged in the AI's grounding context rather than acted upon.
- Sensitive values are redacted before being written to logs (see `utils/logging.py::redact`).
- Two independent rate limiters protect WhatsApp message throughput and Gemini API usage per customer.

## 8. Extending

- Add Shopify webhook endpoints (e.g. `orders/updated`, `app/uninstalled`) using `verify_shopify_webhook_hmac` for validation, and call `token_store.remove_token(shop)` on uninstall.
- Swap `memory/store.py` and `shopify/auth.py`'s `ShopTokenStore` for a database (Postgres/Redis) for multi-worker or multi-instance deployments.
- Extend `ai/faq.py` with more verified answers as your policies evolve.
- Extend `shopify/search.py` filters (e.g. size, discount %) — keep all product facts sourced from Shopify, never from the LLM.
- Wire `whatsapp/sender.py::send_button_message` / `send_list_message` into the orchestration flow in `app.py` for richer guided shopping menus (e.g. "Browse by Category", "Track My Order").
