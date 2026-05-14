# AutoTest Helm chart (skeleton)

This chart deploys AutoTest v1.1 on Kubernetes. **Status: scaffold.**
The backend Deployment + Service + PDB + Secret + chart helpers are
production-ready; the celery, frontend, apisix, postgres, valkey,
seaweedfs, and ingress templates are tracked as future work in the
chart's `templates/TODO.md`.

## Quick start (kind / minikube)

```sh
# 1) Build local images and push to your registry, OR
#    --set global.imageRegistry=ghcr.io/your-org and pin a real tag.
helm install autotest ./deploy/helm/autotest \
  --set postgres.password=$(openssl rand -hex 16) \
  --set secrets.jwtSecret=$(openssl rand -hex 32) \
  --set secrets.fernetKey=$(python -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())') \
  --set secrets.minioRootPassword=$(openssl rand -hex 16)
```

## Production checklist

* `postgres.external: true` pointing at managed Postgres (RDS / CloudSQL)
* `valkey.external: true` pointing at managed Redis (ElastiCache)
* `secrets.existingSecret` referring to a secret in your secret manager
  (External Secrets Operator, sealed-secrets, …) instead of plain values
* `observability.prometheus.enabled: true` and a ServiceMonitor wired
  to your cluster's kube-prometheus-stack
* `observability.otlp.endpoint` pointing at your tracing backend
* `observability.sentry.dsn` set
* `ingress.tls.enabled: true` with cert-manager handling renewals
* Backups via the postgres / SeaweedFS managed offerings (the in-cluster
  `scripts/backup.sh` is for docker-compose deploys only)

> **Note (v1.1.2+):** The docker-compose frontend image bakes a self-signed
> cert at build time and listens on both `:80` and `:443` — needed by the
> self-hosted Playwright Trace Viewer's `SharedArrayBuffer` requirement
> (only available in secure contexts). On Kubernetes, **prefer
> ingress-level TLS termination via cert-manager** (Let's Encrypt or your
> private CA) over the bundled self-signed cert; proper certs avoid the
> manual OS-keychain trust step. The `/install-cert/server.crt` download
> endpoint baked into the frontend image is meant for LAN docker-compose
> users only and can be disabled in production via a nginx config override.

> **Note (v1.1.3+):** Casdoor IAM is now an opt-in sidecar (compose profile
> `casdoor`). For K8s the equivalent is a separate `casdoor` Deployment +
> Service + Ingress at `/casdoor`; this chart does not template it yet.
> Required envs on the backend Deployment when enabled: `CASDOOR_ENABLED`,
> `CASDOOR_ENDPOINT` (e.g., `http://casdoor:8000`), `CASDOOR_ORG`,
> `CASDOOR_APP`, `CASDOOR_CLIENT_ID`, `CASDOOR_CLIENT_SECRET`,
> `CASDOOR_REDIRECT_URL`, `CASDOOR_WEBHOOK_TOKEN`, `CASBIN_ENABLED`,
> `CASDOOR_RECONCILE_ENABLED`. The Casbin enforcer needs a sync SQLAlchemy
> engine pool — `casbin_rule` table is auto-created in `autotest_db` on
> backend lifespan; nothing extra to provision. Run `python -m app.cli
> seed-casbin` as a one-off Job after the migration job.

## Status

| Resource | State |
|---|---|
| Chart.yaml + values.yaml | ✅ |
| _helpers.tpl              | ✅ |
| backend Deployment/Service/PDB | ✅ |
| Opaque Secret (with required keys) | ✅ |
| celery Deployment + KEDA scaler | ⏳ scaffolded in values, template pending |
| frontend Deployment/Service | ⏳ |
| apisix Deployment/Service | ⏳ |
| postgres StatefulSet (in-cluster) | ⏳ — recommend external |
| valkey StatefulSet | ⏳ — recommend external |
| seaweedfs StatefulSet | ⏳ |
| Ingress | ⏳ |
| NetworkPolicy | ⏳ |
| ServiceMonitor (Prometheus) | ⏳ |

The backend template is the highest-leverage piece (custom secrets, env
shape, health probes), so it ships first. The remaining services are
configurable container deploys with established patterns; finish them
when ramping a real K8s deployment.
