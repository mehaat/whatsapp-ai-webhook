# Kubernetes & Helm Deployment — ME-HAAT Fashion AI Bot v9.0

This guide covers deploying the bot to Kubernetes, either with the Helm chart at
`deploy/helm/mehaat/` (recommended) or the raw manifests at `deploy/k8s/`.

## Architecture

| Component | Command | Purpose |
|-----------|---------|---------|
| **web** | `gunicorn app:app --bind 0.0.0.0:5000` | Flask HTTP API + admin + webhooks (port 5000) |
| **worker** | `celery -A celery_app.celery_app worker -Q mehaat` | Background jobs (needs `QUEUE_BACKEND=celery` + `REDIS_URL`) |
| **beat** | `celery -A celery_app.celery_app beat` | Periodic scheduler (singleton) |

Health: `/health/live` (liveness), `/health/ready` (readiness). Metrics: `/metrics`
(keep off the public ingress — see below). Persistent data (SQLite, invoices,
token store) lives in `/var/data`.

## 1. Build & push the image

```bash
export IMAGE=ghcr.io/me-haat/mehaat-fashion-ai-bot:9.0
docker build -t "$IMAGE" .
docker push "$IMAGE"
```

Point `image.repository` / `image.tag` (Helm) or the `image:` fields (raw
manifests) at your registry. For a private registry add a pull secret and set
`imagePullSecrets`.

## 2. Install with Helm

```bash
# Create your own values file (copy the block below) — never commit real secrets.
helm install mehaat deploy/helm/mehaat -n mehaat --create-namespace -f my-values.yaml
```

Upgrade / rollback:

```bash
helm upgrade mehaat deploy/helm/mehaat -n mehaat -f my-values.yaml
helm rollback mehaat -n mehaat
```

### Minimal `my-values.yaml`

```yaml
image:
  repository: ghcr.io/me-haat/mehaat-fashion-ai-bot
  tag: "9.0"

replicaCount: 2

worker:
  enabled: true
  replicas: 2
beat:
  enabled: true

autoscaling:
  enabled: true
  minReplicas: 2
  maxReplicas: 6
  targetCPUUtilizationPercentage: 70

ingress:
  enabled: true
  className: nginx
  hosts:
    - host: mehaat.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: mehaat-tls
      hosts:
        - mehaat.example.com

# Bring-your-own managed Redis / Postgres:
redis:
  enabled: false
  externalRedisUrl: "redis://my-redis:6379/0"
postgresql:
  enabled: false
  externalDatabaseUrl: "postgresql+psycopg2://mehaat:pass@my-pg:5432/mehaat"

migrations:
  enabled: true

secretEnv:
  WHATSAPP_TOKEN: "..."
  WHATSAPP_APP_SECRET: "..."
  VERIFY_TOKEN: "..."
  GEMINI_API_KEY: "..."
  SHOPIFY_API_KEY: "..."
  SHOPIFY_API_SECRET: "..."
  SHOPIFY_WEBHOOK_SECRET: "..."
  JWT_SECRET: "..."
  ADMIN_USERNAME: "admin"
  ADMIN_PASSWORD: "..."
  ADMIN_SECRET_KEY: "..."
```

## 3. Secrets management

Non-secret config → `values.yaml` `env:` → rendered into a **ConfigMap**.
Sensitive keys → `secretEnv:` → rendered into an **Opaque Secret** (`stringData`).
Both are mounted into every pod via `envFrom`.

Options that avoid putting secrets in a values file:

```bash
# Inline overrides:
helm install mehaat deploy/helm/mehaat -n mehaat \
  --set-string secretEnv.WHATSAPP_TOKEN=... \
  --set-string secretEnv.GEMINI_API_KEY=...

# Or reference a pre-created / externally-managed Secret:
#   values.yaml:  extraEnvFromSecrets: ["my-sealed-secret"]
```

For GitOps use Sealed Secrets, External Secrets Operator, or SOPS — never commit
plaintext. The raw-manifest path ships `deploy/k8s/secret.example.yaml`; prefer
`kubectl create secret generic mehaat-secret --from-literal=...` over committing it.

## 4. Scaling

- **Web (HTTP):** set `autoscaling.enabled=true` — an HPA scales the web
  Deployment on CPU (`targetCPUUtilizationPercentage`). When autoscaling is on,
  `replicaCount` is ignored. Requires the metrics-server.
- **Workers:** scale `worker.replicas` (or add a KEDA ScaledObject on Redis queue
  depth). Beat stays at exactly 1 replica (Recreate strategy) to avoid duplicate
  scheduled jobs.
- **PodDisruptionBudget:** enable `podDisruptionBudget.enabled` with `minAvailable`
  to protect web availability during node drains.

> Gunicorn is pinned to `workers: 1` by default because the in-process rate
> limiters / job queue assume a single process. With `QUEUE_BACKEND=celery` and a
> Postgres `DATABASE_URL`, the web tier is horizontally scalable across pods.

## 5. Redis & Postgres — in-cluster vs managed

- **Managed (recommended for prod):** `redis.enabled=false` +
  `redis.externalRedisUrl`, and `postgresql.enabled=false` +
  `postgresql.externalDatabaseUrl`. These are injected as `REDIS_URL` /
  `DATABASE_URL`. (You can equivalently set `secretEnv.REDIS_URL` /
  `secretEnv.DATABASE_URL`.)
- **In-cluster:** add the Bitnami subcharts alongside this release, e.g.

  ```bash
  helm install mehaat-redis bitnami/redis -n mehaat
  helm install mehaat-pg    bitnami/postgresql -n mehaat
  ```

  then point `externalRedisUrl` / `externalDatabaseUrl` at the resulting
  Services (`redis://mehaat-redis-master:6379/0`,
  `postgresql+psycopg2://...@mehaat-pg-postgresql:5432/mehaat`). The
  `redis.enabled` / `postgresql.enabled` flags are bitnami-style toggles reserved
  for wiring those subcharts as dependencies.

With no Redis and `QUEUE_BACKEND=inprocess`, the app runs single-pod on SQLite in
`/var/data` — fine for a smoke test, not for HA.

## 6. Database migrations (Alembic)

The chart ships a migration hook. Set `migrations.enabled=true` and a Helm
`pre-install,pre-upgrade` **Job** runs `alembic upgrade head` (from `migrations.command`)
before the web pods roll:

```bash
kubectl logs -n mehaat job/mehaat-migrate
```

Alternatives:

- **initContainer:** run `alembic upgrade head` in an initContainer on the web
  Deployment (simple, but runs on every pod start).
- **Manual:** `kubectl exec -n mehaat deploy/mehaat-web -- alembic upgrade head`.

Migrations need `DATABASE_URL` to point at Postgres; on SQLite in `/var/data` the
app auto-creates tables and Alembic is optional.

## 7. Keeping `/metrics` internal

The `/metrics` Prometheus endpoint should not be exposed publicly. The Helm
ingress template carries a commented nginx `server-snippet` example, and the raw
`deploy/k8s/ingress.yaml` includes a working one:

```yaml
nginx.ingress.kubernetes.io/server-snippet: |
  location = /metrics { deny all; return 403; }
```

Scrape it in-cluster via the Service (`mehaat:80/metrics`) with a Prometheus
`ServiceMonitor`/`PodMonitor` instead.

## 8. Raw-manifest alternative (kubectl)

No Helm? Apply the plain manifests:

```bash
kubectl apply -f deploy/k8s/namespace.yaml
kubectl -n mehaat create secret generic mehaat-secret --from-literal=WHATSAPP_TOKEN=... # etc
kubectl apply -f deploy/k8s/configmap.yaml
kubectl apply -f deploy/k8s/deployment-web.yaml
kubectl apply -f deploy/k8s/deployment-worker.yaml
kubectl apply -f deploy/k8s/service.yaml
kubectl apply -f deploy/k8s/ingress.yaml
kubectl apply -f deploy/k8s/hpa.yaml
```

`deploy/k8s/secret.example.yaml` documents the expected keys — copy it, fill real
values, and keep it out of git.

## 9. Verify

```bash
kubectl get pods -n mehaat
kubectl exec -n mehaat deploy/mehaat-web -- curl -fsS http://localhost:5000/health/ready
```
