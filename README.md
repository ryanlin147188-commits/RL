# RL — 自架自動化測試平台

> **Apache 2.0 開源、完全 self-hosted、可直接用於商業環境**
> 一份 `docker compose up` 就能跑起來的測試平台:BDD 案例 / 多模式錄製 / Robot Framework + Playwright 執行 / 缺陷管理 / 測試看版 / 跨組織協作。

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-v1.1.10-blue.svg)](#)
[![Docker](https://img.shields.io/badge/Deploy-Docker%20Compose-2496ED.svg)](https://docs.docker.com/compose/)
[![Self-hosted](https://img.shields.io/badge/Self--hosted-100%25-success.svg)](#)

---

## 為什麼選 RL

| 條件 | RL | TestRail / Zephyr / qTest / Jira |
|---|---|---|
| 授權費 | **零**(Apache 2.0) | 按人 / 按專案計費 |
| 部署位置 | **你的伺服器**,完全離線可運作 | SaaS,資料在外部雲端 |
| 商業使用 | **可直接商用**,可修改、可整合進產品、可內部部署收費 | 嚴格 EULA + 用量限制 |
| Telemetry / Phone-home | **無** | 通常有 |
| Vendor lock-in | **無**(資料在 PostgreSQL + Markdown) | 強(專有格式) |

只要保留 [LICENSE](LICENSE) + [NOTICE](NOTICE) 兩個檔案,你就可以:

- ✅ 拿來內部使用、修改、二次發布
- ✅ 整合進你公司的產品 / SaaS 服務並收費
- ✅ 部署在客戶 / 企業內網提供測試服務
- ✅ 不必把修改後的 source code 開源(Apache 2.0 不是 copyleft)

詳細條款請參見 [LICENSE](LICENSE)。第三方授權與商用稽核(60+ 元件)見 [LICENSES.md](LICENSES.md)。

---

## 快速開始(5 分鐘)

**前置需求**:Docker 24+、Docker Compose v2.23+。Linux / macOS / Windows(Docker Desktop)皆可。

```bash
git clone https://github.com/ryanlin147188-commits/RL-for-Kapito.git
cd RL-for-Kapito

# 1) 自動產生 .env(含隨機 secret;已存在則跳過)
docker compose --profile init run --rm bootstrap

# 2) 預先建置動態 spawn image(robot-runner + recorder),首次約 5–10 分鐘
docker compose --profile spawnable build

# 3) 啟動所有常駐服務
docker compose up -d --build
```

開啟 [http://localhost](http://localhost) → 任何人都可以點「**建立帳號**」自助註冊(免 email 驗證)。
第一個自架的人請註冊一個帳號當管理員,或用內建 `admin` / `admin123` 登入後從「設定 → 專案協作成員」分配權限。

完整使用教學請見 **[操作說明.md](操作說明.md)**。

---

## 功能總覽(v1.1.10)

**測試生命週期**

- 五層測試樹(Project / Feature / Page / Scenario / TestCase),BDD + KDT 視覺化編輯
- 動態運算式 `{{= expr }}` — env、變數、`uuid()`、`now()`、`fakerXxx()`、算術
- 條件分支(If / ElseIf / Else)、Capture 變數、前置案例鏈
- 資料驅動(DDT):內建資料表或獨立資料集
- 步驟 CSV / Excel / Markdown 雙向匯入匯出

**錄製 / 執行 / 報告**

- 三模式錄製:Web(Playwright + noVNC)、API(mitmproxy)、App(Appium)
- Robot Framework 7.x + Playwright 執行,每次跑獨立短命容器
- 即時 WebSocket log、逐步截圖、MP4 影片、Playwright Trace Viewer
- 執行報告 + 趨勢圖 + 視覺回歸基準 + 一鍵開缺陷

**協作與管理**

- **自助註冊**:免 email 驗證,自動有個人 Organization + 管理員角色
- **跨組織協作**:被邀請進其他人專案後 sidebar 看得到,權限以邀請方設定為主
- **退出專案 / 刪除帳號**:設定頁可自助退出某專案或永久刪帳號
- RBAC 三層權限(Global / Org / Project),per-project role override,群組可巢狀
- 本地帳號 + 可選 Zoho OIDC SSO,JWT httpOnly cookie

**追蹤與審核**

- 7-state 缺陷管理(NEW → ASSIGNED → ... → VERIFIED → CLOSED),Kanban + List 雙視圖
- 測試看版(待辦泳道,可連結 testcase / report / defect)
- 測試時程(Sprint / 階段 / 里程碑 Gantt 規劃)
- TestCase / Script / Report / Defect 四種實體的審核工作流
- 完整 Audit Log,所有 mutation 都有 actor / action / diff 紀錄

**比對與整合**

- 檔案比對(Excel / CSV 即時 diff,純客戶端)、畫面比對(截圖 pixel diff)
- 排程(ONCE / DAILY / WEEKLY / MONTHLY,每 30 秒掃描)
- REST API(200+ endpoints + OpenAPI / Swagger),`/api/executions` 可串 CI/CD
- 通知中心 + Email 通知(SMTP 設定)

---

## 技術棧

| 層 | 技術 |
|---|---|
| **前端** | Vanilla JS + 預編譯 Tailwind(無 build step) |
| **API Gateway** | FastAPI + httpx + slowapi 限速 + purgatory 熔斷 |
| **後端** | FastAPI(Python 3.11)+ Casbin RBAC + fastapi-users + PyJWT |
| **資料層** | PostgreSQL 16(WAL + streaming replication)+ Valkey 8 + SeaweedFS(S3 相容) |
| **執行引擎** | Robot Framework 7.x + Playwright(動態 spawn 容器) |
| **任務佇列** | Celery + Valkey(broker) |
| **部署** | Docker Compose(9 個常駐服務 + 2 個 spawn image) |

詳細架構圖、服務清單、安全強化建議見 [docs/ops/bootstrap.md](docs/ops/bootstrap.md) 與 [docs/ops/data-safety.md](docs/ops/data-safety.md)。

---

## 常用維運指令

| 操作 | 指令 |
|---|---|
| 查看容器狀態 | `docker compose ps` |
| 追蹤即時日誌 | `docker compose logs -f` |
| 停止(保留資料) | `docker compose down` |
| 完全重置(清除 DB) | `docker compose down -v` |
| 觸發手動備份 | `docker exec autotest-backup-cron sh /backup.sh` |
| 啟用 debug 模式 | `AUTOTEST_DEBUG=True docker compose up -d --force-recreate backend` |

> ⚠️ **絕對不要執行 `docker image prune -a`** — 會誤刪 backend 在 runtime 動態 spawn 的 `autotest-robot-runner` 與 `autotest-recorder` image。請改用 `docker image prune -f`(僅清 dangling)。

---

## 商業部署檢查清單

正式環境前請確認:

1. ✅ 覆寫所有預設密鑰:`AUTOTEST_JWT_SECRET`、`AUTOTEST_FERNET_KEY`、`DB_PASSWORD`、`S3_ROOT_PASSWORD`、`REPLICA_PASSWORD`(bootstrap 已自動產生隨機值,定期 rotate)
2. ✅ 設定 `ALLOWED_ORIGINS` 為真實 origin,**不要用 `*`**
3. ✅ 部署在 HTTPS reverse proxy 後(Let's Encrypt / Traefik / Caddy)
4. ✅ 釘定 image tag(`RECORDER_IMAGE` / `ROBOT_RUNNER_IMAGE` 不要用 `latest`)
5. ✅ Docker log rotation(`/etc/docker/daemon.json` 設 `max-size: 10m`)
6. ✅ 備份策略:`backup-cron` 每日 03:00 自動跑(從 replica `pg_dump` + SeaweedFS tar,保留 7 天);可另外設 S3 鏡像
7. ✅ Zoho SSO(若需要):`.env` 加 `ZOHO_CLIENT_ID` / `ZOHO_CLIENT_SECRET` / `ZOHO_REDIRECT_URL`,重啟 backend

完整安全政策見 [SECURITY.md](SECURITY.md)。

---

## 文件導覽

| 文件 | 用途 |
|---|---|
| [操作說明.md](操作說明.md) | **使用者操作手冊**(註冊 / 撰寫 / 錄製 / 執行 / 報告 / 協作) |
| [LICENSE](LICENSE) | Apache License 2.0 授權正本 |
| [NOTICE](NOTICE) | Apache 2.0 必要 attribution + 第三方相依套件 |
| [LICENSES.md](LICENSES.md) | 第三方授權整理 + SaaS 商業使用稽核 |
| [SECURITY.md](SECURITY.md) | 安全漏洞回報政策 / 安全部署建議 |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | 貢獻者行為準則 |
| [docs/ops/bootstrap.md](docs/ops/bootstrap.md) | 首次部署 SOP / 預設帳號 / Zoho SSO / 自簽憑證 |
| [docs/ops/data-safety.md](docs/ops/data-safety.md) | 資料安全 / 備份機制 / 還原流程 |

---

## 貢獻

- Bug / 功能請求:[開 issue](../../issues)
- 安全漏洞:見 [SECURITY.md](SECURITY.md)
- PR 歡迎 — 送出前請對前端跑 `node --check frontend/index.html`,後端跑 `python -m py_compile`

---

## 授權

```
Copyright 2026 Ryan Lin (Kapito)

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```

**商業使用提醒**:Apache 2.0 允許商業使用,但要求在散佈時保留 [LICENSE](LICENSE) 與 [NOTICE](NOTICE) 兩個檔案的內容。詳細條件請逕行閱讀 LICENSE 全文。

---

> 完整使用教學請見 **[操作說明.md](操作說明.md)**
