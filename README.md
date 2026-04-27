# AutoTest v1.0 — Enterprise Test Automation Platform

> **一個平台,涵蓋整條測試生命週期。** 用業界標準開源技術(Robot Framework + Playwright + Appium),
> 取代 Selenium IDE / Postman / Jira / TestRail / Allure 各自分散的工具鏈,
> 讓 QA、開發與產品團隊在同一份資產上協作。

[![License](https://img.shields.io/badge/License-Apache%202.0-green.svg)](LICENSES.md)
[![Robot Framework](https://img.shields.io/badge/Engine-Robot%20Framework%207.x-blue.svg)](https://robotframework.org/)
[![Docker](https://img.shields.io/badge/Deploy-Docker%20Compose-2496ED.svg)](https://docs.docker.com/compose/)
[![Stack](https://img.shields.io/badge/Stack-FastAPI%20%2B%20PostgreSQL%20%2B%20SeaweedFS-0a7e07.svg)](#-技術架構)

---

## 🎯 為誰而做

AutoTest 鎖定的客戶是 **15–500 人規模、有自動化測試需求但被工具鏈拖累** 的軟體團隊:

| 痛點 | AutoTest 怎麼解 |
|---|---|
| **「QA 寫 Selenium、PM 看 Confluence、Bug 在 Jira、報告在 Allure,沒人看得到全貌」** | 一個平台:案例 / 排程 / 執行 / 報告 / 缺陷 / 待辦 全部串通 |
| **「商業 SaaS 一條 user $20–$100 / 月,我們 50 人就 $5K-$60K / 年」** | **Apache 2.0 全開源、零授權費**,自架到內網即可 |
| **「QA 流動率高,案例只有原作者看得懂」** | Markdown 為原生格式 + BDD/AC 雙寫 + 錄製器產生定位器,**新人零門檻接手** |
| **「想接 CI/CD 但 SaaS API 限速、要付 enterprise 加價」** | **API-First** + Swagger;`/api/executions` 開放呼叫,無速率限制 |
| **「合規:測試資料、截圖、影片不能傳第三方」** | 全棧自架(PostgreSQL / SeaweedFS / Valkey),資料完全留在內網 |

---

## 💼 商業定位

### vs. 主流商業 SaaS

| 維度 | TestRail / Zephyr / qTest | Tricentis / Katalon | **AutoTest** |
|---|---|---|---|
| 部署方式 | SaaS only | SaaS / On-Prem(高價) | **自架(Docker Compose)** |
| 價格(50 人) | $30K–$60K / 年 | $50K–$200K / 年 | **$0** |
| 廠商鎖定 | DSL + 專屬 SDK | 專屬 IDE + 腳本格式 | **Robot Framework 標準語法** |
| 資料主權 | 廠商雲端 | 廠商雲端(可選自架) | **完全在你的伺服器** |
| 可程式化 | 受限 API | 受限 API + 加價 | **完整 REST + WebSocket** |
| 案例匯出格式 | 廠商專屬 | 廠商專屬 | **`.md` + `.robot` 標準** |
| 升級路徑 | 跟廠商走 | 跟廠商走 | **隨時 fork、社群版本** |

### 適合的應用情境

- ✅ **金融 / 政府 / 醫療**:資料不可外洩、需要 air-gap 部署
- ✅ **製造業 / IoT**:測試對象在內網設備,SaaS 連不到
- ✅ **新創 / 中型 SI**:預算有限但要有完整測試平台,跳過 SaaS 學費
- ✅ **多專案顧問公司**:一套平台給多客戶共用,組織隔離 + 多租戶
- ❌ 不適合:純前端 unit test(請用 Vitest/Jest)、純後端 API 整合(請直接用 pytest/Postman)

---

## ✨ 核心價值主張

| 價值 | 說明 |
|---|---|
| 🧩 **不寫程式也能建測試** | 100+ 原子動作關鍵字、12 種比對運算子、28 種 Faker 隨機資料,拖拉填表即完成 |
| 📄 **Markdown 是第一類公民** | 每個測試案例都能匯出 `.md` — 進 git、過 PR review、版本控管、CLI 獨立執行 |
| 🔓 **零廠商鎖定** | 底層 Robot Framework 7.x + Playwright + Appium;社群人才庫龐大、技術可遷移 |
| 🌐 **跨 5 大平台單一體驗** | WEB UI / HTTP API / 手機 App / SQL DB / E2E,**一份案例、一份報告** |
| 🎨 **企業內建工作流** | 待辦清單(Epic→Story→Task/Bug/Spike 階層)、通知中心、追溯矩陣、多租戶組織管理 |
| 🌍 **雙語雙主題開箱即用** | 繁體中文 / English 一鍵切換、亮 / 暗主題自動記憶 |

---

## 🚀 5 分鐘上線

```bash
# macOS / Ubuntu / Linux
git clone <你的 repo URL> && cd autotest_v1.0
./deploy.sh                # 自動建 image、啟動全部服務、開瀏覽器

# Windows (PowerShell)
.\deploy.ps1
```

腳本完成後到 <http://localhost> 用 `admin` / `admin123` 登入即可開始建案例。

> 📖 **完整教學**(從建專案 → 寫案例 → 跑測試 → 看報告)請見 **[操作說明.md](操作說明.md)**

---

## 🎯 一個平台,七種主流測試方法論

AutoTest 的核心設計理念是「**讓每一種主流測試方法論都能在同一平台內自然表達**」:

| 方法論 | 平台支援 | 對應功能 |
|---|---|---|
| **ATDD** 驗收測試驅動 | 每案例獨立的「驗收準則 (AC)」+「前置動作 (Pre-Setup)」區塊 | 編輯器四區塊之一 |
| **BDD** 行為驅動 | `Given / When / Then / And / But` 關鍵字下拉 + 可讀步驟描述 | 步驟表第一欄 |
| **KDT** 關鍵字驅動 | 內建 100+ 動作關鍵字,完全免寫 code | 步驟表「動作」下拉 |
| **DDT** 資料驅動 | DDT 資料表 + `${變數}` 自動替換 + 逐列展開,**每列獨立錄影 + Trace** | 編輯器底部 DDT 區 |
| **TDD** 測試驅動開發(驗收層級) | 步驟層級 Pass/Fail 即時回饋 + 失敗精準定位 | WebSocket 即時日誌 |
| **SBE** Specification by Example | DDT 列出例子、AC 描述規則,兩者對應後可同時被執行 | AC + DDT 組合 |
| **FDD** 功能驅動開發 | 5 層樹 Feature → Platform → Page → Scenario → TestCase + 測試回合 | 左側目錄樹 + 測試回合 |

---

## 🏗 整合的 ALM 工作流

除了測試案例本身,平台內建一套完整的應用生命週期管理:

| 模組 | 功能 |
|---|---|
| **WBS** | 工作分解結構 + 進度百分比 + 依負責人篩選 |
| **測試計畫** | ISTQB 8 區塊格式(Scope In/Out / 策略 / 資源 / 時程 / 風險 / 入出條件 / 簽核)|
| **需求 + RTM** | User Story → AC 階層,RTM 矩陣顯示需求 × 案例 × 缺陷 |
| **待辦清單(Backlog)** | Epic → Story → Task / Bug / Spike 階層 + Sprint label;Kanban 樹狀檢視;個人 / 全員過濾 |
| **缺陷管理** | 8 種狀態工作流 + 嚴重性 + 附件;與測試案例 / 報告自動連結 |
| **測試時程** | 里程碑 + Gantt 風格時間軸 + 起訖日色彩標記 |
| **測試回合** | 命名集合彙總執行,單一報告 |
| **通知中心** | 站內紅點 badge + Email(per-event channel)+ toast 訊息自動入列 |
| **多租戶組織** | Organization + Role(Admin / QA / Viewer)+ 完整 audit log + JWT |
| **OIDC SSO** | Discovery URL 自動拉 endpoints,可接 Azure AD / Okta / Keycloak |

---

## 🛠 業界標準開源技術棧

完全建立在開源、社群活躍、人才好找的技術之上 — **不會把團隊鎖在廠商 DSL**:

| 元件 | 版本 | 用途 |
|---|---|---|
| Robot Framework | 7.x | 測試引擎、`.robot` 語法、log.html / report.html |
| Browser Library | 19.x | Playwright 底層,trace + video + auto-wait |
| RequestsLibrary | latest | HTTP API(GET / POST / PUT / PATCH / DELETE)|
| DatabaseLibrary | latest | SQL(MySQL / PostgreSQL / MSSQL / Oracle / SQLite / MongoDB / Redis)|
| AppiumLibrary | latest | iOS / Android 自動化 |
| Markdown | — | 案例原生格式,`run_tests.py` 可 CLI 直接執行 |

整套 stack 為 **Apache 2.0 / BSD-3 / PostgreSQL License** — 商業 SaaS 部署無授權義務。

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
                     │  Nginx   │  ← 反代 /api /ws /results + CORS
                     └────┬─────┘
                          │
            ┌─────────────┼──────────────┐
        ┌───▼────┐    ┌───▼───┐    ┌─────▼───┐
        │FastAPI │    │Celery │    │WebSocket│
        │ (REST) │    │Worker │    │(即時日誌)│
        └──┬─────┘    └──┬────┘    └─────┬───┘
           │             │ spawn          │
           │             ▼                │
           │    ┌────────────────┐        │
           │    │ robot-runner   │ ←  每案一個 │
           │    │ 容器(短效期)  │   跑完自毀 │
           │    └────────┬───────┘        │
           │             │                │
           ▼             ▼                ▼
      ┌──────────┐  ┌───────────┐  ┌────────┐
      │PostgreSQL│  │SeaweedFS  │  │ Valkey │
      │  (資料)  │  │(截圖/影片)│  │(broker)│
      └──────────┘  └───────────┘  └────────┘

       ┌─────────┐  ┌─────────┐  ┌────────────┐
       │ APISIX  │  │FluentBit│  │VictoriaLogs│
       │(API GW) │  │(log 採集)│  │ (vmui)     │
       └─────────┘  └─────────┘  └────────────┘
```

**架構亮點**:

- **前端**:單一 `index.html` + Tailwind CDN + Vanilla JS,**零 build step**
- **每案隔離**:Celery Worker 透過 Docker SDK spawn 獨立 runner 容器,跑完自毀
- **物件儲存**:截圖 / 影片 / Trace 即時上傳 SeaweedFS(S3 相容,Apache 2.0)
- **集中式 log**:Fluent Bit → VictoriaLogs(自帶 vmui 查詢面板,port 9428)
- **零 Copyleft**:整套 stack 全 Apache 2.0 / BSD-3 / PostgreSQL License
- **API-First**:所有操作都有 REST / WebSocket API,Swagger 在 `/docs`,無痛接 CI/CD

---

## 💡 典型應用場景

### 1. QA 團隊建立自動化基線
從零建立第一份回歸套件;UI / API / DB 共用一套工具與報告格式。新人接手不必重學 — 一份 Markdown 人工審查 / 自動執行兩用。

### 2. DevOps 接 CI/CD
透過 REST API 觸發執行、查詢報告、下載 Trace;或用 `run_tests.py` 把 `tests/*.md` 轉 `.robot` 在 Jenkins / GitHub Actions / GitLab CI 內跑。

### 3. PM / BA 用 Markdown 撰寫驗收標準
PM 寫的 AC 直接成為測試案例起點,QA 接手補 BDD 步驟與 DDT 例子,大幅減少需求失真。

### 4. E2E 跨領域測試
一份案例同時驗證 UI 操作 + API 回應 + DB 寫入 + 手機 App 推播;單一報告以時間軸還原完整使用者旅程。

### 5. 多客戶顧問公司
Organization 多租戶 + 完整 audit log,讓同一套平台同時服務多個客戶,資料完全隔離。

---

## 🌍 跨平台部署

**官方支援 Windows / macOS / Ubuntu / Linux**。唯一需求:Docker 24+ / Docker Compose v2。

> 🍎 **Apple Silicon (M 系列) Mac**:可正常執行,但 Runner 容器為 amd64 透過 Rosetta / QEMU 模擬,啟動約比原生慢 2–4 倍。長時間大量跑案例建議用原生 amd64 機器。

---

## 📚 延伸閱讀

- 📖 **[操作說明.md](操作說明.md)** — 從零到上線的完整教學
- 📜 **[LICENSES.md](LICENSES.md)** — 第三方授權與 SaaS 商業使用稽核
- 🔌 **REST API 文件**:<http://localhost:8000/docs>(Swagger UI)
- 🎬 **Playwright Trace Viewer**:<https://trace.playwright.dev/> 可載入平台產出的 `trace.zip`

---

## 🤝 商業授權與支援

AutoTest 採用 **Apache 2.0**,允許商業使用、修改、再散佈;**不收授權費**。

需要的話可以另外洽談:

- 🛠 **客製化開發** — 整合進你的內部系統、SSO、Jira / Slack 等
- 🎓 **導入培訓** — QA 團隊上手工作坊(2 天)、自動化框架顧問
- 🆘 **企業級支援** — SLA 維運、版本升級協助
- ☁ **託管部署** — 你不想自己管運維?我們可代管你的私有雲

聯繫: 請開 issue 或 email 給維運團隊。

---

<p align="center">
<b>AutoTest v1.0</b> — 讓自動化測試回歸簡單、透明、可追溯。<br>
<sub>📜 <a href="LICENSES.md">License & Commercial Use</a> · 🐳 <a href="操作說明.md">操作說明</a> · 🔌 <a href="http://localhost:8000/docs">API Docs</a></sub>
</p>
