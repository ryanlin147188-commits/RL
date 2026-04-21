# AutoTest v1.0

企業級自動化測試平台。前端 React + 後端 FastAPI + Celery Worker（**Robot Framework** 執行引擎）+ MySQL + Redis。

## 功能

- 5 層級樹狀目錄管理測試案例（Feature → Platform → Page → Scenario → TestCase）
- 視覺化 ATDD / BDD 步驟編輯與 Data-Driven Testing（DDT）
- 測試案例記錄「驗收準則 (AC) + 前置動作 (Pre-Setup) + BDD 步驟 + DDT 資料」四區塊
- **Playwright codegen 瀏覽器錄製**：一鍵產生 BDD 步驟（含 trace.zip 供 Trace Viewer 分析）
- TopNav 提供三種工作模式：案例編輯、執行報告、錄製
- **Robot Framework** + Browser Library / RequestsLibrary / DatabaseLibrary / AppiumLibrary 統一執行引擎
  - Web UI ：Browser Library（Playwright 為底層，含步驟前後截圖）
  - HTTP API ：RequestsLibrary
  - SQL ：DatabaseLibrary
  - Mobile ：AppiumLibrary（需外接 Appium server）
- WebSocket 即時執行日誌（編輯頁底部抽屜）
- 執行報告儀表板（通過率、趨勢圖）與步驟時間軸詳細頁

## 快速啟動（推薦：Docker Compose）

需要：Docker 24+ / Docker Compose v2

```powershell
# 1. （可選）設定密碼
Copy-Item .env.example .env

# 2. 一鍵啟動所有服務
docker compose up -d --build

# 3. 開啟前端
start http://localhost
```

服務埠：

| 服務 | 對外 | 說明 |
|---|---|---|
| frontend (nginx) | 80 | SPA + 反代 /api、/ws、/pics |
| backend (FastAPI) | 8000 | REST + WebSocket（`/docs` 為 Swagger）|
| mysql | 3306 | 啟動時自動匯入 `backend/migrations/init_schema.sql` |
| redis | 6379 | Celery broker + WS pub/sub |
| celery worker | — | 內含 Robot Framework + Browser Library + Chromium |

停止：`docker compose down`，連資料一起清：`docker compose down -v`

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

Copy-Item .env.example .env               # 編輯 DB_PASSWORD 等

# 初始化資料庫（任一方式）
mysql -uroot -p < migrations/init_schema.sql
# 或啟動後端時自動 create_all（lifespan 會跑 init_db()）

# 三個終端機分開啟動
python run.py                                       # T1 後端
celery -A tasks.celery_app worker -l info           # T2 worker
cd ..\frontend ; npm install ; npm run dev          # T3 前端 (http://localhost:3000)
```

## 環境變數

`backend/.env`（被 FastAPI 與 Celery 讀取）：

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

`.env`（給 docker-compose）：

| 變數 | 預設 | 說明 |
|---|---|---|
| DB_PASSWORD | password | MySQL root |
| DB_NAME | autotest_db | DB 名稱 |
| BASE_URL | http://localhost | 截圖 URL 前綴（透過 nginx 反代 /pics） |
| STORAGE_BACKEND | local | Docker Compose 預設走本機檔案儲存；MinIO 服務仍會一併啟動供切換使用 |
| MINIO_ROOT_USER / MINIO_ROOT_PASSWORD | minioadmin / minioadmin | MinIO 管理帳密 |

## 測試案例資料模型

`testcase_contents` 表（PK = `node_id`，對應 TESTCASE 層級的 tree node）：

| 欄位 | 型別 | 說明 |
|---|---|---|
| `ac_text` | TEXT | 驗收準則 (Acceptance Criteria) 純文字 |
| `setup_text` | TEXT | **前置動作 (Pre-Setup)** 純文字：記錄 seed DB / 取得 token / 啟動 mock server 等執行前需要手動準備的事項 |
| `steps_json` | JSON | BDD 步驟陣列（見下一節 action 表） |
| `ddt_json`  | JSON | `{ headers: string[], rows: string[][] }` |

> 備註：`setup_text` 目前為「說明型」文字，供人工閱讀。若需自動執行前置動作，請將指令寫入 BDD 步驟。

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

**DDT 變數替換**：locator / input / expected 內可寫 `${headerName}` 或 `$headerName`，
runner 會逐列代入 ddt_json 的 rows 並為每一列產生一個 Robot Test Case。

## 專案結構

```
backend/
  app/            FastAPI（routers / services / models / schemas / ws）
  tasks/          Celery 任務 + Robot Framework runner / listener
  migrations/     init_schema.sql
  Dockerfile / Dockerfile.celery
frontend/
  src/            React + Zustand + AntD + Chart.js
  Dockerfile / nginx.conf
docker-compose.yml
```

## 錄製功能（Playwright codegen）

於 TopNav 點選「🎬 錄製」頁（`/recorder`）：

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
- 截圖靜態檔：`http://localhost/pics/{report_id}/{tag}.png`
- Playwright Trace Viewer：<https://trace.playwright.dev>（上傳 trace.zip 後離線分析）

常用頁面：

- `http://localhost/editor`：案例編輯
- `http://localhost/reports`：執行報告
- `http://localhost/recorder`：瀏覽器錄製
