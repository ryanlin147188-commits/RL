# RL — Enterprise Test Automation Platform

> 🌐 **Languages**: [English](README.md) · **繁體中文**

> **一個平台,涵蓋整條測試生命週期,還會自己「動手」測試。**
> 用業界標準開源技術(Robot Framework + Playwright + Appium),取代 Selenium IDE + Postman + Jira + TestRail + Allure 各自分散的工具鏈。
> **Apache 2.0 全開源,自架到內網即可,內建 AI 助理可直接操作瀏覽器產生測試案例。**

[![License](https://img.shields.io/badge/License-Apache%202.0-green.svg)](LICENSES.md)
[![Robot Framework](https://img.shields.io/badge/Engine-Robot%20Framework%207.x-blue.svg)](https://robotframework.org/)
[![Docker](https://img.shields.io/badge/Deploy-Docker%20Compose-2496ED.svg)](https://docs.docker.com/compose/)
[![Stack](https://img.shields.io/badge/Stack-FastAPI%20%2B%20PostgreSQL%20%2B%20SeaweedFS-0a7e07.svg)](#-技術架構)
[![AI](https://img.shields.io/badge/AI-Hermes%20ACP%20%2B%20mem0%20%2B%20MCP%20%2B%2011%20providers-7c3aed.svg)](#-ai-原生:平台會自己寫案例-自己跑測試)

---

## 30 秒看懂

| 你的問題 | RL 的解法 |
|---|---|
| **「QA 寫 Selenium、PM 看 Confluence、Bug 在 Jira、報告在 Allure 各自孤島」** | 一個平台:案例 / 排程 / 執行 / 報告 / 缺陷 / Backlog / RTM / 待辦 / 群組 / 版號 全串通 |
| **「商業 SaaS 一條 user $20–$100 / 月,50 人就 $5K–$60K / 年」** | **零授權費**(Apache 2.0)、自架到內網、**完全沒有 user-based pricing** |
| **「合規:測試資料、截圖、影片、AI 對話不能傳第三方」** | 全棧自架(PostgreSQL / SeaweedFS / Valkey),AI 可改用本地 Ollama / LM Studio,**資料完全不離開內網** |
| **「QA 寫案例慢、AI 寫的 case 不會跑、需要人工再轉」** | ✨ **AI 助理直接生 `steps_json`**(已驗證的執行單元),一鍵套用即可跑;**MCP 模式** AI 還能直接操作瀏覽器產生案例 |
| **「Playwright 錄製只能錄死流程,不會抓變數、不會分支、不會帶條件」** | ✨ **動態運算式 / Capture step / If-ElseIf-Else / 視覺增強錄製**,平台轉成 RF 5.0 IF/ELSE/END 一鍵變條件式測試 |
| **「想接 CI/CD 但 SaaS API 限速、要付 enterprise 加價」** | **API-First** + Swagger;`/api/executions` 開放呼叫,無速率限制 |
| **「用了商業 SaaS 之後,案例匯出來不是專屬格式就是空殼」** | 案例 = Markdown,環境 = `.env`,Robot file 標準語法,**100% 可遷移** |

---

## 🤖 AI 原生:平台會自己寫案例、自己跑測試

RL **不是** 把 ChatGPT 嵌進對話框就叫 AI 化的傳統測試工具。
v1.1 內建 **四條 AI 生產線**,把 LLM 當作平台第一公民:

### 1️⃣ Hermes AI 助理 ⚡ 一鍵生案例 + 持久記憶
對話層改用 [hermes-agent](https://github.com/NousResearch/hermes-agent) 透過 ACP 協定跑 per-user 子進程,每個使用者一個獨立 LLM context:
> 「我要測購物車從加入到結帳的完整流程」
- 內建多輪 tool calling(OpenAI / Anthropic / Google 三家統一 schema)
- 直接吐出 `steps_json` schema 化結構,**不是純文字**,平台立即可跑
- **持久語意記憶**(走 [mem0](https://github.com/mem0ai/mem0) sidecar):per-user pgvector 庫、對話後自動抽 fact、LLM 對話中可主動 invoke `search_memory` MCP tool 回查偏好
- 套用到當前案例 / 開新 SCENARIO 建新案例兩種選擇

### 2️⃣ AI 直接操作瀏覽器(Playwright MCP)
透過 [Model Context Protocol](https://modelcontextprotocol.io/) 串接 [Playwright MCP](https://github.com/microsoft/playwright-mcp),AI 變成可操作的 agent:
- **per-user MCP 容器**:每個使用者開獨立 chromium,互不打架
- **multi-turn tool calling 迴圈**:LLM → call browser tool → 看截圖 → 決定下一步 → 直到任務完成
- **即時中止**:任何時候按「停止」立刻 cancel asyncio task,不會留殭屍容器
- **背景 idle sweeper**:閒置 MCP 容器自動回收,不浪費資源
- **使用情境**:跟 AI 說「請打開公司官網點客服按鈕填表單」→ AI 自己點完後產生案例

### 3️⃣ AI 增強錄製(Vision)
錄製完的 trace 可一鍵餵給支援 vision 的 LLM(GPT-4o / Claude 3.5 Sonnet / Gemini):
- 抽 trace.zip 內的 screenshot → 連同操作序列丟給 LLM
- LLM 推斷使用者意圖 → 自動加 **多條件斷言、Capture 變數、If/ElseIf 分支**
- 結果以 **diff view** 呈現,逐 step 接受 / 拒絕,不會直接污染原稿
- 全程記入 audit log:「使用者接受了哪幾條 AI 建議」可追溯

### 4️⃣ 持久語意記憶(mem0 sidecar)
獨立 `mem0` 容器(FastAPI + pgvector),每個使用者各自的長期記憶:
- **Pre-hook 自動召回**:每次 send_message 前先跑 `mem0.search`,top-5 過往記憶以 `<recalled_memory>` 注入 prompt
- **Post-hook fire-and-forget 寫入**:對話完 LLM 自動抽 atomic fact,mem0 dedup + 落 pgvector
- **`search_memory` MCP tool**:LLM 對話中也可以主動回查(例:「我之前有沒有提過 staging URL?」)
- **per-user 隔離**:`org_id:username` partition key、X-Mem0-User-Id header 由 backend 設定,不能被 LLM tool args 偽造
- **Graceful degrade**:circuit breaker、5s timeout、cache miss 回 friendly text;主對話絕不被 mem0 故障擋住

### 🎯 11 家 LLM provider + 完全本地化選項
| 雲端 | 本地 / 自架 |
|---|---|
| OpenAI · Anthropic · DeepSeek · Groq · OpenRouter · Together AI · Mistral · xAI · Google Gemini | Ollama · LM Studio · 自架 OpenAI-compatible 端點 |

- **「用 token 拉模型清單」** 按鈕:輸入 API key 一鍵列出該 provider 全部可用模型
- 自動偵測推理模型(o1 / o3 / GPT-5 / DeepSeek-R1 等)→ 啟用「思考程度」(low / medium / high)
- API key / model id / 自架 base_url **Fernet 加密落地**,從不明文存 DB

#### mem0 記憶層 provider 對應

| 主對話 LLM | Embedder | 說明 |
|---|---|---|
| OpenAI | OpenAI `text-embedding-3-small` | 同把 token,不需額外設定 |
| Gemini | Gemini `text-embedding-004` | 同把 token,不需額外設定 |
| Anthropic (Claude) | OpenAI / Gemini fallback(同 org 任一把) | Anthropic 沒 embedder API — backend 自動挑同 org 內最便宜的 OpenAI/Gemini token 當 embedder。**設了 Claude 的同時加一把 OpenAI token 就能解鎖記憶功能** |
| 純 Anthropic(沒 fallback) | — | 自動跳過記憶功能,主對話照常 |

---

## 為誰打造

RL 鎖定 **15–500 人規模、有自動化測試需求但被工具鏈拖累** 的軟體團隊:

### ✅ 適合
- **金融 / 政府 / 醫療**:資料合規敏感、需要 air-gap 部署、SaaS 不能用、AI 必須走本地 Ollama
- **製造業 / IoT**:測試對象在內網設備、SaaS 連不到
- **新創 / 中型 SI**:預算有限但要完整測試平台 + AI 加速,不想付 SaaS 學費
- **多客戶顧問公司**:一套平台多客戶共用,內建 Organization 多租戶 + 管理員集中管帳號 + audit log

---

## ROI 試算(實例)

> 假設 50 人團隊,4 名 QA,每年跑 3 次 release,加 AI 寫測試案例 + 自動執行的人力節省。

| 項目 | 商業 SaaS(TestRail + Tricentis + ChatGPT Team)| RL |
|---|---|---|
| 平台授權費(年) | $50,000–$120,000 | **$0** |
| AI 工具訂閱(50 人 × $25/mo) | $15,000 / 年 | **$0**(自帶 11 家 provider 切換)|
| QA 寫案例人力(估 30% 由 AI 生成) | 不變 | **節省 30%** = 約 **$36K / 年** |
| 上手 / 培訓 | 2 週 × 4 人 ≈ $20K | 1 天上手 ≈ **$3K** |
| 每年廠商升級被迫跟進 | $5K–$15K | **$0**(自己控節奏)|
| 廠商鎖定造成的遷移風險 | 換系統就重建 | **隨時 fork、案例直接帶走** |
| **3 年 TCO** | **$280K–$520K** | **$3K + 維運人力** |

維運人力:後端跑在 Docker Compose,1 名兼職運維 0.1 FTE 即可。

---

## vs. 主流商業 SaaS

| 維度 | TestRail / Zephyr / qTest | Tricentis / Katalon | **RL** |
|---|---|---|---|
| 部署 | SaaS only | SaaS / On-Prem(高價)| **自架(Docker Compose)** |
| 價格(50 人)| $30K–$60K / 年 | $50K–$200K / 年 | **$0** |
| AI 生成案例 | ❌ / 加價購 | ✦ 限定模型 | ✅ **11 家 provider 切換 + 本地 Ollama** |
| AI 直接操作瀏覽器 | ❌ | ❌ | ✅ **MCP + Playwright,per-user 容器隔離** |
| AI 視覺增強錄製 | ❌ | ❌ | ✅ **trace.zip 截圖 → LLM diff view** |
| 條件式測試(if/else)| 程式式 (DSL) | 程式式 (DSL) | ✅ **flat step 結構,RF 5.0 IF/ELSE/END** |
| 動態運算式 / 變數綁定 | 受限 | 受限 | ✅ **`{{= 表達式 \| filter}}` mini DSL,AST 白名單** |
| 廠商鎖定 | 專屬 DSL + SDK | 專屬 IDE + 腳本格式 | **Robot Framework 標準語法** |
| 資料主權 | 廠商雲端 | 廠商雲端(可選自架)| **完全在你的伺服器** |
| 可程式化 | 受限 API | 受限 API + 加價 | **完整 REST + WebSocket,無速率限制** |
| 案例匯出 | 廠商專屬 | 廠商專屬 | **`.md` + `.robot` 標準** |
| 升級路徑 | 跟廠商走 | 跟廠商走 | **隨時 fork、社群版本** |

---

## ✨ 核心能力

| 類別 | 能力 |
|---|---|
| 🤖 **AI 原生** ✨ | AI Chat ⚡ 一鍵生 steps_json、MCP 直接操作瀏覽器、Vision 增強錄製、11 家 provider + 本地、推理模型自動偵測思考程度 |
| 🧬 **動態 / 條件式測試** ✨ | `{{= ${count}+1 \| upper}}` mini DSL、Capture step(text / attr / json path)、If / ElseIf / Else / EndIf 平面分支、AST 白名單防 injection |
| 🎬 **錄製器雙模式** ✨ | **本機**(終端機 codegen)+ **Docker**(noVNC iframe 直接操作)雙軌;結束自動轉 steps,可選「主動建案」或「自動建案模式」 |
| 🧩 **不寫程式建測試** | 100+ 原子動作關鍵字、12 種比對運算子、28 種 Faker 隨機資料 |
| 📄 **Markdown 為原生格式** | 每個案例都能匯出 `.md` — 進 git、過 PR review、版本控管、CLI 獨立執行 |
| 🌐 **跨 5 平台單一體驗** | WEB UI / HTTP API / 手機 App / SQL DB(7 種)/ E2E,**一份案例、一份報告** |
| 📱 **API / APP 錄製** | mitmproxy Docker mode 抓 HTTP,Appium script 解析自動轉步驟 |
| 🔁 **DDT 資料驅動** | 同案例跑多組資料、每列獨立錄影 + Trace |
| 📊 **完整 RTM 追溯鏈** ✨ | User Story → AC → TestCase → Defect 一頁看穿;**Backlog Task 可橫向連結 10 種實體**(含測試版號)+ link_kind 語意(verifies / blocks / duplicates)+ 反向視圖在每個 entity detail modal |
| 🎯 **跨 entity 指派系統** ✨ | 統一 schema 涵蓋 6 種 entity(defect / todo / testcase / requirement / document / review),群組指派自動 fan-out 通知,bulk reassign(≤200/call),「我的工作」inbox 個人視圖 |
| 📋 **統一 7 欄看板 + 拖移變更狀態** ✨ | 8 個 entity 的 status 統一成 `新建立 / 等待處理 / 進行中 / 等待審核 / 退回修改 / 已驗證 / 已關閉` 7 個值;看板用同一套欄位顯示;**defect / todo / requirement 卡片可拖到其他欄變更狀態**(樂觀更新 + 失敗回滾);卡片顯示優先級 / Blocked / Module / Type / Assignee / 到期日 |
| 🏢 **多租戶 + 集中帳號管控** ✨ | Organization 隔離、**禁止自助註冊**(統一由管理員建帳號)、**預設 admin/admin123 強制首登改密**、忘記密碼 email 重置連結、群組可巢狀 + 可當 Todo assignee(後端保留供「指派 todo 給群組」共用) |
| 🛡 **AB 表設計 + 完整審核工作流** ✨ | 6 種業務 entity 全 mirror 進 `entity_versions` 快照表(JSONB)+ `content_status`(`ai_draft / pending_review / approved / rejected`),change_source 區分 `human / ai / system / revert`,任意版本一鍵還原 + parent 追溯;AI 寫入直接落 ai_draft,review 通過自動 flip approved |
| 🏷 **測試版號追蹤** ✨ | WEB / API / APP 版號獨立管理,測試報告 / 缺陷 / 回合反向 FK 連動,**待辦可連結到版號**,清楚「這 bug 是哪個版本爆的、哪些 todo 在追蹤」 |
| 🎯 **多方法論支援** | ATDD / BDD / KDT / DDT / TDD / SBE / FDD 都能在平台內自然表達 |
| 🏗 **完整 ALM** | 測試計畫(ISTQB 8 區塊)/ 需求 / 缺陷 / 里程碑 / WBS / 文件 / Backlog / 排程 / 通知 |
| 🔐 **企業級 Auth** | JWT 雙 token + 主動換發、bcrypt 密碼、Fernet 加密 secret(含 AI key / DB pwd / SMTP pwd)、大頭貼上傳、角色權限矩陣、OIDC 整合 |
| 🌍 **雙語雙主題** | 繁體中文 / English 一鍵切換、亮 / 暗主題自動記憶 |

---

## 🆕 v1.1.1 — 助理 × 平台動作工具 × 真瀏覽器

v1.1.0 把 Hermes ACP sidecar + mem0 語意記憶接進來,v1.1.1 把這條鏈**真的串通**了:

- **Platform MCP server**(新)— backend 自掛 `/platform-mcp/mcp` FastMCP 子 app,把
  專案 / 測試案例 / 缺陷 / 文件 / 需求 / 時程 / 版號 / 計畫 / 待辦 / 錄製 / 執行
  共 **27 個** action 露給 Hermes LLM。使用者說「幫我建 Kapito 專案」→ 助理直接
  `create_project` 而非反問技術棧細節。
- **Per-user Playwright MCP** — Hermes provision 時自動 spin `autotest-mcp`
  container,LLM 收到 22 個 `browser_*` tool(navigate / click / type / snapshot /
  get_images / 等),真的能操作瀏覽器探索網站、產生案例、執行驗證。
- **`platform_help(topic?)` 知識庫** — 不污染 mem0 個人記憶,把「平台有什麼功能」
  做成助理隨時可 query 的 module-level 字典。
- **執行串接** — `execute_testcase` / `get_execution_status` / `list_executions`
  從助理一句話跑完整條 docker 模式測試。
- **語言追隨平台 i18n** — 前端 fetch wrapper 帶 `Accept-Language`(zh-TW / en),
  backend 在每輪訊息前注入 `<language_directive>`,使用者切語言**即時生效**,
  不必 reprovision Hermes session。
- **助理 UI 簡化** — 移除排程任務 / LLM 串接 / 暫停記憶 / 案例工具列等進階入口,
  「AI 助理」改名為「助理」,Enter 不再誤送、改點傳送鈕。
- **錄製鏈修補** — `start_recording_session` 透過 MCP 建 DB row,`convert_recording_to_steps`
  解析 Playwright codegen / HAR 為 step 陣列。
- **多輪 bug 修法**(全部已 ship):`0005` migration 對 fresh DB 的 ai_conversations 索引修正、
  全 API auth 流程的 401/403 + must_change_password URL clear、`POST /api/hermes/sessions`
  的 provider mapping(OpenAI → custom + base_url + api_mode=chat_completions)、
  Playwright MCP 的 Streamable HTTP `initialize` handshake、Docker Desktop bind-mount
  舊 inode 截斷修補。
- **AI Token 模型清單** — 過濾掉 whisper / dall-e / embedding / tts 等非 chat 模型。
- **平台限制邊界**(雙層) — `acp_lockdown.py` monkey-patch 把 `web` / `terminal` /
  `file` / `code_execution` / `delegation` 等跳出平台的 toolset 從 LLM tool list 整批
  拿掉;system_prompt 第二道防線教 LLM 拒絕越界要求。

升級提醒:此版本含 0001 → 0018 共 18 條 alembic migration。**fresh DB 部署**直接
`./deploy.sh`(會跑 alembic upgrade head);**舊 v1.1.0 升級**請先停 stack、`docker
compose pull` / `build`、再 up,backend lifespan 會自動 alembic upgrade。

---

## v1.0 連續 7 輪 UX 強化(A → G)

進入 v1.0 後針對「使用者每天會碰到的痛點」做 7 輪密集打磨,全部已 ship 到 main:

| Tier | 主軸 | 重點 |
|---|---|---|
| **A** | 6 個設定分頁 baseline | search / sort / unified loading-empty-error states / cascade-aware delete confirms |
| **B** | 進階清單 + 批次操作 | pagination 後端參數、role usage 統計、role clone、bulk role assignment |
| **C** | 跨分頁協同新功能 | 權限反向查詢 drawer、Email domain 預覽器、完整邀請 lifecycle UI、群組 → 專案橋接、加成員多選 + search |
| **D** | 指派系統翻新 | TodoItem schema 對齊、群組 fan-out 補完、bulk reassign + stale 偵測、「我的工作」 inbox、picker 改造、4 個 native prompt 全換成 form modal |
| **E** | 收尾完整覆蓋 | bulk reassign 推到 testcase / review 清單、`/me?entity_type=todo` API 修 |
| **F** | 多 entity 看板 | 看板從只看 defect 變成跨 6 種 entity,接到 Tier D 指派系統 |
| **G** | 待辦連結機制完善 | TodoLink 支援測試版號、link_kind 語意、5 個 detail modal 加反向視圖、bulk-from-targets(從 N 個 entity 一鍵建追蹤待辦)、連結通知 |
| **H** | **狀態統一 + 拖移看板 + 側欄重組** | 8 個 entity 的 status enum 全部對齊成統一 7 值工作流(`New → Assigned → InProgress → InReview → (Verified \| ReworkRequired \| Closed)`);新增 `0011_unify_status` + `0012_unify_status_part2` 兩支可逆 alembic migration 自動轉換舊資料;routers 加 legacy normalize map;Python enum 別名讓舊 `.DRAFT / .APPROVED / .ACTIVE` 等引用照舊有效。**看板支援 defect / todo / requirement 拖移變更狀態**;卡片顯示 Blocked / Rejected / Priority / Module / Type / Assignee / 到期日 等 metadata。**側邊欄與首頁快速導覽**重組為 5 大類別(專案管理 / 測試設計 / 測試環境 / 執行中心 / 品質追蹤)。 |

每輪都是 backend additive(不破壞既有 API)+ 前端漸進升級,**沒有 schema breaking change**(D-1 唯一一次 column rename + H 的 enum→VARCHAR 都以 alembic migration 可逆處理)。

---

## 5 分鐘上線

**前置**:Docker 24+ 與 Docker Compose v2.23+。下列指令在 Linux / macOS / Windows(Docker Desktop)完全相同 — 不需要為平台分別寫部署腳本。

```bash
git clone https://github.com/ryanlin147188-commits/RL_TMP.git && cd RL_TMP

# 1) 產生帶隨機 secret 的 .env(若已存在 .env 會跳過,不覆寫)
docker compose --profile init run --rm bootstrap

# 2) 預 build 四個 spawn-time image(Robot runner / 錄製 web / 錄製 api / MCP)
#    這些是 backend 在 runtime 動態 `docker run` 出來的 per-session 容器,
#    不是常駐 service,但 image 必須先存在。第一次約 5-10 分鐘。
docker compose --profile spawnable build

# 3) 啟動主服務
docker compose up -d --build

# (可選) 4) 改 seed admin 的初始密碼;沒設就用內建的 admin/admin123
# echo "AUTOTEST_DEFAULT_ADMIN_PASSWORD=Op3rator-Init" >> .env
```

完成後到 <http://localhost>,用 `admin` / `admin123`(或你 override 的密碼)登入。
**第一次登入會被強制改密碼**(後端閘擋,前端跳 modal),改完才能用其他功能。
之後新帳號統一在「設定 → 專案協作成員」 由管理員建立(本系統禁止自助註冊)。

### 日常維運

| 想做… | 指令 |
|---|---|
| 看容器狀態 | `docker compose ps` |
| 看即時 log | `docker compose logs -f` |
| 停掉(保留資料) | `docker compose down` |
| 完全重置(**會清光 DB + S3**) | `docker compose down -v` |
| 啟用觀察性堆疊(Prometheus + Jaeger) | 任何指令加 `--profile obs` |
| Dev 模式(DEBUG,不暴露額外 host port) | `AUTOTEST_DEBUG=True docker compose up -d --force-recreate backend` |

預設啟動為 **10 個常駐容器 + `seaweedfs-init` one-shot**;加上 `--profile obs` 後為 **12 個常駐容器 + `seaweedfs-init` one-shot**。Docker Desktop 可能另把 compose app 群組算成 1 個 item,所以畫面上可能看到 12 / 14 items。

> 📖 **完整教學**(從建專案 → 寫案例 → 用 AI 生案例 → 跑測試 → 看報告)請見 **[操作說明.md](操作說明.md)**

### 不想本機 build?用預先打包的 image

從 [GitHub Releases](https://github.com/ryanlin147188-commits/RL_TMP/releases) 下載 `autotest-images-1.1.1.tar`(離線散佈包,含 backend / celery / runner / mcp / frontend 等 image),在你的 VM 上:

```bash
# 1) 載入 image(2.6 GB,需要幾分鐘)
docker load -i autotest-images-1.1.1.tar

# 2) 取得單一 docker-compose.yml + apisix/、fluent-bit/ 設定檔(repo 根目錄裡都有)
git clone https://github.com/ryanlin147188-commits/RL_TMP.git && cd RL_TMP

# 3) 啟動(使用已載入 image,不做本機 build)
docker compose up -d --no-build
```

`docker compose ps` 應全綠。瀏覽器到 VM IP 即看到登入頁。

---

## 🎯 一個平台,七種主流測試方法論

| 方法論 | 平台支援 | 對應功能 |
|---|---|---|
| **ATDD** 驗收測試驅動 | 每案例獨立的「驗收準則 (AC)」+「前置動作 (Pre-Setup)」區塊 | 編輯器四區塊之一 |
| **BDD** 行為驅動 | `Given / When / Then / And / But` 關鍵字下拉 + 可讀步驟描述 | 步驟表第一欄 |
| **KDT** 關鍵字驅動 | 內建 100+ 動作關鍵字,完全免寫 code | 步驟表「動作」下拉 |
| **DDT** 資料驅動 | DDT 資料表 + `${變數}` 自動替換 + 逐列展開,**每列獨立錄影 + Trace** | 編輯器底部 DDT 區 |
| **TDD** 測試驅動開發 | 步驟層級 Pass/Fail 即時回饋 + 失敗精準定位 | WebSocket 即時日誌 |
| **SBE** Specification by Example | DDT 列出例子、AC 描述規則,兩者對應後可同時被執行 | AC + DDT 組合 |
| **FDD** 功能驅動開發 | 5 層樹 Feature → Platform → Page → Scenario → TestCase + 測試回合 | 左側目錄樹 + 測試回合 |

---

## 🏗 整合的 ALM 工作流

| 模組 | 功能 |
|---|---|
| **Backlog 待辦** ✨ | Feature → Task / Bug / Spike 階層 + Sprint label;**可連結 10 種實體**(需求 / 案例 / 缺陷 / 文件 / **測試版號** / WBS / 計畫 / 回合 / 里程碑 / 專案)+ **link_kind 語意**(verifies / blocks / duplicates / relates_to);指派可指向使用者或群組;**從 N 個 entity 一鍵 bulk 建追蹤待辦** |
| **「我的工作」 inbox** ✨ | 個人視圖,跨 6 種 entity 列出所有指派給我的工作;KPI(過期 / 今日到期 / 全部)+ entity-type tab 切換 + 點擊跳到對應詳情頁 |
| **群組管理** ✨ | 設定頁分頁,可巢狀(parent_id),Todo / 缺陷 / 案例 / 需求 / 文件 / 審核 都可選群組為 assignee → 自動 fan-out 通知所有成員(含子群組去重);**群組 → 專案橋接**:整個群組一鍵加入專案成員 |
| **需求 + RTM** | User Story → AC 階層,**RTM 追溯鏈** 在每個節點顯示 linked Backlog,完整可視化 |
| **缺陷管理** ✨ | **統一 7 值狀態工作流**(`新建立 → 等待處理 → 進行中 → 等待審核 →(已驗證 \| 退回修改 \| 已關閉)`)+ 嚴重性 + 附件 + 「關聯測試案例」下拉,自動納入 RTM 鏈;**可標記發生於哪個測試版號**;清單支援 bulk reassign + bulk 建立追蹤待辦;detail modal 顯示「相關待辦」 |
| **測試版號** ✨ | 設定頁分頁,WEB / API / APP 三軌獨立管理;版號連動測試報告 / 缺陷 / 回合;**待辦可連結回版號**,detail modal 顯示「相關待辦」 |
| **WBS** | 工作分解結構 + 進度百分比 + 依負責人篩選 |
| **測試計畫** | ISTQB 8 區塊格式(Scope / 策略 / 資源 / 時程 / 風險 / 入出條件 / 簽核)|
| **測試時程** | 里程碑 + Gantt 風格時間軸 |
| **測試回合** | 命名集合彙總執行,單一報告 |
| **測試看版 (Kanban)** ✨ | **多 entity 看板 + 統一 7 欄 + 拖移變更狀態**:全部 / 缺陷 / 待辦 / 案例 / 需求 / 文件 / 審核 7 個 tab + 「我的指派 / 全部」 toggle;所有 entity 都用統一 7 欄(`新建立 / 等待處理 / 進行中 / 等待審核 / 退回修改 / 已驗證 / 已關閉`);**defect / todo / requirement 卡片可拖到其他欄即時變更狀態**(樂觀更新 + 失敗回滾);卡片顯示 Priority / Blocked / Module / Type / Assignee / 到期日 等 metadata + 過期紅標 + 「重新指派」按鈕 |
| **審核中心** ✨ | 4 種類型(testcase / document / script / report)的送審 → approved / rejected workflow;清單支援 bulk reassign |
| **通知中心** | 站內紅點 badge + Email(per-event channel)+ toast 訊息歷史 |
| **多租戶 + 集中帳號管控** ✨ | Organization 隔離,**禁止自助註冊**:首次部署自動 seed `admin/admin123`(可透過 `AUTOTEST_DEFAULT_ADMIN_PASSWORD` env var 覆蓋)+ 強制首次登入改密碼;之後新帳號統一在「設定 → 專案協作成員」由管理員建立 / 編輯 / 重設密碼 / 刪除;忘記密碼走 email 重置連結(token 1 小時有效,單次使用) |
| **WBS v1 階層 + Task 連結** ✨ | Feature → WorkPackage → Task 三層自動推斷 + Task 葉節點可橫向連結到 4 種 entity(任務 / 測試案例 / 缺陷 / 執行紀錄),樹狀視圖即時顯示連結 count badge |
| **AI 助理 + AI Token** ✨ | 11 家 provider 切換、用 token 拉模型清單、推理模型思考程度自動偵測;Fernet 加密落地 |
| **使用者帳戶** | 大頭貼上傳(SeaweedFS,5 MB 內)、改顯示名稱 / Email / 角色;JWT 雙 token + 主動換發、bcrypt 密碼、Fernet 加密 secret |

---

## 🛠 業界標準開源技術棧

完全建立在開源、社群活躍、人才好找的技術之上 — **不會把團隊鎖在廠商 DSL**:

| 元件 | 版本 | 用途 |
|---|---|---|
| Robot Framework | 7.x | 測試引擎、`.robot` 語法、log.html / report.html、IF/ELSE/END 分支 |
| Browser Library | 19.x | Playwright 底層,trace + video + auto-wait |
| RequestsLibrary | pinned in runner image | HTTP API(GET / POST / PUT / PATCH / DELETE)|
| DatabaseLibrary | pinned in runner image | SQL(MySQL / PostgreSQL / MSSQL / Oracle / SQLite / MongoDB / Redis)|
| AppiumLibrary | pinned in runner image | iOS / Android 自動化 |
| Playwright MCP | 0.0.69 | LLM tool calling 直接操作瀏覽器(Anthropic Model Context Protocol)|
| mitmproxy | 12.2.2 | API 錄製 Docker mode,自動轉 Http.* steps |
| Markdown | — | 案例原生格式,`run_tests.py` 可 CLI 直接執行 |

整套 stack 為 **Apache 2.0 / BSD-3 / PostgreSQL License** — 商業 SaaS 部署無授權義務。詳見 [LICENSES.md](LICENSES.md)。

---

## ⚙ 部署模式

### 純 Docker Compose(推薦)

三個指令完成首次部署,跨平台行為一致(Linux / macOS / Windows Docker Desktop):

```bash
docker compose --profile init run --rm bootstrap     # 1) 產 .env(含隨機 secret)
docker compose --profile spawnable build             # 2) 預 build 4 個 spawn-time image
docker compose up -d --build                         # 3) 啟動主服務(自動 seed admin/admin123,首登強制改密)
```

**日常維運指令**:

| 用途 | 指令 |
|---|---|
| 容器狀態 | `docker compose ps` |
| 即時 log | `docker compose logs -f` |
| 停止(保留資料)| `docker compose down` |
| 重置(**清空所有資料**)| `docker compose down -v` |
| 啟用 obs(Prometheus + Jaeger)| 任何指令加 `--profile obs` |
| Dev DEBUG 設定 | `AUTOTEST_DEBUG=True docker compose up -d --force-recreate backend` |

### 系統需求

- Docker 24+ / Docker Compose v2.23+
- 4 GB RAM(建議 8 GB,若大量用 MCP 容器建議 16 GB)
- 10 GB 磁碟(初始,加 MCP image 約多 1.5 GB)
- Windows / macOS / Ubuntu / Linux

> 📖 **完整部署流程**(`.env`、跨平台指令、本機開發、升級)請見 **[操作說明.md](操作說明.md)**
> 📖 **REST API**:OpenAPI JSON 經 gateway 取得 <http://localhost/api/openapi.json>;互動 Swagger UI 可在 backend 容器內查 `docker compose exec backend curl localhost:8000/docs`

---

## 🏗 技術架構

```
         ┌──────────────────────────────────────────┐
         │   使用者瀏覽器(單頁 HTML/JS,無 build step)│
         │   Chart/Mermaid/html2pdf 用到時才 lazy load │
         └─────────────────┬────────────────────────┘
                           │ port 80
                      ┌────▼────────┐
                      │   nginx     │ ← SPA shell + /recorder/<id>/* WS 反代
                      └────┬────────┘
                           │ /api /ws /pics /results
                      ┌────▼─────────┐
                      │   APISIX     │ ← request-id · CORS · rate-limit · breaker
                      │  (API GW)    │
                      └────┬─────────┘
                           │ proxy_pass backend:8000(內網,host 不對外)
            ┌──────────────┼──────────────────────┐
        ┌───▼────┐     ┌───▼───┐    ┌─────▼─────┐
        │FastAPI │     │Celery │    │ WebSocket │
        │ (REST) │     │Worker │    │ 即時日誌   │
        └──┬─────┘     └──┬────┘    └───────────┘
           │              │ spawn(每 session 一個容器,跑完自毀)
           │              ▼
           │     ┌──────────────────────┐
           │     │ robot-runner         │  ← Robot Framework + Playwright
           │     │ recorder / recorder-api │ ← noVNC iframe / mitmproxy
           │     │ playwright-mcp       │  ← per-user 隔離 + idle sweeper
           │     │  (4 個 spawn-time image,docker compose
           │     │   --profile spawnable build 預先 build)
           │     └──────────────────────┘
           │
           ▼
      ┌──────────┐  ┌───────────────┐  ┌────────────┐
      │PostgreSQL│  │ SeaweedFS     │  │ Valkey     │
      │ 16       │  │ S3-compatible │  │ 8(快取 +   │
      │(全部資料)│  │ 強制 STORAGE  │  │  Celery     │
      │          │  │ _BACKEND=s3   │  │  broker)    │
      └──────────┘  └───────────────┘  └────────────┘

       ┌─────────┐  ┌─────────────┐  ┌─────────────────────┐
       │FluentBit│  │VictoriaLogs │  │ Prometheus + Jaeger │
       │ docker  │  │ 內網 vmui   │  │ (--profile obs 才啟動)│
       │ tail +  │  │ 內網 log UI │  │ 內網 metrics / trace │
       │ Lua 富化│  │ 含 container│  │ UI,host 不直接暴露   │
       │         │  │ _name 富化  │  │                     │
       └─────────┘  └─────────────┘  └─────────────────────┘

  ┌──────────────────────────────────────────────────────────┐
  │              AI sidecars(內網,不對外)                  │
  │                                                          │
  │  hermes:7800 — Hermes ACP supervisor                     │
  │    └─ per-user ACP 子進程池(idle-evict)                │
  │       └─ MCP HTTP client → mem0:7900/mcp/mcp             │
  │                                                          │
  │  mem0:7900   — 語意記憶層                                │
  │    ├─ FastAPI proxy + FastMCP `search_memory` tool       │
  │    └─ pgvector(mem0-postgres,per-user partition)       │
  └──────────────────────────────────────────────────────────┘
```

**架構亮點**:

- **真閘道**:nginx → APISIX → backend 全內網閉環,backend port 8000 從 host 完全拿掉,所有外部流量強制過 APISIX(rate-limit / breaker / request-id)
- **零本機資料**:案例 / 結果 / 截圖 / 附件 / 表單(含 Mock 端點 + DB 連線 + AI 對話歷史)**全部寫 DB / SeaweedFS**,`STORAGE_BACKEND=s3` 在啟動時強制檢查,設成 local 或不設都會 fail-fast 拒啟動
- **每案隔離**:Celery Worker 透過 Docker SDK spawn 獨立 runner 容器,跑完自毀(含截圖、Trace、影片即時上傳 SeaweedFS)
- **per-user MCP 容器** ✨:每個使用者擁有獨立 Playwright MCP chromium,互不打架;**asyncio cancel 即時中止 + idle sweeper 背景回收**
- **單一前端檔 + 漸進改善**:`frontend/index.html` + Tailwind CDN + Vanilla JS,**零 build step、零 npm install**;非 critical 第三方 lib(Chart / Mermaid / html2pdf / marked)改 lazy-load,首頁省 ~800 KB transfer
- **Fernet 加密 secret**:DB password、SMTP password、AI API Key、OIDC client secret 都在 PostgreSQL 中以密文落地
- **AST 白名單運算式**:動態運算式不用 `eval`,改用 `ast` AST 解析 + 函式呼叫 / 屬性存取 全黑名單,杜絕 injection
- **集中式 log + 觀察性**:Fluent Bit 用 Lua filter 從 docker config 抓 container_name → VictoriaLogs 內部 vmui(可按容器過濾);觀察性堆疊(Prometheus + Jaeger)`--profile obs` 一鍵開啟,但 host 對外仍只保留 `http://localhost/`
- **零 Copyleft**:整套 stack 全 Apache 2.0 / BSD-3 / PostgreSQL License — 商業 SaaS 上線無授權義務
- **API-First**:所有操作都有 REST / WebSocket API,Swagger 在 `/docs`,CI/CD 直接接

---

## 💡 典型應用場景

### 1. QA 團隊建立自動化基線(從 0 到 1)
從零建立第一份回歸套件;UI / API / DB 共用一套工具與報告格式。
**新升級**:用 AI Chat ⚡ 一鍵生 30 個基礎案例先鋪量,再人工精修。

### 2. DevOps 接 CI/CD
透過 REST API 觸發執行、查詢報告、下載 Trace;或用 `run_tests.py -f your_test.md` 把自備的 Markdown BDD 檔轉 `.robot` 在 Jenkins / GitHub Actions / GitLab CI 內跑。

### 3. PM / BA 用 Markdown 撰寫驗收標準 + AI 補案例
PM 在「需求 / RTM」分頁寫 User Story 跟 AC,**AI Chat ⚡ 直接從 AC 生對應測試案例**,QA 接手調整。RTM 追溯鏈即時可視化「需求覆蓋率」。

### 4. E2E 跨領域測試
一份案例同時驗證 UI 操作 + API 回應 + DB 寫入 + 手機 App 推播;單一報告以時間軸還原完整使用者旅程。
**新升級**:If / ElseIf 條件分支讓單一案例可走「成功路徑」與「失敗路徑」共用前置動作。

### 5. 多客戶顧問公司
Organization 多租戶 + 集中式帳號管控(管理員建帳號)+ 完整 audit log + 7 欄統一狀態工作流(新建立 → 等待處理 → 進行中 → 等待審核 → 退回修改 → 已驗證 → 已關閉),讓同一套平台同時服務多個客戶,資料完全隔離,所有 entity 走相同生命週期。

### 6. 零 Selenium 經驗的新團隊(AI 加速導入)
讓 AI 透過 MCP 開啟客戶網站「自己」走一遍流程 → 平台直接吸收成案例 → QA 補斷言條件。**從錄製 + 寫腳本變成 review + 補強。**

---

## 🌍 跨平台部署

**官方支援 Windows / macOS / Ubuntu / Linux**。唯一需求:Docker 24+ / Docker Compose v2.23+。

> 🍎 **Apple Silicon (M 系列) Mac**:可正常執行,但 Robot runner 容器為 amd64 透過 Rosetta / QEMU 模擬,啟動約比原生慢 2–4 倍。長時間大量跑案例建議用原生 amd64 機器。

---

## 📚 延伸閱讀

- 📖 **[操作說明.md](操作說明.md)** — 從零到上線的完整教學(含 5 大使用者旅程 + AI 生案 + MCP 自動操作 + 條件分支)
- 📜 **[LICENSES.md](LICENSES.md)** — 第三方授權與 SaaS 商業使用稽核
- 🔌 **REST API 文件**:OpenAPI JSON `http://localhost/api/openapi.json`;Swagger UI 走容器內 `docker compose exec backend curl localhost:8000/docs`
- 🎬 **Playwright Trace Viewer**:<https://trace.playwright.dev/> 可載入平台產出的 `trace.zip`
- 🤖 **Model Context Protocol**:<https://modelcontextprotocol.io/> RL MCP 整合採用此規範

---

## 🤝 商業授權與支援

RL 採用 **Apache 2.0**,允許商業使用、修改、再散佈;**不收授權費**。

---

<p align="center">
<b>RL v1.1</b> — 讓自動化測試回歸簡單、透明、可追溯,讓 AI 替你完成第一輪測試。<br>
<sub>📜 <a href="LICENSES.md">License & Commercial Use</a> · 🐳 <a href="操作說明.md">操作說明</a> · 🔌 <a href="http://localhost/api/openapi.json">API Docs</a></sub>
</p>
