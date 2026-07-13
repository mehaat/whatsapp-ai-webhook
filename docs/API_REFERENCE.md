# ME-HAAT Fashion Commerce API — Reference (v6.1)

JSON API for orders, tracking and payment webhooks in the ME-HAAT Fashion AI
Bot.

- **Base URL:** `/` (the API blueprint registers absolute paths).
- **Interactive docs:** `GET /api/docs` (Swagger UI).
- **Machine-readable spec:** `GET /api/openapi.json` (OpenAPI 3.0.3).
- **Content type:** all request and response bodies are `application/json`.

## Authentication

Protected endpoints accept **either** of two credentials:

1. **Bearer JWT** — obtained from `POST /api/token`, sent as
   `Authorization: Bearer <token>`. Tokens are HS256-signed and expire after
   `expires_in` seconds.
2. **API key** — a static key sent as the `X-API-Key: <key>` header.

Public endpoints (`GET /tracking/<ref>` and `POST /payments/webhook/<provider>`)
require **no** authentication. Everything else does.

### Get a token

```bash
curl -X POST https://your-host/api/token \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "s3cret"}'
```

```json
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "expires_in": 3600
}
```

Then call a protected endpoint:

```bash
# With the bearer token
curl https://your-host/orders -H "Authorization: Bearer $TOKEN"

# Or with the API key
curl https://your-host/orders -H "X-API-Key: $API_KEY"
```

Error responses share a single shape:

```json
{ "error": "invalid_credentials" }
```

---

## Endpoints

### POST /api/token — issue a bearer JWT

Public. Exchange admin credentials for a JWT.

**Body**

| Field      | Type   | Required | Notes            |
|------------|--------|----------|------------------|
| `username` | string | yes      | Admin username.  |
| `password` | string | yes      | Admin password.  |

**Responses:** `200` `{token, expires_in}` · `401` invalid credentials ·
`503` token signing unavailable.

---

### GET /orders — list orders

Auth required. Returns a filtered, paginated page of orders plus the total
matching count.

**Query parameters**

| Param            | Type    | Default | Notes                                              |
|------------------|---------|---------|----------------------------------------------------|
| `status`         | string  | —       | Filter by fulfilment status.                       |
| `payment_status` | string  | —       | Filter by payment status.                          |
| `q`              | string  | —       | Free text over order_number, wa_number, customer.  |
| `date_from`      | date    | —       | Inclusive lower bound, `YYYY-MM-DD`.               |
| `date_to`        | date    | —       | Inclusive upper bound, `YYYY-MM-DD`.               |
| `limit`          | integer | 50      | 1–200.                                             |
| `offset`         | integer | 0       | Pagination offset.                                 |

**Example**

```bash
curl "https://your-host/orders?status=shipped&limit=20" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "count": 137,
  "results": [
    {
      "id": 1001,
      "order_number": "MH-2026-0001",
      "customer_name": "Aditi Sharma",
      "currency": "INR",
      "total_amount": 2826.9,
      "status": "shipped",
      "payment_status": "paid",
      "created_at": "2026-07-10T11:05:00+00:00"
    }
  ]
}
```

List results omit `items` and `tracking`; fetch a single order for those.

---

### GET /orders/&lt;ref&gt; — fetch one order

Auth required. `ref` is either an all-digit primary key **or** an
`order_number` string. Returns the full order including `items` and
`tracking`.

**Example**

```bash
curl https://your-host/orders/MH-2026-0001 \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "id": 1001,
  "order_number": "MH-2026-0001",
  "customer_name": "Aditi Sharma",
  "currency": "INR",
  "subtotal": 2598.0,
  "discount": 0.0,
  "shipping": 99.0,
  "tax": 129.9,
  "total_amount": 2826.9,
  "status": "shipped",
  "payment_status": "paid",
  "courier": "Delhivery",
  "tracking_number": "DLV123456789",
  "items": [
    {
      "id": 42,
      "product_name": "Hand-block Cotton Kurta",
      "variant": "M / Indigo",
      "quantity": 2,
      "unit_price": 1299.0,
      "currency": "INR",
      "line_total": 2598.0
    }
  ],
  "tracking": [
    {
      "id": 7,
      "status": "shipped",
      "courier": "Delhivery",
      "tracking_number": "DLV123456789",
      "location": "Jaipur Hub",
      "note": "Picked up by courier",
      "created_at": "2026-07-13T09:30:00+00:00"
    }
  ]
}
```

**Responses:** `200` order · `404` not found.

---

### POST /orders/update — update an order

Auth required. Drives a status transition and/or updates editable fields. The
body must include `order` (id or order_number). A `status` value records a
tracking event; the tracking-carrying fields (`courier`, `tracking_number`,
`location`, `note`) apply to that event.

**Body**

| Field             | Type   | Notes                                                  |
|-------------------|--------|--------------------------------------------------------|
| `order`           | string | **Required.** Order id or order_number.                |
| `status`          | string | New fulfilment status (records a tracking event).      |
| `courier`         | string | Courier name.                                          |
| `tracking_number` | string | Courier tracking number.                               |
| `location`        | string | Tracking event location.                               |
| `note`            | string | Tracking event note.                                   |
| `customer_name`   | string | Editable field.                                        |
| `city`            | string | Editable field.                                        |
| `state`           | string | Editable field.                                        |
| `notes`           | string | Order notes.                                           |
| `discount`        | number | Recomputes `total_amount`.                             |
| `shipping`        | number | Recomputes `total_amount`.                             |
| `tax`             | number | Recomputes `total_amount`.                             |

**Example**

```bash
curl -X POST https://your-host/orders/update \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "order": "MH-2026-0001",
        "status": "shipped",
        "courier": "Delhivery",
        "tracking_number": "DLV123456789",
        "location": "Jaipur Hub"
      }'
```

Returns the refreshed order (same shape as `GET /orders/<ref>`).

**Responses:** `200` order · `400` missing order ref · `404` not found.

---

### GET /tracking/&lt;ref&gt; — public tracking

**Public — no auth.** Resolves `ref` by id or order_number and returns a
tracking summary with the canonical stage pipeline and raw events.

**Example**

```bash
curl https://your-host/tracking/MH-2026-0001
```

```json
{
  "order_number": "MH-2026-0001",
  "status": "shipped",
  "payment_status": "paid",
  "courier": "Delhivery",
  "tracking_number": "DLV123456789",
  "stages": [
    { "stage": "received",         "state": "done" },
    { "stage": "confirmed",        "state": "done" },
    { "stage": "packed",           "state": "done" },
    { "stage": "shipped",          "state": "current" },
    { "stage": "out_for_delivery", "state": "pending" },
    { "stage": "delivered",        "state": "pending" }
  ],
  "tracking": [
    {
      "id": 7,
      "status": "shipped",
      "courier": "Delhivery",
      "tracking_number": "DLV123456789",
      "location": "Jaipur Hub",
      "created_at": "2026-07-13T09:30:00+00:00"
    }
  ]
}
```

#### Tracking stages

The pipeline is fixed and ordered:

`received → confirmed → packed → shipped → out_for_delivery → delivered`

Each stage carries a `state`:

- `done` — the stage precedes the live status, or appears in the event history.
- `current` — the stage equals the order's live status.
- `pending` — not yet reached.

**Responses:** `200` summary · `404` not found.

---

### POST /payments/webhook/&lt;provider&gt; — provider webhook

**Public — no auth** (providers cannot present our JWT). `provider` is one of
`razorpay`, `stripe`, `cashfree`, `phonepe`, `manual_upi`. Each provider
adapter verifies its own signature from the raw body and headers.

The endpoint **always** responds with HTTP `200` — even on verification
failure — so the provider does not enter a retry storm. Failures are logged.

**Example**

```bash
curl -X POST https://your-host/payments/webhook/razorpay \
  -H "Content-Type: application/json" \
  -H "X-Razorpay-Signature: <sig>" \
  --data-binary @payload.json
```

```json
{ "ok": true, "status": "paid" }
```

**Contract:** send the provider's raw payload and signature headers; expect
`{ "ok": <bool>, "status": <string> }` with status `200`.

---

## Order schema

Returned by the order and tracking endpoints (fields mirror the service
serializer).

| Field                    | Type            | Notes                                               |
|--------------------------|-----------------|-----------------------------------------------------|
| `id`                     | integer         | Primary key.                                        |
| `order_number`           | string          | Human-facing reference, e.g. `MH-2026-0001`.        |
| `wa_number`              | string \| null  | Customer WhatsApp number.                           |
| `customer_name`          | string \| null  | Customer name.                                      |
| `language`               | string \| null  | Preferred language code.                            |
| `wa_order_id`            | string \| null  | Source WhatsApp order id.                           |
| `catalog_id`             | string \| null  | WhatsApp catalog id.                                |
| `currency`               | string          | ISO currency, e.g. `INR`.                           |
| `subtotal`               | number          | Sum of line totals.                                 |
| `discount`               | number          | Discount applied.                                   |
| `shipping`               | number          | Shipping charge.                                    |
| `tax`                    | number          | Tax amount.                                         |
| `total_amount`           | number          | `subtotal - discount + shipping + tax`.             |
| `status`                 | string          | Fulfilment status (see stages).                     |
| `payment_status`         | string          | `pending`, `paid`, `failed`, `refunded`.            |
| `fulfillment_status`     | string          | Internal fulfilment state.                          |
| `shopify_draft_order_id` | string \| null  | Shopify draft order id.                             |
| `shopify_order_id`       | string \| null  | Shopify order id.                                   |
| `checkout_url`           | string \| null  | Hosted checkout URL.                                |
| `invoice_url`            | string \| null  | Invoice URL.                                        |
| `courier`                | string \| null  | Courier name.                                       |
| `tracking_number`        | string \| null  | Courier tracking number.                            |
| `city`                   | string \| null  | Shipping city.                                      |
| `state`                  | string \| null  | Shipping state.                                     |
| `notes`                  | string \| null  | Free-text notes.                                    |
| `created_at`             | date-time \| null | ISO-8601 creation timestamp.                      |
| `updated_at`             | date-time \| null | ISO-8601 update timestamp.                        |
| `items`                  | array           | `OrderItem[]` — present when items are included.    |
| `tracking`               | array           | `TrackingEvent[]` — present when included.          |

### OrderItem

| Field                 | Type           | Notes                          |
|-----------------------|----------------|--------------------------------|
| `id`                  | integer        | Line item id.                  |
| `product_retailer_id` | string \| null | Retailer SKU.                  |
| `product_id`          | string \| null | Product id.                    |
| `variant_id`          | string \| null | Variant id.                    |
| `product_name`        | string         | Product name.                  |
| `variant`             | string \| null | Variant label.                 |
| `quantity`            | integer        | Quantity ordered.              |
| `unit_price`          | number         | Price per unit.                |
| `currency`            | string         | ISO currency.                  |
| `line_total`          | number         | `unit_price * quantity`.       |

### TrackingEvent

| Field             | Type              | Notes                          |
|-------------------|-------------------|--------------------------------|
| `id`              | integer           | Event id.                      |
| `status`          | string            | Status at this event.          |
| `courier`         | string \| null    | Courier name.                  |
| `tracking_number` | string \| null    | Courier tracking number.       |
| `location`        | string \| null    | Event location.                |
| `note`            | string \| null    | Event note.                    |
| `created_at`      | date-time \| null | ISO-8601 timestamp.            |

### Error

```json
{ "error": "not_found" }
```

Common codes: `invalid_credentials` (401), `missing_order_ref` (400),
`not_found` (404), `token_unavailable` (503), `internal_error` (500).
