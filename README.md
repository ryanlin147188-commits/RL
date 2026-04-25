# AutoTest v1.0 — 自動化測試平台

> 一站式企業級自動化測試平台，以 **Robot Framework** 為核心，提供可視化步驟設計、多源錄製、Docker 隔離執行、完整 Trace 追蹤與儀表板觀察。**一個平台涵蓋 WEB / API / APP / DB / E2E 五大測試領域**，讓 QA、開發與產品團隊共用同一套測試資產。

---

## ✨ 為什麼選擇 AutoTest

| 價值主張 | 說明 |
|---|---|
| 🧩 **不寫程式也能建立自動化測試** | 100+ 原子動作關鍵字、12 種比對運算子、28 種 Faker 隨機資料，拖拉填表即可完成測試案例 |
| 📄 **Markdown 是第一類公民** | 每個測試案例都能匯出 `.md`：可進 git、可 PR review、可版本控管、可脫離平台用 CLI 直接執行 |
| 🔓 **業界標準開源技術，零廠商鎖定** | 底層為 Robot Framework 7.x + Playwright + Appium；換家公司、接 CI/CD、交接開發都無痛 |
| 🌐 **跨 5 大平台單一體驗** | WEB UI / HTTP API / 手機 App / SQL 資料庫 / E2E 跨領域，**一份案例、一份報告** |

---

## 🎯 一個平台，七種主流測試方法論

AutoTest 的核心設計理念就是「讓每一種主流測試方法論都能在同一平台內自然表達」。下表說明每種方法論在本平台的落地方式：

| 方法論 | 產品如何支援 | 對應功能 |
|---|---|---|
| **ATDD**<br>驗收測試驅動開發 | 每個測試案例都有獨立的「驗收準則 (AC)」文字框與「前置動作 (Pre-Setup)」區塊，**先寫驗收條件 → 再用 BDD 步驟驗證** | 編輯器上半部四區塊之一 |
| **BDD**<br>行為驅動開發 | 每個步驟都有 `Given / When / Then / And / But` 關鍵字下拉 + 可讀性優先的步驟描述欄位；報告也依此敘事呈現 | 步驟表第一欄 |
| **KDT**<br>關鍵字驅動測試 | 內建 **42 個 WEB、16 個 API、20 個 APP、9 個 DB** 動作關鍵字，外加 12 個比對運算子；完全不用寫 code | 步驟表「動作 (Action)」下拉 |
| **DDT**<br>資料驅動測試 | 每個案例可配一份 DDT 資料表，`${變數}` 語法自動替換；可選逐列展開執行，**每列獨立錄影 + Trace** | 編輯器底部 DDT 資料區塊 |
| **TDD**<br>測試驅動開發 | 步驟層級 Pass / Fail 即時回饋 + 失敗訊息明確定位，支援 Red-Green 驗證循環 | 即時 WebSocket 日誌 + 步驟徽章 |
| **SBE**<br>Specification by Example | DDT 每一列即是一個**可執行的具體例子**；AC 描述規則、DDT 列出例子，兩者對應後同時可被執行 | AC + DDT 組合 |
| **FDD**<br>Feature-Driven Development | 5 層目錄樹以 **Feature** 為根，往下 Platform → Page → Scenario → TestCase，天然契合 FDD 的 feature-by-feature 增量交付；**測試回合** 即是 FDD 的 Build Schedule | 左側目錄樹 + 測試回合 |

> 💡 **關於 TDD**：本平台 TDD 主要針對**驗收層級**（acceptance-level TDD）—「先寫失敗案例 → 實作 → 轉綠」的節奏，不是針對 unit test 的傳統 xUnit 模式。

---

## 🛠 以業界標準開源技術為基礎

AutoTest 的執行引擎完全建立在 **Robot Framework 生態系** 之上。這意味著你學到的每一個動作、每一條語法、每一張報告都符合業界標準；將來想脫離平台直接跑 Robot CLI 也無痛接軌。

| 技術 / 元件 | 版本 | 用途 |
|---|---|---|
| **Robot Framework** | 7.x | 測試執行引擎、步驟編排、統一的 `.robot` 語法與 log.html / report.html |
| **Browser Library** | 19.x | WEB UI 自動化，底層為 **Playwright**，內建 trace + video + 自動等待 |
| **RequestsLibrary** | latest | HTTP API 測試（GET / POST / PUT / PATCH / DELETE / HEAD / OPTIONS） |
| **DatabaseLibrary** | latest | SQL 測試（MySQL / PostgreSQL / MSSQL / Oracle / SQLite） |
| **AppiumLibrary** | latest | iOS / Android App 自動化，透過外接 Appium Server |
| **Markdown (`.md`)** | — | 測試案例的原生儲存格式；可 git 追蹤、可匯入匯出、可 `run_tests.py` CLI 直接執行 |

**關鍵訊息**：這些都是**開源、免費、社群活躍** 的成熟技術；選擇 AutoTest 不會把團隊鎖在特定廠商的 DSL 或 SDK 裡。

---

## 📋 功能總覽

依測試生命週期的四大階段組織：

### 設計階段

- **5 層樹狀目錄**：Feature → Platform → Page → Scenario → TestCase
- **四區塊案例編輯器**：驗收準則 + 前置動作 + BDD 步驟表 + DDT 資料表
- **100+ 原子動作關鍵字**：WEB 42 / API 16 / APP 20 / DB 9（E2E 可混用全部）
- **12 種比對運算子**：Equals / NotEquals / Contains / StartsWith / Regex / GreaterThan / IsVisible / ...
- **28 種 Faker 隨機資料產生器**：姓名 / Email / UUID / 手機 / 信用卡 / 日期 / 公司名 / IPv4 ...

### 錄製轉換

- **WEB**：Playwright codegen 一鍵啟動，支援 **Windows PowerShell** 與 **macOS / Linux bash** 雙平台一鍵指令，含 trace.zip 自動上傳
- **API**：DevTools 複製 cURL → 貼上 → 自動解析成 `Http.*` 步驟
- **APP**：貼上 Appium Python 腳本 → 自動解析成 `Mobile.*` 步驟
- **Playwright Assertion**：錄製時使用 `Assert visibility / text / value` 會自動轉成 `AssertVisible / AssertText / AssertValue` 並帶上 Condition / Expected

### 執行

- **Docker 模式**：每個案例獨立 `autotest-robot-runner` 容器執行，**跑完自毀**，完全隔離、零殘留
- **本機模式**：有頭 Chromium，由 `local_agent.py` 認領任務，適合除錯 / 示範 / 教育訓練
- **4 種排程規則**：單次 (`ONCE`) / 每天 (`DAILY`) / 每週 (`WEEKLY`) / 每月 (`MONTHLY`)
- **測試回合**：具名案例集合，彙總多個測試案例一次執行、產出單一報告
- **7 種資料庫連線**：MySQL / PostgreSQL / MSSQL / Oracle / MongoDB / Redis / SQLite，附連線測試與 SQL 查詢區
- **Mock 端點管理**：REST Mock 設定，可在 Headers / Body 內使用 `{{name}}` / `{{uuid}}` / `{{int:1,100}}` 等 Faker 佔位符

### 觀察

- **即時 WebSocket 日誌**：瀏覽器內嵌終端機視窗，步驟狀態即時推送
- **儀表板**：通過率圓環圖、最近 N 次趨勢折線、各 Feature 通過率長條
- **首頁系統狀態面板**：即時顯示 Docker 狀態、**AutoTest 平台容器總用量**（CPU / 記憶體 / 網路流量為所有 `autotest-*` 容器加總）、儲存空間、Docker host 對照資訊（作業系統 / 核心數 / 總記憶體），每 5 秒透過 `/api/system/status` 輪詢
- **步驟時間軸**：UI / API / APP / DB 四種面板依 action 前綴自動切換
- **完整 Trace + Video**：Playwright `trace.zip` + `.webm` 錄影，報告頁可直接嵌入 Trace Viewer（iframe）
- **Screenshot Diff**：三聯比對（Baseline | Actual | 紅色差異覆蓋）+ 像素級 Pillow 驗證，支援「把當下設為新 baseline」一鍵更新
- **PDF 報告匯出**：依步驟類型自動分段呈現，適合交付給非技術團隊
- **Markdown 匯入 / 匯出**：讓測試案例進 git、過 PR review、用 CLI 獨立執行

### 使用者體驗

- **登入 / 註冊 / 忘記密碼**：前端自帶認證層（localStorage），預設帳號 `admin` / `admin123`；註冊後新帳號即時可用；忘記密碼可用 Email 驗證直接重設
- **使用者設定頁**：變更顯示名稱、Email、密碼、個人偏好（預設執行環境、自動刷新、提示未儲存等）
- **觸發者精準記錄**：手動執行的報告「觸發者」欄位顯示執行者帳號；排程觸發顯示「自動排程」
- **暗黑模式**：右上角一鍵切換，偏好自動儲存；所有工作區、表單、卡片同步切色
- **淺暖 UI 設計**：預設採 Stone / Amber / Orange 暖色系，降低長時間使用視覺疲勞
- **響應式佈局 (RWD)**：桌面 / 平板 / 手機三種斷點；手機版側邊欄改為抽屜、TopNav 改為使用者選單內建導覽
- **預設登入首頁**：切換專案或登入後自動回到 AutoTest 首頁（Hero + 專案概況 + 系統狀態 + 快速導覽 + 最近 10 筆測試案例）

---

## 🌍 跨平台部署

**官方支援 Windows / macOS / Ubuntu / Linux**，唯一需求是 Docker 24+ / Docker Compose v2。

> 🍎 **Apple Silicon (M1/M2/M3) Mac 相容性**：可正常執行，但 Runner 容器為 amd64 需透過 Rosetta / QEMU 模擬，瀏覽器啟動時間約比原生慢 2-4 倍。若要長時間跑大量案例，建議用原生 amd64 機器。

---

## ⚡ 快速啟動

### 一鍵部署腳本（推薦）

```bash
# macOS / Ubuntu / Linux
./deploy.sh

# Windows (PowerShell)
.\deploy.ps1
```

一鍵腳本會：檢查 Docker 環境 → 建立 `.env`（若不存在）→ 建 Runner image → `docker compose up -d --build` → 等待服務就緒 → 確保 `admin` 使用者存在 → **自動開瀏覽器**。

### 🎁 Bundle Image — 單一 Docker Image 散佈用

專案也能打包成一個 Ubuntu 24.04 base 的 image，裡面包含整份專案 + deploy 腳本 + Docker CLI。適合交付到新機器一次就跑起來。

```bash
# 打包（在開發機跑一次即可）
./build-bundle.sh          # Linux / macOS
.\build-bundle.ps1         # Windows

# → 產出 autotest_v1.0:latest 與 autotest_v1.0:v1.0 兩個 tag（~257 MB）

# 部署到新機器（該機器只需有 Docker，不需 clone git）
docker run --rm -it \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$PWD:$PWD" -w "$PWD" \
    autotest_v1.0

# 離線散佈：在開發機匯出為 tar，拷到目標機器 load
./build-bundle.sh --save   # 產出 autotest_v1.0-bundle.tar
# 在目標機器：
docker load -i autotest_v1.0-bundle.tar
docker run --rm -it \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$PWD:$PWD" -w "$PWD" \
    autotest_v1.0
```

Bundle image 可用的子命令：`install`（預設，解壓+部署）/ `extract`（只解壓）/ `info`（顯示說明）。詳見 [Dockerfile.bundle](Dockerfile.bundle)。

**子命令**（兩種腳本命名一致）：

| 用途 | bash | PowerShell |
|---|---|---|
| 部署 / 啟動 | `./deploy.sh` | `.\deploy.ps1` |
| 查看容器狀態 | `./deploy.sh --status` | `.\deploy.ps1 -Status` |
| 跟隨即時 log | `./deploy.sh --logs` | `.\deploy.ps1 -Logs` |
| 停止（保留資料）| `./deploy.sh --stop` | `.\deploy.ps1 -Stop` |
| 重置（清空所有資料）| `./deploy.sh --reset` | `.\deploy.ps1 -Reset` |

### 手動啟動（進階）

```bash
# 1. 建 Runner 容器 image
docker build -f backend/Dockerfile.runner -t autotest-robot-runner:latest backend/

# 2. 啟動所有服務
docker compose up -d --build

# 3. 開瀏覽器
# Windows：start http://localhost
# macOS：  open http://localhost
# Linux：  xdg-open http://localhost
```

**首次登入預設帳密**：`admin` / `admin123`（可在設定頁變更，註冊頁也可新增使用者）。
PostgreSQL 與 SeaweedFS 的預設 root 帳密也統一為 `admin` / `admin123`，可透過 `.env` 覆蓋。

> 📖 **完整部署流程**（`.env` 設定、跨平台指令、本機開發、升級與清理）請見 **[操作說明.md](操作說明.md)**。
> 📖 **REST API 規格** 啟動後見 <http://localhost:8000/docs>（Swagger UI，含 `/api/system/status` 系統狀態端點）。

---

## 🏗 技術架構

```
         ┌─────────────────────────────────────────┐
         │       使用者瀏覽器（單頁 HTML/JS）        │
         └────────────┬────────────────────────────┘
                      │
                 ┌────▼─────┐
                 │  Nginx   │ ← 反代 /api /ws /results + CORS
                 └────┬─────┘
                      │
        ┌─────────────┼──────────────┐
        │             │              │
    ┌───▼────┐   ┌────▼───┐    ┌─────▼───┐
    │FastAPI │   │ Celery │    │WebSocket│
    │ (REST) │   │ Worker │    │(即時日誌)│
    └──┬─────┘   └──┬─────┘    └─────┬───┘
       │            │ spawn          │
       │            ▼                │
       │    ┌───────────────┐        │
       │    │ robot-runner  │ ←  每案一 │
       │    │ 容器（短效期）  │   個容器 │
       │    └───────┬───────┘        │
       │            │                │
       ▼            ▼                ▼
  ┌──────────┐  ┌──────────┐  ┌────────┐
  │PostgreSQL│  │SeaweedFS │  │ Valkey │
  │  (資料)  │  │(截圖/影片)│  │(broker)│
  └──────────┘  └──────────┘  └────────┘
```

**架構重點**：

- **前端**：單一 `index.html` + TailwindCDN + Vanilla JS，**無 build step**，部署極簡
- **後端**：FastAPI 提供 REST + WebSocket；Celery Worker 負責 orchestration
- **每案隔離執行**：Celery Worker 透過 Docker SDK spawn 獨立 `autotest-robot-runner` 容器，跑完自毀，完全無狀態殘留
- **物件儲存**：所有截圖 / 影片 / Trace 即時上傳到 **SeaweedFS**（S3 相容，Apache 2.0），資料庫僅存 URL
- **零 Copyleft 風險**：資料層 PostgreSQL（PG License）/ Valkey（BSD-3）/ SeaweedFS（Apache 2.0）— 商業 SaaS 部署無授權義務
- **API-First**：所有操作都有 REST / WebSocket API，Swagger 在 `/docs`，可無痛接 CI/CD

---

## 💼 典型應用場景

### 1. QA 團隊建立自動化測試基線
從零建立第一份回歸套件。UI / API / DB 共用一套工具與報告格式；同一份 Markdown 可人工審查也可自動執行，降低人員流動風險。

### 2. DevOps 接入 CI/CD
透過 REST API 觸發執行、查詢報告狀態、下載 Trace；或直接用 `run_tests.py` 把 `tests/` 下的 Markdown 轉成 `.robot` 在 Jenkins / GitHub Actions / GitLab CI 內執行。

### 3. Product / BA 用 Markdown 撰寫驗收標準
產品經理寫的 AC 直接成為測試案例起點，開發 / QA 接手補 BDD 步驟與 DDT 例子，大幅減少需求失真與溝通成本。

### 4. E2E 跨領域測試
一份案例同時驗證 UI 操作 + API 回應 + DB 寫入 + 手機 App 推播。單一報告以時間軸呈現完整使用者旅程，Trace Viewer 可還原每一個瞬間。

---

## 📚 延伸閱讀

- **[操作說明.md](操作說明.md)** — 完整使用教學：從建立專案 → 撰寫測試案例 → 錄製 → 排程 → 執行 → 查看報告的每一步
- **[LICENSES.md](LICENSES.md)** — 第三方授權與 SaaS 商業使用稽核（PostgreSQL / Valkey / SeaweedFS 完全開源）
- **[REST API 文件](http://localhost:8000/docs)** — Swagger UI，平台啟動後可直接互動
- **本機 Agent**：`GET /api/local-runner/agent` 下載 `local_agent.py`，本機有頭執行
- **WebSocket 即時日誌**：`ws://<host>/ws/executions/{task_id}/logs`
- **Playwright Trace Viewer**：<https://trace.playwright.dev/> 可載入平台產出的 `trace.zip`

---

<p align="center">
<b>AutoTest v1.0</b> — 讓自動化測試回歸簡單、透明、可追溯。<br>
<sub>📜 <a href="LICENSES.md">License & Commercial Use</a></sub>
</p>
