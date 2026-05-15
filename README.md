# AutoTest — 自動化測試平台

> **一套 self-hosted 的測試自動化平台，內建錄製器、BDD 案例編輯器、Robot Framework + Playwright 執行引擎 — 全部裝在一份 Docker Compose 裡。**
> Apache 2.0，跑在你自己的網路內，無授權費、無按人收費、無 telemetry。

[![License](https://img.shields.io/badge/License-Apache%202.0-green.svg)](LICENSES.md)
[![Robot Framework](https://img.shields.io/badge/Engine-Robot%20Framework%207.x-blue.svg)](https://robotframework.org/)
[![Docker](https://img.shields.io/badge/Deploy-Docker%20Compose-2496ED.svg)](https://docs.docker.com/compose/)
[![Stack](https://img.shields.io/badge/Stack-FastAPI%20%2B%20PostgreSQL%20%2B%20SeaweedFS-0a7e07.svg)](#技術架構)

---

## 這是什麼

AutoTest 是一套精簡、自架的測試管理與自動化執行平台，讓 QA 團隊能夠：

1. **撰寫測試** — 以 Markdown + BDD 步驟格式編寫測試案例，支援 Capture 變數、If/ElseIf/Else 條件分支、動態運算式，以及資料驅動測試（DDT）。
2. **錄製腳本** — 三種模式：Web（Playwright codegen）、API（mitmproxy 或貼 cURL）、App（Appium / iOS Web Inspector / Android uiautomator2）。
3. **自動執行** — 每次執行皆在獨立的 Docker 容器內以 Robot Framework 7.x + Playwright 跑起來，提供 WebSocket 即時日誌、逐步截圖、影片錄製、Playwright trace。
4. **查看報告** — Allure 風格的執行報告，包含歷史趨勢圖、trace viewer、可匯出 PDF。
5. **定期排程** — 設定 ONCE / 每日 / 每週 / 每月排程，自動觸發測試執行，無需人工介入。

---

## 快速開始（約 5 分鐘）

**前置需求**：Docker 24+ 與 Docker Compose v2.23+。支援 Linux、macOS、Windows（Docker Desktop）。

```bash
git clone https://github.com/ryanlin147188-commits/RL-for-Kapito.git
cd RL-for-Kapito

# 1) 自動產生 .env（含隨機 secret；若已存在則不覆寫）
docker compose --profile init run --rm bootstrap

# 2) 預先建置 spawn-time image（Robot runner / Web 錄製 / API 錄製）
#    這些 image 由後端在 runtime 動態 docker run，但必須先存在
#    首次建置約需 5–10 分鐘
docker compose --profile spawnable build

# 3) 啟動主服務
docker compose up -d --build

# (可選) 修改預設管理員密碼；若未設定，預設為 admin / admin123
# echo "AUTOTEST_DEFAULT_ADMIN_PASSWORD=YourPassword123" >> .env
```

服務啟動後，開啟 [http://localhost](http://localhost)，以 `admin` / `admin123` 登入（第一次登入會強制要求修改密碼）。**自助註冊已停用** — 新使用者由 **設定 → 專案協作成員** 建立。

### 常用操作指令

| 操作 | 指令 |
|---|---|
| 查看容器狀態 | `docker compose ps` |
| 追蹤即時日誌 | `docker compose logs -f` |
| 停止（保留資料） | `docker compose down` |
| 完全重置（清除 DB 與儲存） | `docker compose down -v` |
| 開啟 debug 模式 | `AUTOTEST_DEBUG=True docker compose up -d --force-recreate backend` |

---

## 平台功能一覽

| 功能模組 | 說明 |
|---|---|
| **測試案例管理** | Project / Feature / Platform / Page / Scenario / TestCase 六層樹狀結構，Markdown 編輯，版本歷史 |
| **BDD 編輯器** | 視覺化步驟編輯表格，支援 Given / When / Then / And，行內斷言（Condition + Expected）|
| **Web 錄製器** | Playwright codegen 本機模式或 Docker 模式（含 noVNC 遠端瀏覽器）|
| **API 錄製器** | 貼上 cURL 指令解析，或 mitmproxy Docker 模式自動擷取流量 |
| **App 錄製器** | Appium / iOS Web Inspector / Android uiautomator2 腳本轉換 |
| **資料驅動（DDT）** | 每個案例可帶資料表，單次執行自動跑完所有資料列 |
| **動態運算式** | `{{= expression }}` 支援變數引用、環境變數、算術、內建函式（uuid、now、faker 等）|
| **Capture 步驟** | 從畫面或 API 回傳值擷取變數，供後續步驟使用 |
| **條件分支** | If / ElseIf / Else / EndIf，由 Robot Framework 真實執行 |
| **執行引擎** | Robot Framework 7.x + Playwright headless，每次執行於獨立容器，支援逐步截圖、影片、trace、retry |
| **執行報告** | Allure 風格報告，歷史趨勢圖（Chart.js），trace viewer，PDF 匯出 |
| **測試排程** | ONCE / DAILY / WEEKLY / MONTHLY 排程，每 30 秒掃描並自動觸發 |
| **測試回合** | 將多次執行群組成 Round，共用 dashboard 與 KPI |
| **審核中心** | testcase / script / report 的審核工作流，pending / approved / rejected 分頁，完整 audit trail |
| **待辦清單** | Feature → Task / Bug / Spike 階層，Sprint 標籤，過期 badge |
| **環境變數** | 每個專案有獨立環境變數表，執行時以 `{{= env.KEY }}` 引用 |
| **Mock 端點** | 為尚未完成後端的測試案例提供 mock API 回應 |
| **本機 Agent** | 有頭瀏覽器模式，讓測試在實體桌面上執行（便於 debug）|
| **Markdown 匯入/匯出** | 整個專案樹可與 `.md` 檔案雙向轉換，方便版控與遷移 |
| **REST API** | 完整 Swagger，`/api/executions` 對外開放，支援 Jenkins / GitHub Actions / GitLab CI |
| **RBAC 與成員管理** | 三層權限（Global / Org / Project），角色 CRUD，群組（可巢狀），邀請管理 |
| **Auth / SSO** | fastapi-users + argon2，Zoho OIDC，JWT httpOnly cookie，強制首次修改密碼 |
| **Audit Log** | 所有變更動作記錄，符合 SOC 2 baseline |

完整使用教學見 [操作說明.md](操作說明.md)。

---

## <a id="技術架構"></a> 技術架構

```
┌─────────────────────────────────────────────────────────────────┐
│        Web UI（Vanilla JS + Tailwind CDN，無需建置步驟）          │
│        按需 lazy-load：Chart.js / Mermaid / html2pdf             │
└──────────────────────────────┬──────────────────────────────────┘
                               │ HTTP / WebSocket（port 80）
┌──────────────────────────────▼──────────────────────────────────┐
│   nginx（前門、SPA shell、                                        │
│          /recorder/<id>/* WebSocket reverse-proxy）              │
└──────────────────────────────┬──────────────────────────────────┘
                               │ /api/*  /ws/*  /pics/*  /results/*
┌──────────────────────────────▼──────────────────────────────────┐
│         FastAPI（Python 3.11）                                   │
│         OIDC · slowapi 限速 · Fernet 加密 · Casbin RBAC          │
└────────┬───────────────┬─────────────────┬──────────────────────┘
         │               │                 │
┌────────▼─────┐ ┌───────▼──────┐ ┌────────▼──────────────────────┐
│ PostgreSQL 16│ │  Valkey 8    │ │ Celery worker                  │
│（主要資料庫） │ │（快取 + 佇列）│ │  → robot-runner（Robot FW）    │
└──────────────┘ └──────────────┘ │  → recorder（Playwright）      │
                                  │  → recorder-api（mitmproxy）   │
┌──────────────┐                  │  （每次執行各自的短命容器）     │
│  SeaweedFS   │                  └───────────────────────────────┘
│（S3 相容儲存）│
└──────────────┘
```

**常駐服務**（8 個）：`postgres`、`valkey`、`docker-proxy`、`seaweedfs`、`seaweedfs-init`（一次性）、`backend`、`celery`、`frontend`

**依需求建置**（4 個，profile=spawnable）：`robot-runner`、`recorder`、`recorder-api`、`mcp`

---

## 部署到正式環境前的安全強化

在對外暴露 AutoTest 之前，請完成以下設定：

- 將 `ALLOWED_ORIGINS` 設為你的前端 origin（**不要**使用 `*`）
- 覆寫預設密鑰：`AUTOTEST_JWT_SECRET`、`AUTOTEST_FERNET_KEY`、`DB_PASSWORD`、`S3_ROOT_PASSWORD`
  （bootstrap profile 首次執行時會自動生成隨機值；之後請定期 rotate）
- 部署在 HTTPS reverse proxy 後方（Let's Encrypt 或企業 CA）
- 將 `RECORDER_IMAGE` 與 `ROBOT_RUNNER_IMAGE` 釘定為特定 tag 或 sha256（**不要**使用 `latest`）
- 定期備份 PostgreSQL 與 SeaweedFS volume
- 設定 Docker log rotation，防止日誌無限增長：

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF
sudo systemctl restart docker
```

詳細漏洞通報政策見 [SECURITY.md](SECURITY.md)。

---

## Zoho SSO 設定

```bash
# 1. 前往 https://api-console.zoho.com → Add Client → Server-based Applications
#    Authorized Redirect URIs：http://<your-host>/api/auth/zoho/callback

# 2. 將以下變數加入 .env：
echo "ZOHO_CLIENT_ID=<client_id>"   >> .env
echo "ZOHO_CLIENT_SECRET=<secret>"  >> .env
echo "ZOHO_REDIRECT_URL=http://<host>/api/auth/zoho/callback" >> .env

# 3. 重啟 backend：
docker compose up -d --force-recreate backend

# 4. 重新整理登入頁，橘色「使用 Zoho 登入」按鈕即出現
```

---

## FAQ

**Q：為什麼不直接用 TestRail / Zephyr / qTest？**
按人頭計費、測試資料存在外部雲端、export 格式非開放。AutoTest 全部跑在自己的網路內，資料格式完全開放。

**Q：為什麼不直接用 Robot Framework + CI server？**
撰寫 UI、錄製器、逐步截圖與影片、歷史趨勢圖、RBAC、多租戶範圍、測試樹狀結構，都不是 vanilla Robot Framework 原生提供的。AutoTest 把這些整合成一個產品，讓團隊有共同的 source of truth。

**Q：Apple Silicon（M1 / M2 / M3）支援嗎？**
可以執行（透過 Rosetta 2），但 recorder 容器為 x86-64，比原生 amd64 機器慢 2–4 倍。原生 arm64 image 在 roadmap 中。

**Q：可以對外開放 backend port 8000 嗎？**
**不可以。** 預設 `backend:8000` 不對 host expose，所有外部流量必須經過 nginx。請參閱 [SECURITY.md](SECURITY.md)。

---

## 貢獻

- Bug 回報 / 功能請求：[開 issue](../../issues)
- 安全漏洞：見 [SECURITY.md](SECURITY.md)
- 社群規範：見 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- PR 歡迎 — 請在送出前於本地執行 `gitleaks`、`pip-audit`、`bandit`（CI 也會執行這三項）

---

## 授權

Apache License 2.0。完整條文與第三方相依授權見 [LICENSES.md](LICENSES.md)。

---

> 完整使用教學見 [操作說明.md](操作說明.md)。
