# Deployment Guide — Render Free + Neon PostgreSQL

This guide deploys **ME-HAAT Fashion AI Bot** on **Render Free** with **Neon
PostgreSQL** as the durable database. No persistent disk is required: all state
(Shopify OAuth tokens, admin dashboard data, commerce orders) lives in Neon.

The backend is chosen entirely by the `DATABASE_URL` environment variable — the
same codebase runs on local SQLite and on Neon Postgres with **no code
changes**.

---

## 1. Create the Neon database

1. Sign in at <https://neon.tech> and create a project (choose a region close to
   your Render region, e.g. AWS Singapore / US-East).
2. Open **Dashboard → Connection Details** and copy the **connection string**.
   It looks like:

   ```
   postgresql://user:password@ep-cool-name-123456.us-east-2.aws.neon.tech/neondb?sslmode=require
   ```

   * Keep the `?sslmode=require` suffix — Neon requires TLS.
   * Either the direct host or the **pooled** host (`…-pooler.…`) works. The
     pooled host is recommended for many short connections.
3. You do **not** need to create tables by hand — migrations do that (step 4).

> The app auto-normalizes a bare `postgres://` / `postgresql://` URL to the
> `postgresql+psycopg` (psycopg 3) dialect, so you can paste Neon's string
> verbatim.

---

## 2. Configure environment variables on Render

In the Render service **Environment** tab (or via `render.yaml`, which is already
wired), set at minimum:

| Variable | Value |
|---|---|
| `DATABASE_URL` | *your Neon connection string from step 1* |
| `VERIFY_TOKEN` | your WhatsApp webhook verify token |
| `WHATSAPP_TOKEN` | WhatsApp Cloud API token |
| `PHONE_NUMBER_ID` | WhatsApp phone number id |
| `GEMINI_API_KEY` | Google Gemini key |
| `SHOPIFY_API_KEY` / `SHOPIFY_API_SECRET` | Shopify app credentials |
| `SHOPIFY_APP_URL` | your public Render URL, e.g. `https://mehaat.onrender.com` |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | admin dashboard login |
| `ADMIN_SECRET_KEY` | a strong random string (session signing) |
| `TOKEN_ENCRYPTION_KEY` | *(optional)* Fernet key to encrypt tokens at rest |

`render.yaml` already lists every variable; secrets are marked `sync: false`, so
Render prompts you for them on first deploy. `DATABASE_URL` is `sync: false` and
**required**.

> There is intentionally **no `disk:` block** — Render Free has no persistent
> disk, and none is needed because Neon holds all durable state.

---

## 3. Build & start commands

`render.yaml` sets:

```yaml
buildCommand: pip install -r requirements.txt
startCommand: alembic upgrade head && gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 30
```

* `alembic upgrade head` creates/updates the schema on Neon before the web
  process starts. It is idempotent and safe to run on every deploy.
* One Gunicorn worker fits the Free tier. Because all state is in Neon (not
  worker memory), you can safely scale to more workers on a paid plan without
  the historical multi-worker OAuth-state bug.

---

## 4. First deploy

1. Push this branch and connect the repo in Render (**New → Blueprint** if using
   `render.yaml`, otherwise **New → Web Service**).
2. Render runs the build, then `alembic upgrade head` (creating all tables on
   Neon), then boots Gunicorn.
3. Watch the logs for:
   * `DATABASE | Engine initialised | backend=postgresql driver=psycopg`
   * `alembic.runtime.migration | Running upgrade … initial unified schema`
   * `OAUTH_DB | Token store validated …`

---

## 5. Verify

* **Health:** open `https://<your-app>/health`. Expect `"status":"ok"` and
  `"database": { "backend": "postgresql", "integrity": "ok", "reachable": true }`.
* **Admin dashboard:** open `/admin` and log in with `ADMIN_USERNAME` /
  `ADMIN_PASSWORD`.
* **Shopify install:** visit
  `/shopify/install?shop=<your-store>.myshopify.com` and complete OAuth. After a
  redeploy, `/health` should still show your installed shop
  (`oauth.token_count ≥ 1`) — proving persistence survives restarts.

---

## 6. Bringing existing data across (optional)

If you already have a populated local/production `mehaat.db` (SQLite) and want to
keep that data, run the one-off copy tool **once** (see `MIGRATION_GUIDE.md`):

```bash
export DATABASE_URL='postgresql://…neon…?sslmode=require'
python scripts/migrate_sqlite_to_postgres.py --source /path/to/mehaat.db
```

---

## 7. Notes on ephemeral files

Generated PDFs (invoices under `INVOICE_OUTPUT_DIR`, exports under
`COMPLIANCE_EXPORT_DIR`) and logs (`LOG_DIR`) are written to `/tmp`, which is
**ephemeral** on Render Free — they do not survive a restart. This is fine for
on-demand generation; if you need durable documents, offload them to object
storage (e.g. S3/R2). The **database** is always durable (Neon).

---

## 8. Local development (unchanged)

With no `DATABASE_URL` (or a `sqlite:///…` one), the app uses a local SQLite
file — identical behaviour to before, no Postgres required:

```bash
pip install -r requirements.txt
# DATABASE_URL unset -> sqlite:///mehaat.db
python app.py           # or: gunicorn app:app
```
