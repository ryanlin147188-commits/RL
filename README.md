# AutoTest — 自動化測試平台

> **一套 self-hosted 的測試自動化平台，內建 BDD 案例編輯器、多模式錄製器、Robot Framework + Playwright 執行引擎——全部裝在一份 Docker Compose 裡。**
> Apache 2.0，完全跑在你自己的網路內，無授權費、無按人收費、無 telemetry。

[![License](https://img.shields.io/badge/License-Apache%202.0-green.svg)](LICENSES.md)
[![Robot Framework](https://img.shields.io/badge/Engine-Robot%20Framework%207.x-blue.svg)](https://robotframework.org/)
[![Docker](https://img.shields.io/badge/Deploy-Docker%20Compose-2496ED.svg)](https://docs.docker.com/compose/)
[![Stack](https://img.shields.io/badge/Stack-FastAPI%20%2B%20PostgreSQL%20%2B%20SeaweedFS-0a7e07.svg)](#技術架構)

---

## 這是什麼

AutoTest 是一套精簡、完全自架的 QA 工作台，讓測試團隊能夠：

1. **撰寫** — 以五層樹狀結構（Project → Feature → Page → Scenario → TestCase）管理測試案例，支援 BDD / KDT 步驟格式，內建 Capture 變數、If/ElseIf/Else 條件分支、動態運算式（Mini DSL）及資料驅動（DDT）。
2. **錄製** — Web 錄製（Playwright codegen，Docker noVNC 模式）、API 錄製（mitmproxy Docker 模式）、App 錄製（Appium 腳本轉換）。
3. **執行** — 每次跑都在獨立的短命 Docker 容器內以 Robot Framework 7.x + Playwright 執行，或透過本機 Agent 直接在桌面瀏覽器上跑；提供 WebSocket 即時日誌、逐步截圖、MP4 影片錄製、Playwright trace。
4. **報告** — 執行後自動產生報告，歷史趨勢圖（Chart.js）、per-step 截圖 / 影片 / trace viewer、可匯出 PDF。
5. **排程** — ONCE / DAILY / WEEKLY / MONTHLY 自動觸發，每 30 秒掃描，無需人工介入。
6. **審核** — TestCase / Script / Report 三種實體的 Pending → Approved / Rejected 工作流，完整 audit trail。

---

## 快速開始（約 5 分鐘）

**前置需求**：Docker 24+ 與 Docker Compose v2.23+。Linux、macOS、Windows（Docker Desktop）皆可。

```bash
git clone https://github.com/ryanlin147188-commits/RL-for-Kapito.git
cd RL-for-Kapito

# 步驟 1：自動產生 .env（含隨機 secret；已存在則跳過）
docker compose --profile init run --rm bootstrap

# 步驟 2：預先建置 spawn-time image（Robot runner / Web 錄製 / API 錄製）
#         這些 image 由後端在 runtime 動態 docker run，但必須先建好
#         首次約需 5–10 分鐘
docker compose --profile spawnable build

# 步驟 3：啟動主服務
docker compose up -d --build
```

服務啟動後，開啟 [http://localhost](http://localhost)，以 `admin` / `admin123` 登入。
**第一次登入會強制要求設定顯示名稱、Email 及新密碼。**
自助註冊已停用，新使用者由管理員從「設定 → 專案協作成員」建立。

---

## 常用維運指令

| 操作 | 指令 |
|---|---|
| 查看容器狀態 | `docker compose ps` |
| 追蹤即時日誌 | `docker compose logs -f` |
| 停止（保留資料） | `docker compose down` |
| 完全重置（清除 DB 與儲存） | `docker compose down -v` |
| 開啟 debug 模式 | `AUTOTEST_DEBUG=True docker compose up -d --force-recreate backend` |
| 手動觸發備份 | `docker exec autotest-backup-cron sh /backup.sh` |
| 查看備份清單 | `docker exec autotest-backup-cron ls -lh /backups/` |
| 孤兒清理（不跑 `-a`，會刪 spawn image） | `docker volume prune -f && docker image prune -f` |

---

## 平台功能一覽

| 功能模組 | 說明 |
|---|---|
| **儀表板** | 專案測試統計（總數 / 通過 / 失敗 / 通過率 / 平均時長）、趨勢折線圖、系統健康監控（CPU / 記憶體 / 磁碟 / Docker）|
| **專案管理** | 建立專案、複製整個專案（含完整樹狀結構、TestcaseContent、前置案例連結）|
| **五層測試樹** | Project / Feature / Page / Scenario / TestCase，右鍵 CRUD，Drag-and-drop 排序 |
| **BDD / KDT 編輯器** | 視覺化步驟表格；Given / When / Then / And；行內斷言（Condition + Expected）|
| **Capture 步驟** | 從畫面元素或 API 回傳值取值，存入變數供後續步驟引用 |
| **條件分支** | If / ElseIf / Else / EndIf，由 Robot Framework 7 真實執行，非僅模擬 |
| **動態運算式** | `{{= expr }}` 支援變數、env、算術、字串、uuid()、now()、fakerXxx() 等內建函式 |
| **資料驅動（DDT）** | 每個 TestCase 帶資料表，或引用獨立測試資料集；`{{= row.col }}` 在執行時解析 |
| **前置案例（Setup）** | TestCase 可掛 N 個前置 TestCase，並排 sort_order、可停用、失敗即中止 |
| **Web 錄製器** | Playwright codegen Docker 模式（noVNC 遠端瀏覽器，無需本機安裝）|
| **API 錄製器** | mitmproxy Docker 模式，完整擷取 SPA / REST API 流量，一鍵轉 BDD 步驟 |
| **App 錄製器** | Appium Inspector Python 腳本轉換為 AppiumLibrary keyword 步驟 |
| **執行引擎** | Robot Framework 7.x + Playwright headless，每次執行獨立容器，即時 WebSocket log，截圖 / MP4 影片 / Playwright trace，支援 Docker 與本機 Agent 兩種模式 |
| **測試報告** | 執行報告列表、Dashboard 趨勢圖、per-step trace viewer、PDF 匯出 |
| **TestRun（測試回合）** | 將多個 TestCase 群組成一次批次執行，共用 dashboard 與 KPI |
| **測試排程** | ONCE / DAILY / WEEKLY / MONTHLY，每 30 秒掃描，自動觸發；支援多節點選取與立即執行 |
| **審核中心** | TestCase / Script / Report 審核工作流；Pending / Approved / Rejected 分頁；完整 audit trail |
| **待辦清單（Backlog）** | Feature → Task / Bug / Spike 兩層階層，Sprint 標籤，逾期 badge，CRUD |
| **測試資料集** | 獨立 DDT 資料集（可跨多個 TestCase 共用），欄位 + 資料列管理，JSON 匯入匯出 |
| **環境變數** | 每個專案有獨立環境變數表，Faker 隨機資料生成，`.env` 格式批次匯入 |
| **Mock 端點** | Per-project mock API 定義（method + path + canned response），供前端測試尚未完成的 API |
| **本機 Agent** | 有頭模式，讓測試在你的桌面瀏覽器上跑（便於 debug），無需 headless Docker |
| **Markdown 匯入/匯出** | 整個子樹可與 `.md` 檔雙向轉換，方便版控與跨環境搬遷 |
| **版本歷史** | 每次儲存 TestCase 皆建立 entity_version 快照，可回溯或比對 diff |
| **RBAC 三層** | Global / Org / Project 三層權限，角色 CRUD（含 clone），per-project 權限 override，群組（可巢狀）|
| **Auth / SSO** | 本地帳號 + 可選 Zoho OIDC SSO；JWT httpOnly cookie；refresh token；首次登入強制設定密碼 |
| **通知中心** | in-app 通知，審核事件自動推送，可標記全部已讀 |
| **Audit Log** | 所有 mutation 動作記錄，完整 actor / action / entity / timestamp / diff |
| **REST API** | 完整 OpenAPI / Swagger；`/api/executions` 對 CI/CD 開放（Jenkins / GitHub Actions / GitLab CI）|

完整操作教學見 [操作說明.md](操作說明.md)。

---

## <a id="技術架構"></a> 技術架構

```
┌──────────────────────────────────────────────────────────────────┐
│  Web UI（Vanilla JS + Tailwind CDN，無需建置步驟）                 │
│  按需 lazy-load：Chart.js / html2pdf                              │
└─────────────────────────────┬────────────────────────────────────┘
                              │ HTTP / WebSocket（port 80 / 443）
┌─────────────────────────────▼────────────────────────────────────┐
│  nginx（前門、SPA shell、                                          │
│         /recorder/<id>/* WebSocket reverse-proxy 至 spawn 容器）  │
└─────────────────────────────┬────────────────────────────────────┘
                              │ /api/*  /ws/*  /pics/*  /results/*
┌─────────────────────────────▼────────────────────────────────────┐
│  FastAPI（Python 3.11）                                           │
│  OIDC · slowapi 限速 · Fernet 加密 · Casbin RBAC                  │
│  100+ 個 REST 端點 + WebSocket 執行 log 串流                       │
└──────────┬───────────────┬──────────────────┬────────────────────┘
           │               │                  │
┌──────────▼───┐ ┌─────────▼──────┐ ┌─────────▼──────────────────┐
│ PostgreSQL 16 │ │   Valkey 8     │ │ Celery worker               │
│（主要資料庫） │ │（快取 + 佇列） │ │  → robot-runner（每次執行）  │
│     ↕ WAL    │ │ Redis 協議相容 │ │  → recorder（Web 錄製）      │
│ postgres-    │ └────────────────┘ │  → recorder-api（API 錄製）  │
│ replica（熱備）│                  └────────────────────────────┘
│     ↓        │
│ backup-cron  │  ┌──────────────┐ ┌──────────────┐
│（日 03:00）  │  │  SeaweedFS   │ │ docker-proxy │
└──────────────┘  │（S3 相容）   │ │（安全 Docker  │
                  │ 截圖/影片/   │ │  socket 代理）│
                  │ trace 儲存   │ └──────────────┘
                  └──────────────┘
```

### 服務清單

**常駐服務（10 個）**：

| 服務 | 說明 |
|---|---|
| `postgres` | PostgreSQL 16 主庫（WAL 啟用，支援 streaming replication） |
| `postgres-replica` | PostgreSQL 16 熱備副本（streaming replication，readonly standby） |
| `backup-cron` | 每日 03:00 自動備份（從副本 pg_dump + SeaweedFS tar，保留 7 天） |
| `valkey` | Valkey 8（快取 + Celery broker，Redis wire protocol 相容） |
| `docker-proxy` | 安全 Docker socket 代理（限制 backend 只能呼叫必要 API） |
| `seaweedfs` | SeaweedFS 3.80（S3 相容物件儲存，存截圖 / 影片 / trace） |
| `seaweedfs-init` | 一次性 bucket 建立（`pic`、`results`） |
| `backend` | FastAPI（Python 3.11），port 8000（不對外暴露） |
| `celery` | Celery worker，執行測試 / 錄製任務 |
| `frontend` | nginx（port 80 / 443），SPA shell + reverse proxy |

**按需建置（profile=spawnable，4 個）**：`robot-runner`、`recorder`、`recorder-api`、`mcp`

**初始化（profile=init，1 個）**：`bootstrap`（一次性，產生 .env）

---

## 部署到正式環境前的安全強化

1. 將 `ALLOWED_ORIGINS` 設為你的前端 origin（**不要**使用 `*`）
2. 覆寫預設密鑰：`AUTOTEST_JWT_SECRET`、`AUTOTEST_FERNET_KEY`、`DB_PASSWORD`、`S3_ROOT_PASSWORD`、`REPLICA_PASSWORD`（bootstrap 首次執行時自動生成隨機值；之後定期 rotate）
3. 部署在 HTTPS reverse proxy 後方（Let's Encrypt 或企業 CA）
4. 將 `RECORDER_IMAGE` 與 `ROBOT_RUNNER_IMAGE` 釘定為特定 tag 或 sha256（**不要**使用 `latest`）
5. 定期備份已由 `backup-cron` 容器自動處理（每日 03:00）；可另外設定 S3 鏡像（`S3_BUCKET` 環境變數）

**Docker log rotation（一次性設定）：**

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF
sudo systemctl restart docker
```

詳細安全政策見 [SECURITY.md](SECURITY.md)。

---

## Zoho SSO 啟用方式

```bash
# 1. 前往 https://api-console.zoho.com → Add Client → Server-based Applications
#    Authorized Redirect URIs: http://<your-host>/api/auth/zoho/callback

# 2. 將以下內容加入 .env：
echo "ZOHO_CLIENT_ID=<client_id>"                              >> .env
echo "ZOHO_CLIENT_SECRET=<secret>"                             >> .env
echo "ZOHO_REDIRECT_URL=http://<host>/api/auth/zoho/callback"  >> .env

# 3. 重啟 backend：
docker compose up -d --force-recreate backend

# 4. 重新整理登入頁，橘色「使用 Zoho 登入」按鈕即出現
```

---

## FAQ

**Q：為什麼不直接用 TestRail / Zephyr / qTest？**
按人頭計費、資料存在外部雲端、export 格式受限。AutoTest 全部跑在自己的伺服器上，資料格式完全開放（PostgreSQL + Markdown）。

**Q：為什麼不直接用 Robot Framework + CI server？**
錄製器、BDD 視覺化編輯、逐步截圖與影片、趨勢報表、RBAC、多租戶、五層樹、排程、審核流程——這些都不是 vanilla Robot Framework 提供的。AutoTest 把它們組成一個完整產品。

**Q：可以對外直接 expose backend port 8000 嗎？**
**不可以。** 所有外部流量必須經 nginx（port 80/443），`backend:8000` 不對 host expose。

**Q：Apple Silicon（M1 / M2 / M3）支援嗎？**
可執行（透過 Rosetta 2），但 recorder / robot-runner 容器為 x86-64，比原生 amd64 主機慢 2–4 倍。

**Q：支援 HTTPS 嗎？**
nginx 設定已預留 443 port，掛上你的 TLS 憑證即可。建議搭配 Let's Encrypt（Certbot）或反向代理（Traefik / Caddy）。

**Q：可以串接 CI/CD 嗎？**
可以。`POST /api/executions` 接受 Bearer token 呼叫，GitHub Actions / Jenkins / GitLab CI 均可直接觸發，見 [操作說明.md](操作說明.md) 第九章。

**Q：postgres-replica 啟動失敗怎麼辦？**
先確認 `.env` 中 `REPLICA_PASSWORD` 已設定，再執行 `./scripts/setup-replica.sh`（既有部署一次性設定 replication user），然後 `docker compose up -d postgres-replica backup-cron`。

---

## 貢獻

- Bug 回報 / 功能請求：[開 issue](../../issues)
- 安全漏洞：見 [SECURITY.md](SECURITY.md)
- 社群規範：見 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- PR 歡迎 — 請在送出前於本地執行 `gitleaks`、`pip-audit`、`bandit`

---

## 授權

Apache License 2.0。完整條文與第三方相依授權見 [LICENSES.md](LICENSES.md)。

---

> 完整操作教學見 [操作說明.md](操作說明.md)
