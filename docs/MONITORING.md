# Monitoring — ME-HAAT Fashion AI Bot v8.0

This document describes the observability stack: how the app exposes metrics,
how Prometheus scrapes them, how Grafana renders dashboards, and how Sentry
captures errors.

## Overview

```
                 scrape /metrics (15s)
  ┌──────────┐  ───────────────────────▶  ┌────────────┐   query   ┌─────────┐
  │  Flask   │                             │ Prometheus │ ◀──────── │ Grafana │
  │  app     │  ── errors ──▶  Sentry      │  :9090     │           │  :3000  │
  │  :5000   │                             └────────────┘           └─────────┘
  └──────────┘
```

- **App** exposes Prometheus text-format metrics at `GET /metrics`
  (see `utils/observability.py`). No `prometheus-client` dependency — the
  exposition is hand-rolled.
- **Prometheus** scrapes `app:5000/metrics` every 15s (`deploy/prometheus.yml`).
- **Grafana** auto-provisions the Prometheus datasource and the overview
  dashboard on startup (`deploy/grafana/provisioning/`).
- **Sentry** captures exceptions when `SENTRY_DSN` is set and `sentry-sdk` is
  installed; otherwise it's a silent no-op.

## The `/metrics` endpoint

The app renders Prometheus-format metrics. Exported series:

| Metric | Type | Meaning |
| --- | --- | --- |
| `mehaat_uptime_seconds` | gauge | Process uptime in seconds. |
| `mehaat_http_requests_total` | counter | Total HTTP requests handled. |
| `mehaat_http_5xx_total` | counter | Total HTTP 5xx responses. |
| `mehaat_http_request_duration_seconds_count` | counter | Number of timed requests (summary count). |
| `mehaat_http_request_duration_seconds_sum` | counter | Cumulative request duration in seconds (summary sum). |
| `mehaat_orders_total` | gauge | Total orders in the database. |
| `mehaat_jobs_queued` | gauge | Jobs currently in the `queued` state. |
| `mehaat_jobs_failed` | gauge | Jobs currently in the `failed` state. |
| `mehaat_payments_total` | counter | Total successful payments processed. |
| `mehaat_revenue_total` | counter | Cumulative revenue (in your store currency). |
| `mehaat_notifications_total` | counter | Total outbound notifications sent (WhatsApp/email). |

> Avg latency is derived in Grafana as
> `rate(mehaat_http_request_duration_seconds_sum[5m]) / rate(mehaat_http_request_duration_seconds_count[5m])`.

The `_total`, `_payments_total`, `_revenue_total`, `_notifications_total`, and
the `_request_duration_seconds_*` summary series are added in v8.0.

## Running the stack

The monitoring services run alongside the app via `docker-compose`. Add these
two services (and a volume) to `docker-compose.yml`:

```yaml
  prometheus:
    image: prom/prometheus:latest
    ports:
      - "9090:9090"
    volumes:
      - ./deploy/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus-data:/prometheus
    restart: unless-stopped
    depends_on:
      - app

  grafana:
    image: grafana/grafana:latest
    ports:
      - "3000:3000"
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:-admin}
      GF_USERS_ALLOW_SIGN_UP: "false"
    volumes:
      - ./deploy/grafana/provisioning:/etc/grafana/provisioning:ro
      - ./deploy/grafana/dashboards:/var/lib/grafana/dashboards:ro
      - grafana-data:/var/lib/grafana
    restart: unless-stopped
    depends_on:
      - prometheus

volumes:
  prometheus-data:
  grafana-data:
```

Then:

```bash
cp .env.example .env    # set SENTRY_DSN, GRAFANA_ADMIN_PASSWORD, etc.
docker compose up --build
```

- Prometheus UI: <http://localhost:9090> (check *Status → Targets* — `mehaat`
  should be `UP`).
- Grafana: <http://localhost:3000> (login `admin` / your
  `GRAFANA_ADMIN_PASSWORD`). The **ME-HAAT → Overview** dashboard is
  auto-loaded.

### Provisioning layout

| File | Purpose |
| --- | --- |
| `deploy/prometheus.yml` | Scrape config (job `mehaat` → `app:5000/metrics`). |
| `deploy/grafana/provisioning/datasources/datasource.yml` | Prometheus datasource (default). |
| `deploy/grafana/provisioning/dashboards/dashboards.yml` | Dashboard file provider. |
| `deploy/grafana/dashboards/mehaat-overview.json` | The overview dashboard. |

If the app runs on the Docker host instead of as a compose service, switch the
Prometheus target to the commented `host.docker.internal:5000` in
`deploy/prometheus.yml`.

## Securing `/metrics`

`/metrics` exposes operational internals and must not be public. The sample
reverse proxy (`deploy/nginx.conf`) already restricts it with an allowlist:

```nginx
location /metrics {
    allow 10.0.0.0/8;   # private / container network
    allow 127.0.0.1;    # localhost
    deny all;
    proxy_pass http://mehaat_app;
}
```

Within docker-compose, Prometheus scrapes the app directly on the internal
network, so the endpoint never needs to traverse the public-facing Nginx.
Keep it behind the allowlist (or a VPN / firewall rule) in production.

## Sentry

Set `SENTRY_DSN` in `.env` to enable error monitoring (see
`utils/observability.py::init_sentry`). When unset or when `sentry-sdk` is not
installed, initialization is skipped and the app runs normally. The release is
tagged `mehaat@<version>` and traces sample at 10%.
