# AutoTest — Enterprise Test Automation Platform

> 🌐 **語言**: [English](README.md) · **繁體中文**

> **一套 self-hosted 的測試自動化平台,內建錄製器、BDD 案例編輯器、Robot Framework + Playwright runner — 全部裝在一份 Docker Compose 裡。**
> Apache 2.0,跑在你自己的網路內,無授權費、無按人收費、無 telemetry。

[![License](https://img.shields.io/badge/License-Apache%202.0-green.svg)](LICENSES.md)
[![Robot Framework](https://img.shields.io/badge/Engine-Robot%20Framework%207.x-blue.svg)](https://robotframework.org/)
[![Docker](https://img.shields.io/badge/Deploy-Docker%20Compose-2496ED.svg)](https://docs.docker.com/compose/)
[![Stack](https://img.shields.io/badge/Stack-FastAPI%20%2B%20PostgreSQL%20%2B%20SeaweedFS-0a7e07.svg)](#tech-stack)

---

## 這是什麼

AutoTest 是個有意識精簡過的 self-hosted 平台,讓 QA 團隊做到:

1. **撰寫** — 用 Markdown + BDD 寫測試案例,支援 Capture 變數、
   If/ElseIf/Else 分支、動態運算式、資料驅動列(DDT)。
2. **錄製** — 三種模式:Web(Playwright codegen)、API(mitmproxy
   或貼 cURL)、App(Appium / iOS Web Inspector / Android
   uiautomator2)。
3. **執行** — 每次跑都起一個獨立的 Docker container,用
   Robot Framework 7.x + Playwright Browser Library + AppiumLibrary,
   WebSocket 即時日誌、每步截圖、video、Playwright trace。
4. **報告** — Allure 風格的執行檢視,歷史走勢圖、trace viewer、
   缺陷連結欄位、可匯出 PDF。

原本是一套很大的 ALM 平台(含缺陷 / 需求 / RTM / WBS / 看板 /
AI 助理 / 設備池 / 測試計畫 / 里程碑 / 排程 / 指派 / 測試文件)。
**v1.1.9 故意把這些拿掉**,聚焦回「寫 → 跑 → 看報告」的主軸。

---

## 🔥 v1.1.9 — 精簡版

把平台砍回最核心的「測試案例撰寫 → 執行 → 報告」迴圈。刪除 13 項功能:

- **測試看版**(多 entity kanban)
- **測試專案 page**(獨立的專案管理頁。`projects` entity 本體還在,
  從側邊欄的專案切換器存取即可)
- **DB 資訊**(per-project DB 連線管理)
- **AI 助理** — Hermes ACP / mem0 / OpenClaw 整個拔掉:
  三個 sidecar container、~14 個後端檔、聊天面板、AI Token 設定、
  AI-Enhance 錄製、MCP 測試面板、Vision-enhanced 錄製
- **設備資訊**(專案設備列表)
- **測試文件**(test_documents entity)
- **測試版號 UI**(model 留著因為 Defect / TestRound / ExecutionReport
  都有 `test_version_id` FK,但管理頁拿掉)
- **測試時程**(`schedules` + `test_milestones` + 後台 `scheduler_loop`)
- **測試計畫**
- **WBS**(work breakdown structure + WBS links)
- **需求/RTM UI**(model 為 FK 完整性留著,UI 拿掉)
- **缺陷管理 UI**(model 留著,UI 拿掉)
- **指派**(跨 entity Assignment / 我的工作 inbox / bulk reassign)。
  保留 entity 的 `assigned_to` 欄位 — 測試案例與 review 仍可從各自的
  PATCH 端點設定 assignee。

**DB schema 全部保留。** Alembic migrations 一行都沒動,要刪除功能對應
的 table 還在 PostgreSQL 裡。需要的話可以再出一支 migration 一次清掉。

**前端 dead code 也一併掃乾淨。** ~2,100 行沒入口的 JS(AI chat
helpers、Hermes modal helpers、MCP 測試面板、AI Token CRUD、
我的工作 inbox、錄製 AI Enhance、agent runtime 偏好)連同對應的 CSS
selector 一起刪除。`showBacklogView` 已改寫成不再轉跳到已刪除的 kanban
wrapper,**待辦清單** 可由 `#backlog` route 或 linked-todos popover 進入。

---

## 🔥 v1.1.8.1 — Middleware decode 與 OIDC JIT 走 fastapi-users

把兩個還在 hand-rolled 的高價值缺口收回 fastapi-users 體系:

- **Middleware JWT decode**:`app.middleware.AuthMiddleware` 改用
  `fastapi_users_integration.decode_access_token_payload`,跟
  `UsernameSubJWTStrategy.read_token` 共用同一個 decode 函式。
  「valid access token 長什麼樣」**只有一個來源**,middleware 與
  dependency chain 不會 drift。`typ == "access"` 檢查也搬進這個 helper,
  refresh token 打 `/api/*` 會從同一條路徑回 401。
- **OIDC JIT 走 UserManager**:`routers/oidc_auth.py` 不再 hand-roll
  「by (provider, sub) → by email → create」流程。邏輯改放在
  `UserManager.get_or_provision_via_oidc()`,SSO 建立的使用者改用
  **argon2** password hash(透過 PasswordHelper)而非 bcrypt。
  Access token 簽發從 hand-rolled `create_access_token` 改成
  `JWTStrategy.write_token`。

### 🧹 Ops runbook:container log rotation 必設

Docker daemon 預設**沒有** log rotation。在 host 跑一次:

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF
sudo systemctl restart docker
```

把每個 container 的 log 限制在 `10MB × 3 份 = 30MB`。**設一次就好。**
要清掉已經長太大的 log,用
`sudo find /var/lib/docker/containers -name '*-json.log' -exec truncate -s 0 {} +`。

孤兒清理:`docker volume prune -f` + `docker image prune -f` +
`docker builder prune -af`。**不要跑** `docker image prune -a -f` —
平台會經由 `docker.sock` 動態 spawn `autotest-robot-runner` /
`autotest-recorder`,這些 image 沒有長住的 container 參考,blanket
`-a` prune 會把它們也刪掉,測試執行就斷了。

---

## 🔥 v1.1.8 — Auth router 真的走 fastapi-users

v1.1.7 把 fastapi-users 接進來、跑了四支 alembic migration,但 request
路徑上**只有 PasswordHelper 真的被叫**。v1.1.8 把切換完成:

- **全部 260 個 `Depends(get_current_user)` 走 fastapi-users**:
  `dependencies.py::get_current_user` 是
  `fastapi_users_integration.current_active_user` 的 thin alias。
  每個 request 跑 `_fa_current_active_user` →
  `UsernameSubJWTStrategy.read_token` → `UserManager.get_by_username`
  → DB lookup。
- **Login 走 `UserManager.authenticate_by_username`**:username lookup
  + bcrypt verify + **不存在使用者也跑一次 dummy hash**(擋掉 v1.1.7
  時手刻 login 的 timing attack 缺口)+ **bcrypt → argon2 漸進式
  rehash**:登入成功的同時把舊 bcrypt hash 升級成 argon2,自動每次
  登入升級一個使用者。
- **Access token 由 `JWTStrategy.write_token` 簽發**:
  `UsernameSubJWTStrategy` 覆寫 `read_token` / `write_token` 改成
  `sub=username`,而非 fastapi-users 預設的 UUID id,讓 SPA / Casbin
  / log 的 identifier 一致。
- **管理者 CRUD 走 `UserManager`**:`POST /auth/users` 用
  `password_helper.hash()` + `on_after_register()`;`PUT` / 重設密碼
  用 `_update()`;`DELETE` 用 `delete()`。

故意不搬:refresh token(fastapi-users 13 沒這概念)、
`must_change_password` gate、`org_id` cookie、Casbin RBAC。

---

## 🔥 v1.1.7 — FastAPI Users 基礎裝配 + Schema migration

八個 commit、四支 alembic migration 在 `feat/fastapi-users` 分支把
auth 後端從 hand-rolled bcrypt + JWT 切到標準的 fastapi-users。
SPA 沒改;現有 admin + 100+ 使用者都活著。

- migration `0027` 給 `users.id` 加 UUID 欄(`gen_random_uuid()::text`
  default),既有 row 用 backfill 補上。
- migration `0028` 在六個 FK 點(project_members ×2、org_memberships
  ×2、group_memberships、password_reset_tokens)加 nullable `user_id`
  UUID shadow 欄並 JOIN-backfill。
- migration `0029` 把 `users.id` 升 PK;六個 FK 重建,指向新的
  `uq_users_username` unique constraint,Application code 不動。

`SQLAlchemyUserDatabase` / `UserManager` / `JWTStrategy` /
`PasswordHelper` / `BearerTransport` 在
`backend/app/auth/fastapi_users_integration.py` 接好。

Zoho OAuth 從 authlib `AsyncOAuth2Client` 換成 `httpx-oauth.BaseOAuth2`
(fastapi-users 的 OAuth2 client 家族)。

---

## 🔥 v1.1.6 — Per-project 角色權限 override + 三欄首登 modal

同一個 `Project-Tester` 角色,可以在不同專案有不同的有效權限,
**不用 clone 整個角色**。

- **Per-project override 表** `project_role_permissions(project_id,
  role_id, permissions_json)`。SPA 的 **設定 → 專案協作成員** 多了
  「本專案角色權限」區段,列 4 個 project-scope 角色 + 有效權限數 +
  override badge;按「編輯」可勾掉 / 加上權限,只對這個專案生效。
- **Casbin sync 寫入 alias role** `<role>@<short_pid>` 到 `casbin_rule`,
  任何有 override row 的 (project, role) 都會自動建立。
  Enforce 直接吃這個 alias 在 `project:<pid>` domain 下的 p 規則。
- **首登 profile modal 三欄化**(display_name + email + 新密碼),
  端點 `POST /api/auth/profile-setup`,觸發條件不變
  (`users.must_change_password=True`)。

### 推薦的協作 SOP

| 角色 | Global `users.role_id` | `ProjectMember.role_id` | 用途 |
|---|---|---|---|
| 平台擁有者(你) | NULL + `is_superuser=True` | — | 全平台寫入 |
| 客戶 PM | NULL | `Project-Reviewer` | 看計畫、核准 |
| 外部 QA | NULL | `Project-Tester`(+ override if 需要) | 寫案例、跑測試 |
| 唯讀利害關係人 | NULL | `Project-Viewer` | 看 dashboard / 報告 |

---

## 🔥 v1.1.5 — Casdoor sidecar 下架,改 in-process authlib

跑了兩個 minor 版本的 Casdoor sidecar 後,IAM 回到 FastAPI 程序內。

- **Casdoor sidecar 移除。** Compose service / configs / `casdoor`
  Postgres DB / 14 個後端 module 都拿掉。
- **OIDC 改 in-process(`authlib`)**。新路由
  `GET /api/auth/{provider}/login` 跟 `/callback` 直接走 OAuth code
  flow。目前只接 `zoho`;加 Google / Microsoft / Okta 只要在
  `backend/app/auth/oidc.py` 寫一個 30 行的 `OIDCProvider` dataclass。
- **Token 格式還原為 HS256 in-house JWT**(同 v1.1.2)。
- **本地密碼端點回來**:`POST /auth/login` /
  forgot-password / reset-password / change-password / 使用者 CRUD /
  角色 CRUD 全部回到 live code。
- **Casbin 保留,行為不變。** 仍在 FastAPI 程序內跑,以 `casbin_rule`
  表為 source of truth。

### 啟用 Zoho SSO

```bash
# 1. https://api-console.zoho.com → Add Client → Server-based Applications
#    Authorized Redirect URIs: http://<your-host>/api/auth/zoho/callback
# 2. 寫進 .env:
echo "ZOHO_CLIENT_ID=<client_id>"     >> .env
echo "ZOHO_CLIENT_SECRET=<secret>"     >> .env
echo "ZOHO_REDIRECT_URL=http://<host>/api/auth/zoho/callback" >> .env
# 3. 重啟 backend:
docker compose up -d --force-recreate backend
# 4. 重整 SPA 登入頁,橘色「使用 Zoho 登入」按鈕出現。
```

---

## 快速開始(Docker,~5 分鐘)

**前置**:Docker 24+ 與 Docker Compose v2.23+。Linux、macOS、Windows
(Docker Desktop)皆可。

```bash
git clone https://github.com/ryanlin147188-commits/RL-for-Kapito.git && cd RL-for-Kapito

# 1) 自動產 .env(含隨機 secret;若已有 .env 不覆寫)
docker compose --profile init run --rm bootstrap

# 2) 預 build spawn-time image(robot runner / 錄製 web / 錄製 api)
#    這些 image 是 backend 在 runtime 動態 `docker run` 出來的容器,
#    但必須先存在。首次 build 約 5–10 分鐘。
docker compose --profile spawnable build

# 3) 啟動主服務
docker compose up -d --build

# (可選) 4) 改 seed admin 的初始密碼;沒設就用內建的 admin/admin123
# echo "AUTOTEST_DEFAULT_ADMIN_PASSWORD=Op3rator-Init" >> .env
```

後台起來後用 `admin` / `admin123`(或你的 override)登入。第一次登入
會強制改密碼。**自助註冊已停用** — 新使用者由 **設定 → 專案協作成員**
建立。

**預設網址**

| 服務 | URL |
|---|---|
| Web UI | <http://localhost> |
| REST API (Swagger) | 進 backend 容器:`docker compose exec backend curl localhost:8000/docs` — port 8000 不對外開,見 [SECURITY.md](SECURITY.md) |
| Logs | `docker compose logs -f`;VictoriaLogs 內部用 |

### 日常操作

| 想做… | 指令 |
|---|---|
| 看運行容器 | `docker compose ps` |
| 追 log | `docker compose logs -f` |
| 停止(保留資料) | `docker compose down` |
| 全部重置(DESTRUCTIVE — 清掉 DB 與 S3) | `docker compose down -v` |
| 啟動 obs 堆疊 | up/down 帶 `--profile obs` |
| backend debug mode | `AUTOTEST_DEBUG=True docker compose up -d --force-recreate backend` |

---

## 平台目前的功能(v1.1.9 之後)

| 層級 | 能力 |
|---|---|
| **測試案例管理** | Project / Feature / Platform / Page / Scenario / TestCase 五層樹,Markdown 編輯,版本歷史(`entity_versions` 鏡像表) |
| **撰寫** | Visual recorder(Playwright codegen)、API recorder(mitmproxy / cURL paste)、App recorder(Appium / iOS Web Inspector / Android uiautomator2)、手動 BDD 編輯器、動態運算式、Capture step、If / ElseIf / Else / EndIf 分支 |
| **資料驅動(DDT)** | 每個案例自帶資料集,單一案例可重複跑 N 筆資料;`{{=row.column}}` 在執行時解析 |
| **執行** | Robot Framework 7.x + Playwright headless,每次跑都起一個獨立 runner 容器,WebSocket 即時 log、每步截圖 / 影片 / trace、tag、Retry-on-flaky |
| **報告** | 每次執行一份 Allure 風格報告、歷史走勢圖(Chart.js)、Per-step trace viewer 整合、可匯出 PDF |
| **測試回合(Test Round)** | 群組多次執行(例:「Smoke Round 2024-Q1」),共用一個 dashboard |
| **審核中心** | 通用審核工作流,涵蓋 testcases / scripts / reports — pending / approved / rejected 分頁、audit trail、每次決策可填理由 |
| **待辦清單(Backlog)** | Feature → Task / Bug / Spike 階層、Sprint 標籤、過期 badge、完整 CRUD;可由 `#backlog` route 或 testcase / report detail modal 內的 linked-todos popover 進入 |
| **設定 — RBAC / 成員 / 群組 / 邀請** | 權限清單、角色 CRUD(含 clone)、project-scope 角色 override、群組 CRUD(可巢狀、可作為 assignee)、組織成員、專案成員、邀請生命週期(送出 / 重送 / 延期 / 撤回 / 批次) |
| **Auth / SSO** | fastapi-users + argon2 PasswordHelper、Zoho OIDC via `httpx-oauth`、JWT 放 httpOnly cookie、refresh token、must-change-password gate、三欄首登 profile 設定 |
| **Audit / 觀測** | `audit_logs` middleware(SOC 2 baseline)、Fluent Bit → VictoriaLogs per-container 串流、可選 Prometheus + Jaeger(`--profile obs`) |
| **API gateway** | nginx → APISIX → backend;request-id、CORS、Per-IP rate-limit、Circuit breaker;backend port 不對外暴露 |
| **儲存** | 所有上傳走 SeaweedFS S3 API(啟動時強制檢查 `STORAGE_BACKEND=s3`) |
| **Mock 端點** | Per-org 的 mock API 註冊器 — 給還沒接好真實後端的測試用 |
| **本機 Agent(有頭模式)** | 在實體桌面瀏覽器上看著測試跑,不在 headless container 裡 |
| **Markdown import / export** | 整個專案樹可 round-trip 到 / 從 `.md` 檔 |
| **REST API** | 完整 Swagger;`/api/executions` 對 CI/CD 開放 — Jenkins / GitHub Actions / GitLab CI 都可以打 |

詳細使用教學見 [操作說明.md](操作說明.md)。

---

## <a id="tech-stack"></a> 技術堆疊

```
┌─────────────────────────────────────────────────────────────────┐
│            Web UI (Vanilla JS + Tailwind CDN, no build step)    │
│            按需 lazy-load Chart.js / Mermaid / html2pdf         │
└──────────────────────────────┬──────────────────────────────────┘
                               │ REST + WebSocket (port 80)
┌──────────────────────────────▼──────────────────────────────────┐
│   nginx (前門、SPA shell、/recorder/<id>/* WS reverse-proxy 到  │
│          動態 spawn 的 container)                                │
└──────────────────────────────┬──────────────────────────────────┘
                               │ /api/*  /ws/*  /pics/*  /results/*
┌──────────────────────────────▼──────────────────────────────────┐
│   APISIX  (request-id · CORS · rate-limit · api-breaker)        │
└──────────────────────────────┬──────────────────────────────────┘
                               │ proxy to backend:8000 (內部)
┌──────────────────────────────▼──────────────────────────────────┐
│         FastAPI (Python 3.11) · OIDC · slowapi · Fernet         │
└────────┬───────────────┬─────────────────┬──────────────────────┘
         │               │                 │
┌────────▼─────┐ ┌───────▼──────┐ ┌────────▼──────────────────┐
│ PostgreSQL 16│ │  Valkey 8    │ │ Celery worker             │
│ (資料)       │ │(快取+佇列)   │ │  → Robot Framework runner │
└──────────────┘ └──────────────┘ │  → Playwright recorder    │
                                  │  → mitmproxy API recorder │
┌──────────────┐ ┌──────────────┐ │  (每個在自己的短命容器)   │
│ SeaweedFS    │ │ Fluent Bit + │ └───────────────────────────┘
│ (S3, 媒體)   │ │ VictoriaLogs │
└──────────────┘ └──────────────┘
```

**預設 compose**(v1.1.9 後):11 個常駐服務 + `seaweedfs-init` 一次性
(postgres / valkey / seaweedfs / docker-proxy / backend / celery /
frontend / apisix / fluent-bit / victoria-logs / `seaweedfs-init`)。
**Profile 啟動**:2 個 obs 服務(Prometheus + Jaeger)、4 個 spawn-time
image(`robot-runner` / `recorder` / `recorder-api` / `mcp` — 建好後
backend 動態啟動)、1 個 bootstrap 一次性。

舊版的 Hermes / mem0 / mem0-postgres / openclaw sidecar 在 v1.1.9 全部
拿掉。

---

## 上線前硬化

在把 AutoTest 開放到 Internet 之前:

- `ALLOWED_ORIGINS` 設成你的前端 origin(**不要**用 `*`)。
- 覆寫預設密鑰 — `AUTOTEST_JWT_SECRET`、`AUTOTEST_FERNET_KEY`、
  `DB_PASSWORD`、`S3_ROOT_PASSWORD`。bootstrap profile 第一次跑會
  自動產一份隨機值;之後定期 rotate。
- 跑在 HTTPS 後面(用 reverse proxy + Let's Encrypt 或自己的 CA)。
- `RECORDER_IMAGE` 與 `ROBOT_RUNNER_IMAGE` 釘特定 tag 或 sha256;
  **不要用** `latest`。
- 排程備份 PostgreSQL 與 SeaweedFS volume。
- 漏洞通報流程見 [SECURITY.md](SECURITY.md)。

---

## FAQ

**Q:為什麼不直接用 TestRail / Zephyr / qTest?**
A:按人收費、測試資料放在別人的雲、export 格式不是專屬就是空殼。
AutoTest 全部跑在你自己的網路內,用的全是開放格式。

**Q:為什麼不直接用 Robot Framework + CI server?**
A:撰寫 UI、錄製、每步截圖 / 影片 / trace、歷史走勢圖、RBAC、
多租戶 scope、測試案例樹這些都不是 vanilla Robot Framework 提供的。
AutoTest 把它們組成一個產品,讓 QA 團隊有共同的 source of truth。

**Q:我想把被砍掉的 AI / 看板 / RTM / 缺陷功能加回來。**
A:可以從 v1.1.9 系列之前的 commit 開始。13 個功能被分成 13 個獨立
commit 拿掉(`6283deb..e64fc59` 區段),可以單獨 revert。被刪除的
entity table 在 DB 裡都還在,revert 不會丟資料。

**Q:這套是 enterprise-ready 嗎?**
A:v1.1 只有單租戶的 stop-gap guard。多租戶隔離、MFA、API token、
Helm chart 都在 roadmap 上。商業部署請開 issue 討論。

**Q:Apple Silicon (M1 / M2 / M3) 支援嗎?**
A:跑得起來(透過 Rosetta 2),但 recorder container 是 x86-64,
比原生 amd64 機器慢 2–4 倍。原生 arm64 image 在 roadmap 上。

---

## 貢獻

- Bug 回報 / 功能請求:[開 issue](../../issues)。
- 安全漏洞:見 [SECURITY.md](SECURITY.md)。
- 社群規範:見 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
- PR 歡迎 — 送 PR 之前請本地跑 `gitleaks`、`pip-audit`、`bandit`
  (CI 跑的也是這三個)。

---

## 授權

Apache License 2.0。完整條文與第三方相依授權見 [LICENSES.md](LICENSES.md)。

---

> 需要英文版?見 [README.md](README.md);完整使用教學見 [操作說明.md](操作說明.md)。
