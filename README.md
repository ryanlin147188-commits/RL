# AutoTest v1.0

企業級自動化測試平台。預設以 nginx 提供根目錄 `index.html` 單頁介面，後端採 FastAPI + Celery Worker（**Robot Framework** 執行引擎）+ 內建排程輪詢器 + MySQL + Redis；支援 Docker headless 執行與本機 `local_agent.py` headed 執行。倉庫內同時保留 `frontend/` 的 React + Vite 專案供開發模式使用。

## 功能

- 5 層級樹狀目錄管理測試案例（Feature → Platform → Page → Scenario → TestCase）
- 視覺化 ATDD / BDD 步驟編輯與 Data-Driven Testing（DDT）
- 測試案例記錄「驗收準則 (AC) + 前置動作 (Pre-Setup) + BDD 步驟 + DDT 資料」四區塊
- **多來源錄製 / 轉換**：WEB 可用 Playwright codegen；API 可貼 cURL；APP 可貼 Appium Python 腳本轉成步驟
- TopNav 提供五種工作模式：案例編輯、測試回合、執行報告、排程、錄製
- 全站可切換兩種執行環境：Docker（容器內 headless）與本機 Agent（本機 headed Chromium）
- **自動化排程**：支援單次、每天、每週、每月四種觸發規則，UI 可多選多筆 `.md` 測試案例後一起排程
- **測試回合**：可把多筆 `.md` 測試案例儲存成命名集合，一鍵彙總執行並產生單一報告
- **Robot Framework** + Browser Library / RequestsLibrary / DatabaseLibrary / AppiumLibrary 統一執行引擎
  - Web UI ：Browser Library（Playwright 為底層，含步驟前後截圖）
  - HTTP API ：RequestsLibrary
  - SQL ：DatabaseLibrary
  - Mobile ：AppiumLibrary（需外接 Appium server）
- **本機 Agent**：下載 `local_agent.py` 後可由本機直接認領 local 模式任務，視覺化觀察瀏覽器執行過程，並把每步 PRE / POST 截圖與耗時回寫到詳細報告
- WebSocket 即時執行日誌（編輯頁底部抽屜）
- 執行報告儀表板（通過率、趨勢圖）與步驟時間軸詳細頁

## 快速啟動（推薦：Docker Compose）

需要：Docker 24+ / Docker Compose v2

```powershell
# 1. 複製 compose 用環境變數（目前 .env.example 僅含最基本三項）
Copy-Item .env.example .env

# 2. 一鍵啟動所有服務
docker compose up -d --build

# 3. 開啟前端
start http://localhost
```

服務埠：

| 服務 | 對外 | 說明 |
|---|---|---|
| frontend (nginx) | 80 | 單頁介面 + 反代 /api、/ws、/pics、/results |
| backend (FastAPI) | 8000 | REST + WebSocket + 內建 scheduler loop（`/docs` 為 Swagger）|
| mysql | 3306 | 啟動時自動匯入 `backend/migrations/init_schema.sql` |
| redis | 6379 | Celery broker + WS pub/sub |
| celery worker | — | 內含 Robot Framework + Browser Library + Chromium |
| minio | 9000 | `pic` / `results` bucket（切換 `STORAGE_BACKEND=minio` 時使用） |
| minio console | 9001 | 物件儲存管理介面 |

停止：`docker compose down`，連資料一起清：`docker compose down -v`

補充：

- backend 啟動時會自動 `create_all()`，並同時啟動排程背景輪詢；目前輪詢間隔是 30 秒。
- `docker-compose.yml` 已將 backend 與 celery 的時區固定為 `Asia/Taipei`，排程時間與報表時間請以此為準。
- 本機 headed 執行不包含在 Docker Compose 內；如需使用，請從 `/api/local-runner/agent` 下載 agent 腳本並在使用者電腦啟動。

## 本機開發（不用 Docker）

需要：Python 3.11+、Node 20+、MySQL 8、Redis 7

```powershell
# 後端
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Robot Framework Browser Library 需要 Node.js 20+ 並初始化
rfbrowser init                            # 會下載 Playwright JS + Chromium

# 建立 backend/.env（此檔目前需自行建立，repo 未附 backend/.env.example）
@"
DB_HOST=localhost
DB_PORT=3306
DB_USER=root
DB_PASSWORD=password
DB_NAME=autotest_db
REDIS_URL=redis://localhost:6379/0
PIC_FOLDER=./PIC
BASE_URL=http://localhost:8000
STORAGE_BACKEND=local
APP_HOST=0.0.0.0
APP_PORT=8000
DEBUG=True
"@ | Set-Content .env

# 初始化資料庫（任一方式）
mysql -uroot -p < migrations/init_schema.sql
# 或啟動後端時自動 create_all（lifespan 會跑 init_db()）

# 三個終端機分開啟動
python run.py                                       # T1 後端
celery -A tasks.celery_app worker -l info           # T2 worker

# React/Vite 開發前端（選用）
cd ..\frontend
npm install
npm run dev                                         # T3 前端 (http://localhost:3000)
```

說明：

- `http://localhost/` 是 Docker Compose 預設對外頁面，實際由 nginx 載入根目錄的 `index.html`。
- `http://localhost:3000/` 是 React/Vite 開發站，會透過 Vite proxy 轉發 `/api` 與 `/ws` 到後端。
- backend 啟動後會自動建立資料表並啟動排程輪詢，不需要另外再開 scheduler 行程。

## 環境變數

`backend/.env`（被 FastAPI 與 Celery 讀取；需自行建立）：

| 變數 | 預設 | 說明 |
|---|---|---|
| DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME | localhost / 3306 / root / password / autotest_db | MySQL |
| REDIS_URL | redis://localhost:6379/0 | Celery broker + WS |
| PIC_FOLDER | ./PIC | 截圖目錄 |
| BASE_URL | http://localhost:8000 | 對外可訪問的截圖 URL 前綴 |
| RECORDER_HOST_ROOT | C:\Demo\autotest_v1.0_20260420 | 錄製一鍵 PowerShell 指令切換用的本機專案根目錄 |
| STORAGE_BACKEND | local | 截圖與附件儲存方式：`local` 或 `minio` |
| MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY | http://minio:9000 / minioadmin / minioadmin | 啟用 MinIO 儲存時使用 |
| APP_HOST / APP_PORT | 0.0.0.0 / 8000 | uvicorn |
| DEBUG | True | uvicorn reload |
| PLAYWRIGHT_HEADLESS | 1 | celery worker 環境變數，設 0 開有頭模式（僅本機） |

`.env`（給 docker-compose；`.env.example` 目前只預放前三項，其餘可自行追加）：

| 變數 | 預設 | 說明 |
|---|---|---|
| DB_PASSWORD | password | MySQL root |
| DB_NAME | autotest_db | DB 名稱 |
| BASE_URL | http://localhost | 截圖 URL 前綴（透過 nginx 反代 /pics） |
| STORAGE_BACKEND | local | Docker Compose 預設走本機檔案儲存；MinIO 服務仍會一併啟動供切換使用 |
| MINIO_ROOT_USER / MINIO_ROOT_PASSWORD | minioadmin / minioadmin | MinIO 管理帳密 |
| PLAYWRIGHT_HEADLESS | 1 | Celery 容器內是否使用 headless Chromium |

## 測試案例資料模型

`testcase_contents` 表（PK = `node_id`，對應 TESTCASE 層級的 tree node）：

| 欄位 | 型別 | 說明 |
|---|---|---|
| `ac_text` | TEXT | 驗收準則 (Acceptance Criteria) 純文字 |
| `setup_text` | TEXT | **前置動作 (Pre-Setup)** 純文字：記錄 seed DB / 取得 token / 啟動 mock server 等執行前需要手動準備的事項 |
| `steps_json` | JSON | BDD 步驟陣列（見下一節 action 表） |
| `ddt_json`  | JSON | 後端仍使用 `{ headers: string[], rows: string[][] }`；目前單頁 UI 預設維護 `No / 檔案名稱 / Key / Value` 四欄 |

> 備註：`setup_text` 目前為「說明型」文字，供人工閱讀。若需自動執行前置動作，請將指令寫入 BDD 步驟。
>
> DDT 執行語意：前端「執行測試」目前固定送出 `ddt_expand=false`，因此不會依每列重跑整個 testcase。若要逐列展開，需直接呼叫 `/api/executions` 並把 `ddt_expand=true` 一併送出。

## 測試案例步驟（steps_json）支援的 action

內部會將 steps_json 動態組成 `.robot` 檔並交由 Robot Framework 執行。不同前綴選用不同 Library。

### Web UI（Browser Library）— 預設、無需前綴

| action | 說明 | 必填欄位 |
|---|---|---|
| Goto / Navigate / Open | 開啟網址 | input 或 expected = URL |
| Click / DoubleClick / RightClick | 點擊 | locator |
| Fill / Input | 填入文字（會清空） | locator, input |
| Type | 逐字輸入 | locator, input |
| Press | 按鍵 | locator, input（如 `Enter`）|
| Hover | 滑鼠移入 | locator |
| Check / Uncheck | 勾選 / 取消 | locator |
| Select | 下拉選單 | locator, input |
| Wait / Sleep | 等待毫秒 | input（數字）|
| WaitForSelector | 等待元素出現 | locator |
| AssertVisible / AssertHidden | 元素可見 / 隱藏 | locator |
| AssertText | 元素內含文字 | locator, expected |
| AssertValue | 表單欄位值 | locator, expected |
| AssertUrl | 當前 URL 內含 | expected |

### HTTP API（RequestsLibrary）— 前綴 `Http.`

| action | 說明 | 欄位對應 |
|---|---|---|
| Http.Get | GET | locator=URL、expected=預期狀態碼 |
| Http.Post / Http.Put / Http.Patch | POST/PUT/PATCH | locator=URL、input=JSON body、expected=狀態碼 |
| Http.Delete | DELETE | locator=URL、expected=狀態碼 |

### SQL（DatabaseLibrary）— 前綴 `Db.`

| action | 說明 | 欄位對應 |
|---|---|---|
| Db.Connect | 建立連線 | input=`driver|host|port|user|pwd|db`（driver 如 `pymysql`）|
| Db.Query | 查詢 SELECT | input 或 locator = SQL |
| Db.Execute | 執行 INSERT/UPDATE/DELETE | input 或 locator = SQL |
| Db.RowCount | 驗證列數 | input=SQL、expected=預期列數 |

### Mobile（AppiumLibrary）— 前綴 `Mobile.`

| action | 說明 | 欄位對應 |
|---|---|---|
| Mobile.Open | 開 App | locator=Appium server URL、input=platformName |
| Mobile.Click | 點擊元素 | locator |
| Mobile.Input | 輸入文字 | locator, input |
| Mobile.Tap | Tap | locator |

**DDT 補充**：後端 runner 仍支援 `${headerName}` / `$headerName` 形式的變數替換；但目前單頁 UI 主要把 DDT 當資料來源表管理，若要啟用逐列展開執行，需改由 API 送出 `ddt_expand=true`。

## 專案結構

```
backend/
  app/            FastAPI（routers / services / models / schemas / ws）
    static/       local_agent.py 下載腳本
  tasks/          Celery 任務 + Robot Framework runner / listener
  migrations/     init_schema.sql
  Dockerfile / Dockerfile.celery
frontend/
  src/            React + Vite 開發版前端（http://localhost:3000）
  Dockerfile / nginx.conf
index.html        Docker Compose 預設首頁（目前實際交付 UI）
run_tests.py      Markdown -> Robot CLI runner
tests/            Markdown / pytest / Robot 測試資產
docker-compose.yml
```

## 排程、測試回合與本機執行

### 執行環境

- `Docker`：由 Celery 容器執行，預設 headless；適合持續整合與無頭環境。
- `本機`：由使用者電腦上的 `local_agent.py` 認領任務並開啟有頭 Chromium；適合除錯與示範。
- 單頁 UI 會把環境切換狀態存到瀏覽器 `localStorage`，手動執行與排程「立即」都會沿用這個預設。

### 本機 Agent

1. 從 `GET /api/local-runner/agent` 下載 `local_agent.py`。
2. 安裝一次性依賴：

```powershell
pip install playwright requests
playwright install chromium
```

3. 啟動 agent：

```powershell
python local_agent.py --server http://localhost
```

補充：

- agent 執行每一步前後都會嘗試截圖，並透過 backend 上傳後寫入詳細報告的時間軸。
- local 模式的截圖 URL 會走 `/pics/{report_id}/{filename}`，因此 `BASE_URL` 應設定為使用者可實際連回平台的網址。

限制：

- 本機 agent 目前只支援 Web UI 類 action；`Http.*` 與 `Mobile.*` 請改用 Docker 模式。
- `Upload / Download` 的檔案路徑是以 agent 執行目錄為準。
- `SwitchTab` 在本機 agent 上僅提供有限支援；複雜多分頁流程建議仍使用 Docker。

### 排程

- 後端提供 `ONCE / DAILY / WEEKLY / MONTHLY` 四種排程規則。
- 排程背景輪詢每 30 秒掃描一次到期工作。
- 單頁 UI 的排程清單支援啟用 / 停用、立即執行、編輯與刪除。
- 單頁 UI 的排程建立視窗目前列出 `.md` 測試案例，支援搜尋與多選；建立後會把所選案例彙總成一次排程執行。
- 若要對更高層節點（Scenario / Page / Feature）排程，需改由 API 建立。

### 測試回合

- 單頁 UI 提供獨立的「測試回合」工作區，可建立具名稱、說明與預設執行環境的一組案例集合。
- 建立回合時可搜尋並多選 `.md` 測試案例；執行時會把全部所選案例彙總成一份報告。
- 回合列表會顯示案例數、預設執行環境與建立時間，並支援執行、編輯、刪除。
- 從單頁 UI 按「執行」時，會以目前頂部環境切換為準；若未傳 `execution_mode`，API 才會退回使用回合本身的預設值。
- 若回合同時包含多筆案例，現階段建議優先使用 Docker 模式；本機 Agent 較適合單案例或單節點回合。

## Markdown 匯出與 CLI 執行

- 編輯頁右上角的「匯出 MD」按鈕會呼叫 `GET /api/testcases/{node_id}/export-md`，下載目前 TESTCASE 的 Markdown。
- 後端另外提供 `POST /api/testcases/{node_id}/import-md` 可把 Markdown 解析回測試案例內容；目前預設 UI 尚未提供匯入按鈕。
- `run_tests.py` 會把 `tests/` 下的 Markdown 測試轉成 `.robot` 後執行：

```powershell
python run_tests.py
python run_tests.py -f tests/e2e/samples/integration_test.md
python run_tests.py -t "登入測試案例"
```

## 錄製功能（WEB / API / APP）

於首頁 TopNav 切換到「🎬 錄製」模式，目前提供三種來源：

- `WEB`：Playwright codegen / rfbrowser codegen，產生 `recorded.py` 與 `trace.zip`
- `API`：貼上瀏覽器 DevTools 的 `Copy as cURL`，解析成 `Http.*` 步驟
- `APP`：貼上 Appium Python 腳本，解析成 `Mobile.*` 步驟

以下為 WEB 錄製流程：

1. 先到「案例編輯」選取一筆 TESTCASE，再切到「錄製」頁；套用步驟時會直接合併到目前選中的案例。
2. 輸入目標 URL → 點「建立錄製階段」。
3. 複製任一指令到本機終端機執行（四種方式擇一）：
   - **A) Node.js npx**（免安裝）：`npx -y playwright codegen --save-trace=... -o ...`
   - **B) Python pip**（已裝 playwright）：`python -m playwright codegen --save-trace=...`
   - **C) rfbrowser codegen**（robotframework-browser）
   - **D) PowerShell 一鍵**：codegen + 自動 curl 上傳（建議）
4. 操作真實瀏覽器視窗；關閉後本機產生 `recorded_xxxx.py` 與 `trace_xxxx.zip`。
5. **想讓步驟自動帶出「比對條件 / 預期結果」**：在 Playwright Inspector 工具列點選
   `Assert visibility`、`Assert text` 或 `Assert value`，再點頁面元素。
   後端解析後自動填入 Condition / Expected 兩欄。
6. 把兩個檔案拖到 ③ 上傳區，或使用一鍵 PowerShell 自動上傳。
7. 點「套用至當前案例」，步驟即合併至右側編輯器；若尚未選中 TESTCASE，系統會提示先返回案例編輯頁。
8. `trace.zip` 可從頁面「下載 trace.zip」按鈕取得，於 <https://trace.playwright.dev> 開啟分析。

補充：

- 長 Locator（例如 `role=heading[name="登入"]`）在步驟表中可透過水平捲動與滑鼠懸停 Tooltip 查看完整內容。
- 錄製轉換支援常見斷言：`to_be_visible()`、`to_have_text()`、`to_contain_text()`、`to_have_value()`。

---

## 關鍵端點

- REST：`http://localhost:8000/docs`（Swagger 全清單；所有路由掛在 `/api/...`）
- WebSocket 即時日誌：`ws://localhost/ws/executions/{task_id}/logs`
- 排程 API：`http://localhost/api/schedules`、`POST /api/schedules/{id}/trigger-now`
- 測試回合 API：`http://localhost/api/rounds`、`POST /api/rounds/{id}/execute`
- 本機 Agent：`GET /api/local-runner/agent`、`POST /api/local-runner/claim`、`POST /api/local-runner/upload-screenshot`、`POST /api/local-runner/tasks/{task_id}/complete`
- 截圖靜態檔：`http://localhost/pics/{key}`（local 模式）
- Robot HTML 報表與附件：`http://localhost/results/{key}`（`STORAGE_BACKEND=minio` 時）
- Playwright Trace Viewer：<https://trace.playwright.dev>（上傳 trace.zip 後離線分析）

常用頁面：

- `http://localhost/`：預設單頁介面；於頁面上方切換案例編輯 / 測試回合 / 執行報告 / 排程 / 錄製
- `http://localhost:3000/`：React/Vite 開發前端（執行 `npm run dev` 時）
