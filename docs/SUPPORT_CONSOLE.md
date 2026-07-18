# v10.2 — Real-time WhatsApp Support Console

Turns the Admin Dashboard into a live customer-support console with **manual
live reply**. Fully additive: no existing feature, route, table or API is
changed. Works on SQLite and PostgreSQL, deploys on Render Free.

Open it at **`/admin/support/console`** (nav link: *Support Console*).

## What it does

- **Live inbox** — every conversation with name, number, last message, last
  seen, status, unread count. AI/Manual + assignment pills. Polls every 3s.
- **Live chat** — WhatsApp-style merged timeline (customer left, bot/admin
  right), day separators, times, delivery/read ticks.
- **Manual reply** — text, image, PDF and **voice** (uploaded to Meta `/media`
  then sent by `media_id`). Emoji picker, attachment button, Enter-to-send.
- **AI toggle per conversation** — turning AI **off** puts the chat in *Manual
  Mode*: the webhook handler stops the bot from auto-replying (the inbound is
  still recorded so it shows live) and the admin drives the conversation.
- **Assignment** — assign a conversation to an admin ("assign to me").
- **Internal notes** — admin-only, never sent to the customer.
- **Customer profile** — phone, name, language, order count, last order, total
  spend, conversation/message counts.
- **Shopify in chat** — product search, send product cards, create draft order.
- **Payment** — generate a payment link (existing providers) and optionally
  send it to the customer on WhatsApp; copy link.
- **Live stats** — customers online, pending replies, today's messages/orders,
  AI replies, manual replies.
- **Notifications** — browser notification + sound + unread nav badge.
- **UI** — WhatsApp-Business styling, light/dark mode, responsive, animations.

## Architecture

| Layer | File |
|---|---|
| ORM tables (additive) | `database/models_support.py` |
| Migration | `alembic/versions/*_v10_2_support_console_tables.py` |
| Service (data/logic) | `admin/support_console.py` |
| HTTP blueprint (`/admin/support`) | `admin/support_routes.py` |
| WhatsApp media send (returns wamid) | `whatsapp/support_sender.py` |
| Manual-mode gate | `app.py` (after `record_inbound`) |
| Delivery/read receipts | `whatsapp/webhook.py` (`_process_status_updates`) |
| Frontend | `admin/templates/admin/support_console.html`, `admin/static/support_console.{css,js}` |

New tables: `conversation_settings` (AI toggle + status), `admin_messages`
(console sends), `conversation_assignments`, `internal_notes`, `message_status`.

## Real-time model

Polling every ~3s (`/api/inbox`, `/api/stats`, and `/api/thread/<wa>` when a
chat is open). This is deliberate: it needs no async worker and works on the
Render Free sync Gunicorn worker. Delivery/read ticks come from WhatsApp status
webhooks persisted to `message_status`.

## Security

Every route requires an authenticated admin (`login_required`). Every
state-changing route enforces CSRF (`csrf_protect`), a per-admin rate limit,
input validation (E.164 number check, upload size/type limits), and writes a
tamper-evident audit row (`audit_logs`). Uploads capped at 16 MB with a MIME
allowlist.

## APIs (all under `/admin/support/api`)

`GET inbox`, `GET thread/<wa>`, `GET profile/<wa>`, `GET stats`,
`GET/POST notes/<wa>`, `POST send/<wa>` (text or multipart media),
`POST ai-toggle/<wa>`, `POST assign/<wa>`, `POST status/<wa>`,
`GET shopify/search`, `POST shopify/send-card/<wa>`,
`POST shopify/draft-order/<wa>`, `POST payment/link/<wa>`.

## Deploy notes

No new dependencies and no deployment change beyond running the migration
(`alembic upgrade head`, already in `render.yaml`'s start command). Manual-mode
state, notes, assignments and admin messages persist in the database, so they
survive restarts and are shared across workers.
