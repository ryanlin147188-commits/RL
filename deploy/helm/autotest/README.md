# AutoTest Helm Chart（骨架）

本 Chart 將 AutoTest v1.1 部署至 Kubernetes。**目前狀態：骨架。**
Backend Deployment + Service + PDB + Secret + chart helpers 已可用於正式環境；其餘服務（celery、frontend、postgres、valkey、seaweedfs、ingress）的 template 為待辦事項，狀態見下方「目前狀態」表格。

---

## 快速開始（kind / minikube）

```bash
# 1. 在本機建置 image 並推送至你的 registry，或
#    --set global.imageRegistry=ghcr.io/your-org 並指定真實 tag
helm install autotest ./deploy/helm/autotest \
  --set postgres.password=$(openssl rand -hex 16) \
  --set secrets.jwtSecret=$(openssl rand -hex 32) \
  --set secrets.fernetKey=$(python -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())') \
  --set secrets.minioRootPassword=$(openssl rand -hex 16)
```

---

## 正式環境 Checklist

- `postgres.external: true`，指向託管式 PostgreSQL（RDS / CloudSQL）
- `valkey.external: true`，指向託管式 Redis（ElastiCache）
- `secrets.existingSecret` 指向 secret manager 中的 Secret（External Secrets Operator、sealed-secrets 等），而非明文 values
- `observability.prometheus.enabled: true`，並將 ServiceMonitor 連接至叢集的 kube-prometheus-stack
- `observability.otlp.endpoint` 指向你的 tracing backend
- `observability.sentry.dsn` 設定完成
- `ingress.tls.enabled: true`，由 cert-manager 處理憑證更新
- 備份透過 PostgreSQL / SeaweedFS 的託管服務進行（`scripts/backup.sh` 僅適用於 docker-compose 部署）

> **注意（v1.1.2+）**：docker-compose 的 frontend image 在 build time 產生自簽憑證，同時監聽 `:80` 和 `:443`——這是 Playwright Trace Viewer 的 `SharedArrayBuffer` 要求（只在 secure context 下可用）。在 Kubernetes 上，**建議使用 ingress 層的 TLS termination（cert-manager + Let's Encrypt 或私有 CA）**，而非 image 內建的自簽憑證；正確的憑證不需要手動信任 OS keychain。前端 image 內建的 `/install-cert/server.crt` 下載端點僅供 LAN docker-compose 使用者，可在正式環境透過 nginx 設定覆蓋予以停用。

---

## 目前狀態

| 資源 | 狀態 |
|---|---|
| Chart.yaml + values.yaml | ✅ 完成 |
| _helpers.tpl | ✅ 完成 |
| Backend Deployment / Service / PDB | ✅ 完成 |
| Opaque Secret（含必要金鑰） | ✅ 完成 |
| Celery Deployment + KEDA scaler | ⏳ 已在 values 中規劃，template 待完成 |
| Frontend Deployment / Service | ⏳ 待完成 |
| Postgres StatefulSet（叢集內） | ⏳ 待完成 — 建議使用外部託管 |
| Valkey StatefulSet | ⏳ 待完成 — 建議使用外部託管 |
| SeaweedFS StatefulSet | ⏳ 待完成 |
| Ingress | ⏳ 待完成 |
| NetworkPolicy | ⏳ 待完成 |
| ServiceMonitor（Prometheus） | ⏳ 待完成 |

Backend template 是最高效益的部分（自訂 Secret、env 結構、health probe），因此優先實作。其餘服務為有既定模式的容器部署；在實際 K8s 部署需求出現時再完成。
