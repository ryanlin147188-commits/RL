# RL — Enterprise Test Automation Platform

> **一個平台,涵蓋整條測試生命週期,還會自己「動手」測試。**
> 用業界標準開源技術(Robot Framework + Playwright + Appium),取代 Selenium IDE + Postman + Jira + TestRail + Allure 各自分散的工具鏈。
> **Apache 2.0 全開源,自架到內網即可,內建 AI 助理可直接操作瀏覽器產生測試案例。**

[![License](https://img.shields.io/badge/License-Apache%202.0-green.svg)](LICENSES.md)
[![Robot Framework](https://img.shields.io/badge/Engine-Robot%20Framework%207.x-blue.svg)](https://robotframework.org/)
[![Docker](https://img.shields.io/badge/Deploy-Docker%20Compose-2496ED.svg)](https://docs.docker.com/compose/)
[![Stack](https://img.shields.io/badge/Stack-FastAPI%20%2B%20PostgreSQL%20%2B%20SeaweedFS-0a7e07.svg)](#-技術架構)
[![AI](https://img.shields.io/badge/AI-MCP%20%2B%20Vision%20%2B%2011%20providers-7c3aed.svg)](#-ai-原生:平台會自己寫案例-自己跑測試)

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
v1.0 內建 **三條 AI 生產線**,把 LLM 當作平台第一公民:

### 1️⃣ AI Chat ⚡ 一鍵生成可執行案例
> 「我要測購物車從加入到結帳的完整流程」
- 內建多輪 tool calling(OpenAI / Anthropic / Google 三家統一 schema)
- 直接吐出 `steps_json` schema 化結構,**不是純文字**,平台立即可跑
- 套用到當前案例 / 開新 SCENARIO 建新案例兩種選擇
- 失敗自動 fallback 為傳統 markdown 模式,不會卡死

### 2️⃣ AI 直接操作瀏覽器(MCP 整合)
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

### 🎯 11 家 LLM provider + 完全本地化選項
| 雲端 | 本地 / 自架 |
|---|---|
| OpenAI · Anthropic · DeepSeek · Groq · OpenRouter · Together AI · Mistral · xAI · Google Gemini | Ollama · LM Studio · 自架 OpenAI-compatible 端點 |

- **「用 token 拉模型清單」** 按鈕:輸入 API key 一鍵列出該 provider 全部可用模型
- 自動偵測推理模型(o1 / o3 / GPT-5 / DeepSeek-R1 等)→ 啟用「思考程度」(low / medium / high)
- API key / model id / 自架 base_url **Fernet 加密落地**,從不明文存 DB

---

## 為誰打造

RL 鎖定 **15–500 人規模、有自動化測試需求但被工具鏈拖累** 的軟體團隊:

### ✅ 適合
- **金融 / 政府 / 醫療**:資料合規敏感、需要 air-gap 部署、SaaS 不能用、AI 必須走本地 Ollama
- **製造業 / IoT**:測試對象在內網設備、SaaS 連不到
- **新創 / 中型 SI**:預算有限但要完整測試平台 + AI 加速,不想付 SaaS 學費
- **多客戶顧問公司**:一套平台多客戶共用,內建 Organization 多租戶 + 邀請碼註冊 + audit log

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
| 📊 **完整 RTM 追溯鏈** | User Story → AC → TestCase → Defect 一頁看穿;**Backlog Task 可橫向連結 9 種實體** |
| 🏢 **多租戶 + 自助註冊** ✨ | Organization 隔離、**email_domain 自動歸屬**、**邀請碼 (OrgInvite)** 控管自助註冊、群組可巢狀 + 可當 Todo assignee |
| 🏷 **測試版號追蹤** ✨ | WEB / API / APP 版號獨立管理,測試報告 / 缺陷 / 回合反向 FK 連動,清楚「這 bug 是哪個版本爆的」 |
| 🎯 **多方法論支援** | ATDD / BDD / KDT / DDT / TDD / SBE / FDD 都能在平台內自然表達 |
| 🏗 **完整 ALM** | 測試計畫(ISTQB 8 區塊)/ 需求 / 缺陷 / 里程碑 / WBS / 文件 / Backlog / 排程 / 通知 |
| 🔐 **企業級 Auth** | JWT 雙 token、bcrypt 密碼、Fernet 加密 secret(含 AI key / DB pwd / SMTP pwd)、大頭貼上傳、角色權限矩陣、OIDC 整合 |
| 🌍 **雙語雙主題** | 繁體中文 / English 一鍵切換、亮 / 暗主題自動記憶 |

---

## 5 分鐘上線

```bash
# macOS / Ubuntu / Linux
git clone https://github.com/ryanlin147188-commits/RL_TMP.git && cd RL_TMP
./deploy.sh                # 自動建 image、啟動全部服務、開瀏覽器

# Windows (PowerShell)
.\deploy.ps1
```

腳本完成後到 <http://localhost> 用 `admin` / `admin123` 登入即可開始建案例。

> 📖 **完整教學**(從建專案 → 寫案例 → 用 AI 生案例 → 跑測試 → 看報告)請見 **[操作說明.md](操作說明.md)**

### 不想本機 build?用預先打包的 image

從 [GitHub Releases](https://github.com/ryanlin147188-commits/RL_TMP/releases) 下載 `autotest-images-1.0.0.tar`(離線散佈包,含 backend / celery / runner / mcp / frontend 等 image),在你的 VM 上:

```bash
# 1) 載入 image(2.6 GB,需要幾分鐘)
docker load -i autotest-images-1.0.0.tar

# 2) 取得 docker-compose.bundle.yml + apisix/、fluent-bit/ 設定檔(repo 根目錄裡都有)
git clone https://github.com/ryanlin147188-commits/RL_TMP.git && cd RL_TMP

# 3) 啟動
docker compose -f docker-compose.bundle.yml up -d
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
| **Backlog 待辦** | Feature → Task / Bug / Spike 階層 + Sprint label;**可連結 9 種實體**(需求 / 案例 / 缺陷 / WBS / 計畫 / 回合 / 里程碑 / 文件 / 專案);**指派可指向使用者或群組** ✨ |
| **群組管理** ✨ | 設定頁分頁,可巢狀(parent_id),Todo assignee 可選群組;群組成員透過 GroupMembership 表維護 |
| **需求 + RTM** | User Story → AC 階層,**RTM 追溯鏈** 在每個節點顯示 linked Backlog,完整可視化 |
| **缺陷管理** | 8 種狀態工作流 + 嚴重性 + 附件 + 「關聯測試案例」下拉,自動納入 RTM 鏈;**可標記發生於哪個測試版號** ✨ |
| **測試版號** ✨ | 設定頁分頁,WEB / API / APP 三軌獨立管理;版號連動測試報告 / 缺陷 / 回合 |
| **WBS** | 工作分解結構 + 進度百分比 + 依負責人篩選 |
| **測試計畫** | ISTQB 8 區塊格式(Scope / 策略 / 資源 / 時程 / 風險 / 入出條件 / 簽核)|
| **測試時程** | 里程碑 + Gantt 風格時間軸 |
| **測試回合** | 命名集合彙總執行,單一報告 |
| **測試看版 (Kanban)** | 缺陷狀態看板 + 拖拉變更狀態 + Backlog 連結徽章 |
| **通知中心** | 站內紅點 badge + Email(per-event channel)+ toast 訊息歷史 |
| **多租戶 + 自助註冊** ✨ | Organization 隔離 + email_domain 自動歸屬 + 邀請碼(`OrgInvite`,可設過期 / 用量上限 / 預設角色) |
| **AI 助理 + AI Token** ✨ | 11 家 provider 切換、用 token 拉模型清單、推理模型思考程度自動偵測;Fernet 加密落地 |
| **使用者帳戶** | 大頭貼上傳(SeaweedFS,5 MB 內)、改顯示名稱 / Email / 角色;JWT 雙 token、bcrypt 密碼、Fernet 加密 secret |

---

## 🛠 業界標準開源技術棧

完全建立在開源、社群活躍、人才好找的技術之上 — **不會把團隊鎖在廠商 DSL**:

| 元件 | 版本 | 用途 |
|---|---|---|
| Robot Framework | 7.x | 測試引擎、`.robot` 語法、log.html / report.html、IF/ELSE/END 分支 |
| Browser Library | 19.x | Playwright 底層,trace + video + auto-wait |
| RequestsLibrary | latest | HTTP API(GET / POST / PUT / PATCH / DELETE)|
| DatabaseLibrary | latest | SQL(MySQL / PostgreSQL / MSSQL / Oracle / SQLite / MongoDB / Redis)|
| AppiumLibrary | latest | iOS / Android 自動化 |
| Playwright MCP | latest | LLM tool calling 直接操作瀏覽器(Anthropic Model Context Protocol)|
| mitmproxy | latest | API 錄製 Docker mode,自動轉 Http.* steps |
| Markdown | — | 案例原生格式,`run_tests.py` 可 CLI 直接執行 |

整套 stack 為 **Apache 2.0 / BSD-3 / PostgreSQL License** — 商業 SaaS 部署無授權義務。詳見 [LICENSES.md](LICENSES.md)。

---

## ⚙ 部署模式

### 一鍵部署(推薦)

```bash
./deploy.sh           # macOS / Ubuntu / Linux
.\deploy.ps1          # Windows (PowerShell)
```

**子命令**:

| 用途 | bash | PowerShell |
|---|---|---|
| 部署 / 啟動 | `./deploy.sh` | `.\deploy.ps1` |
| 容器狀態 | `./deploy.sh --status` | `.\deploy.ps1 -Status` |
| 即時 log | `./deploy.sh --logs` | `.\deploy.ps1 -Logs` |
| 停止(保留資料)| `./deploy.sh --stop` | `.\deploy.ps1 -Stop` |
| 重置(**清空所有資料**)| `./deploy.sh --reset` | `.\deploy.ps1 -Reset` |

### 系統需求

- Docker 24+ / Docker Compose v2.23+
- 4 GB RAM(建議 8 GB,若大量用 MCP 容器建議 16 GB)
- 10 GB 磁碟(初始,加 MCP image 約多 1.5 GB)
- Windows / macOS / Ubuntu / Linux

> 📖 **完整部署流程**(`.env`、跨平台指令、本機開發、升級)請見 **[操作說明.md](操作說明.md)**
> 📖 **REST API**: <http://localhost:8000/docs>(Swagger UI)

---

## 🏗 技術架構

```
         ┌──────────────────────────────────────┐
         │   使用者瀏覽器(單頁 HTML/JS,無 build) │
         └────────────────┬─────────────────────┘
                          │
                     ┌────▼─────┐
                     │  Nginx   │  ← 反代 /api /ws /pics /results /novnc + CORS
                     └────┬─────┘
                          │
            ┌─────────────┼──────────────────────────┐
        ┌───▼────┐    ┌───▼───┐    ┌─────▼───┐  ┌────▼─────┐
        │FastAPI │    │Celery │    │WebSocket│  │  Lifespan│
        │ (REST) │    │Worker │    │(即時日誌)│  │  Tasks   │
        └──┬─────┘    └──┬────┘    └─────┬───┘  │ scheduler│
           │             │ spawn          │     │ MCP idle │
           │             ▼                │     │ sweeper  │
           │    ┌────────────────┐        │     └──────────┘
           │    │ robot-runner   │ ←  每案一個 │
           │    │ 容器(短效期)  │   跑完自毀 │
           │    └────────┬───────┘        │
           │             │                │
           │   ┌─────────▼──────────┐     │
           │   │ recorder / recorder-api │ ← Docker 錄製,noVNC iframe
           │   │ + mitmproxy(API mode)  │
           │   └────────┬─────────────┘
           │            │
           │   ┌────────▼──────────────┐
           │   │ playwright-mcp 容器   │ ← per-user 隔離,LLM 透過 MCP 操作 chromium
           │   │ (idle sweeper 自動回收) │
           │   └───────────────────────┘
           │
           ▼
      ┌──────────┐  ┌───────────┐  ┌────────┐
      │PostgreSQL│  │SeaweedFS  │  │ Valkey │
      │(全部資料)│  │截圖/影片  │  │(broker)│
      │          │  │/Trace/附件│  │        │
      └──────────┘  └───────────┘  └────────┘

       ┌─────────┐  ┌─────────┐  ┌────────────┐
       │ APISIX  │  │FluentBit│  │VictoriaLogs│
       │(API GW) │  │(log 採集)│  │(vmui 面板) │
       └─────────┘  └─────────┘  └────────────┘
```

**架構亮點**:

- **零本機資料**:案例 / 結果 / 截圖 / 附件 / 表單(含 Mock 端點 + DB 連線 + AI 對話歷史)**全部寫 DB / SeaweedFS**,瀏覽器 localStorage 只用於 UI 偏好(主題、語系)
- **每案隔離**:Celery Worker 透過 Docker SDK spawn 獨立 runner 容器,跑完自毀(含截圖、Trace、影片即時上傳 SeaweedFS)
- **per-user MCP 容器** ✨:每個使用者擁有獨立 Playwright MCP chromium,互不打架;**asyncio cancel 即時中止 + idle sweeper 背景回收**
- **單一前端檔**:`frontend/index.html` + Tailwind CDN + Vanilla JS,**零 build step、零 npm install**
- **Fernet 加密 secret**:DB password、SMTP password、AI API Key、OIDC client secret 都在 PostgreSQL 中以密文落地
- **AST 白名單運算式**:動態運算式不用 `eval`,改用 `ast` AST 解析 + 函式呼叫 / 屬性存取 全黑名單,杜絕 injection
- **集中式 log**:Fluent Bit → VictoriaLogs(自帶 vmui 查詢面板,port 9428)
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
Organization 多租戶 + email_domain 自動歸屬 + 邀請碼自助註冊 + 完整 audit log,讓同一套平台同時服務多個客戶,資料完全隔離。

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
- 🔌 **REST API 文件**:<http://localhost:8000/docs>(Swagger UI)
- 🎬 **Playwright Trace Viewer**:<https://trace.playwright.dev/> 可載入平台產出的 `trace.zip`
- 🤖 **Model Context Protocol**:<https://modelcontextprotocol.io/> RL MCP 整合採用此規範

---

## 🤝 商業授權與支援

RL 採用 **Apache 2.0**,允許商業使用、修改、再散佈;**不收授權費**。

---

<p align="center">
<b>RL v1.0</b> — 讓自動化測試回歸簡單、透明、可追溯,讓 AI 替你完成第一輪測試。<br>
<sub>📜 <a href="LICENSES.md">License & Commercial Use</a> · 🐳 <a href="操作說明.md">操作說明</a> · 🔌 <a href="http://localhost:8000/docs">API Docs</a></sub>
</p>
