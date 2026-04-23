# AutoTest v1.0

企業級自動化測試平台。前端為單一 `index.html`（vanilla HTML/JS + TailwindCSS CDN，無 build step），由 nginx 直接掛載提供；後端採 FastAPI + Celery Worker（**orchestrator**）+ 內建排程輪詢器 + MySQL + Redis + MinIO。

執行模型：每觸發一個 testcase，Celery worker 透過 Docker SDK 起一個**獨立的 `autotest-robot-runner` 容器**（base = `ppodgorsek/robot-framework`，含 Robot Framework 7.x、robotframework-browser 19.x、Playwright + chromium、ffmpeg），跑完 case 自毀，所有截圖 / 錄影 / Trace 即時上傳到 MinIO；整個 worker 不在自己 process 內跑 robot subprocess。本機 headed 模式（`local_agent.py`）保留作為除錯用途。

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
- **每案例 spawn 獨立容器**：Docker 模式下，每個 testcase 由 Celery worker 透過 docker SDK 啟動一個一次性的 `autotest-robot-runner` 容器執行（base = `ppodgorsek/robot-framework`），徹底與 worker 進程隔離；測試結束容器自毀
- **Trace（軌跡追蹤）+ Video（錄影）**：執行時 Browser Library 19.x 在 Playwright context 同時開啟 trace 與 video；listener 即時把截圖／影片／trace 上傳到 MinIO，報告詳細頁可下載、播放、頁內嵌入式 Trace Viewer 檢視
- WebSocket 即時執行日誌（編輯頁底部抽屜）
- 執行報告儀表板（通過率、趨勢圖）與步驟時間軸詳細頁

## 快速啟動（推薦：Docker Compose）

需要：Docker 24+ / Docker Compose v2

```powershell
# 1. 建立 .env（必須有 STORAGE_BACKEND=minio，spawn 模式只走 MinIO）
@"
DB_PASSWORD=password
DB_NAME=autotest_db
BASE_URL=http://localhost
STORAGE_BACKEND=minio
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=minioadmin
"@ | Set-Content .env

# 2. 建 spawn 容器用的 image（每觸發一個 testcase 都會起一個此 image 的容器）
docker build -f backend/Dockerfile.runner -t autotest-robot-runner:latest backend/

# 3. 一鍵啟動所有服務（含建 backend / celery image）
docker compose up -d --build

# 4. 開啟前端
start http://localhost
```

服務埠：

| 服務 | 對外 | 說明 |
|---|---|---|
| frontend (nginx) | 80 | `index.html` 單頁介面 + 反代 /api、/ws、/results |
| backend (FastAPI) | 8000 | REST + WebSocket + 內建 scheduler loop（`/docs` 為 Swagger）|
| mysql | 3306 | 啟動時自動匯入 `backend/migrations/init_schema.sql` |
| redis | 6379 | Celery broker + WS pub/sub |
| celery worker | — | **Orchestrator**：透過 docker SDK + 主機 `/var/run/docker.sock` 為每個 case 起一個 `autotest-robot-runner` 容器跑 Robot |
| autotest-robot-runner | — | 短命容器（一個 case 一個），跑完自毀；image 由 `Dockerfile.runner` build |
| minio | 9000 | `pic` / `results` bucket（spawn 模式所有產物存放處） |
| minio console | 9001 | 物件儲存管理介面 |

停止：`docker compose down`，連資料一起清：`docker compose down -v`

> ⚠ **安全提醒**：celery 容器掛載了 host 的 `/var/run/docker.sock`，等同 root 權限。Demo / Dev 環境可接受；正式部署請改用 docker-socket-proxy 限制 API。

補充：

- backend 啟動時會自動 `create_all()`，並同時啟動排程背景輪詢；目前輪詢間隔是 30 秒。
- `docker-compose.yml` 已將 backend / celery 與 mysql 的時區固定為 `Asia/Taipei`，排程時間與報表時間請以此為準。
- 本機 headed 執行不包含在 Docker Compose 內；如需使用，請從 `/api/local-runner/agent` 下載 agent 腳本並在使用者電腦啟動。
- `autotest-robot-runner` image 不會被 `docker compose up --build` 自動 build（不是 compose 服務），第一次部署或修改 `backend/tasks/robot_*.py` / `Dockerfile.runner` 後都要手動執行步驟 2。

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

# 兩個終端機分開啟動（不使用 Docker Compose 時的最小組態）
python run.py                                       # T1 後端 (http://localhost:8000)
celery -A tasks.celery_app worker -l info           # T2 worker
```

直接用瀏覽器開 `index.html`（檔案 `file://...` 也可，但建議透過 nginx 反代以正確載入 `/api`、`/ws`、`/pics`）。
最簡作法是用 Docker Compose 啟動 frontend 服務（`docker compose up -d frontend`），即可在 <http://localhost/> 使用單頁介面。

說明：

- `http://localhost/` 是 nginx（frontend 容器）對外的單頁介面，內容即專案根目錄的 `index.html`。
- backend 啟動後會自動建立資料表並啟動排程輪詢，不需要另外再開 scheduler 行程。

## 環境變數

`backend/.env`（被 FastAPI 與 Celery 讀取；需自行建立）：

| 變數 | 預設 | 說明 |
|---|---|---|
| DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME | localhost / 3306 / root / password / autotest_db | MySQL |
| REDIS_URL | redis://localhost:6379/0 | Celery broker + WS |
| PIC_FOLDER | ./PIC | 留作本機 / `local_agent.py` 模式上傳區暫存；spawn 模式下不使用 |
| BASE_URL | http://localhost | 對外可訪問的 URL 前綴（截圖 / 影片 / Trace 都用此前綴拼接） |
| RECORDER_HOST_ROOT | C:\Demo\autotest_v1.0_20260420 | 錄製一鍵 PowerShell 指令切換用的本機專案根目錄 |
| **STORAGE_BACKEND** | **minio** | spawn 模式必須是 `minio`；改 `local` 會在執行時報錯 |
| MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY | http://minio:9000 / minioadmin / minioadmin | spawn 模式必填 |
| APP_HOST / APP_PORT | 0.0.0.0 / 8000 | uvicorn |
| DEBUG | True | uvicorn reload |
| PLAYWRIGHT_HEADLESS | 1 | spawn 容器內是否使用 headless Chromium（headed 走 xvfb） |

`.env`（給 docker-compose 讀取；需自行建立）：

| 變數 | 預設 | 說明 |
|---|---|---|
| DB_PASSWORD | password | MySQL root |
| DB_NAME | autotest_db | DB 名稱 |
| BASE_URL | http://localhost | URL 前綴（透過 nginx 反代 /results） |
| **STORAGE_BACKEND** | **minio** | spawn 模式必須是 `minio` |
| MINIO_ROOT_USER / MINIO_ROOT_PASSWORD | minioadmin / minioadmin | MinIO 管理帳密 |
| PLAYWRIGHT_HEADLESS | 1 | spawn 容器內是否使用 headless |
| ROBOT_RUNNER_IMAGE | autotest-robot-runner:latest | spawn 用的 image tag；改成自己的 registry 也可以 |
| ROBOT_RUNNER_NETWORK | autotest_v10_20260420_default | spawn 容器要附加的 docker network；必須能連到 `minio` 與 `redis` 服務名 |

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
  app/                 FastAPI（routers / services / models / schemas / ws）
    static/            local_agent.py 下載腳本
  tasks/
    execution_tasks.py Celery 任務入口
    robot_runner.py    Orchestrator：產 .robot → 上傳 MinIO → docker SDK spawn 容器 → 抓回結果
    robot_container.py spawn 容器 entrypoint：拉 .robot → 跑 robot → 上傳產物
    robot_listener.py  Robot Framework Listener v3：即時上傳截圖 + 收集影片/Trace
  migrations/          init_schema.sql
  Dockerfile           backend (FastAPI) image
  Dockerfile.celery    celery worker image（含 docker SDK；不含 Robot 執行環境）
  Dockerfile.runner    spawn 容器 image（FROM ppodgorsek/robot-framework）
index.html             前端唯一入口（vanilla HTML/JS + TailwindCDN，由 nginx 直接掛載）
nginx.conf             前端容器的 nginx 設定（反代 /api /ws /results 並補 CORS）
run_tests.py           Markdown -> Robot CLI runner（與平台分離的獨立工具）
tests/                 Markdown / pytest / Robot 測試資產
docker-compose.yml
```

## 排程、測試回合與本機執行

### 執行環境

- `Docker`：Celery worker 為每個 testcase 動態 spawn 一個 `autotest-robot-runner` 容器跑 Robot Framework，預設 headless（容器內走 xvfb），跑完容器自毀。**所有產物（截圖 / 完整錄影 / trace.zip）即時上傳到 MinIO，僅以 URL 寫回 DB**。適合持續整合與無頭環境。
- `本機`：由使用者電腦上的 `local_agent.py` 認領任務並開啟有頭 Chromium；適合除錯與示範。截圖透過 `/api/local-runner/upload-screenshot` 上傳，目前仍走 `local` 儲存路徑。
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

## Trace（軌跡追蹤）+ Video（錄影）

spawn 容器內 Browser Library 19.x 在 New Context 時帶入 `tracing=True` 與 `recordVideo`，listener 在執行過程中即時把截圖上傳到 MinIO，case 結束關閉 context 後 listener 再把完整 `.webm` 與 `trace.zip` 也上傳。所有產物統一存於 MinIO `results` bucket：

```
results/
├── inputs/<task_id>/<case_tag>.robot           ← worker 上傳的 .robot 輸入
├── results-json/<task_id>/<case_tag>.json      ← spawn 容器跑完的 step 結果
├── screenshots/<report_id>/<uuid>_<test>_sNN_pre.png    ← 每步前後截圖（即時上傳）
├── screenshots/<report_id>/<uuid>_<test>_sNN_post.png
├── videos/<report_id>/<test_name>.webm         ← 每個案例 / DDT 列一份完整錄影
└── traces/<report_id>/<test_name>.zip          ← 每個案例 / DDT 列一份 Playwright trace
```

對外存取走 nginx 反代：`http://localhost/results/<key>`（已開 CORS `*` 供 trace.playwright.dev 跨網域 fetch）。

啟用 / 關閉：

- 編輯頁的「執行」按鈕旁有齒輪設定，預設「啟用 Trace + Video」為開啟。
- 也可直接呼叫 API：`POST /api/executions` body 加上 `"enable_recording": false` 即可關閉。
- 關閉後 listener 不處理 video / trace（截圖仍上傳），可降低執行時間與磁碟占用。

報告頁呈現方式（執行報告 → 點報告 → 點任一步驟）：

- 「完整錄影」按鈕：頁內 Modal 播放整個案例錄影（同 case 所有步驟共用）
- 「下載錄影」按鈕：直接下載 `.webm`
- 「下載 Trace」按鈕：直接下載 `trace.zip`，可用 `playwright show-trace` 在本機開啟
- 「Trace Viewer ↗」按鈕：新分頁載入 `https://trace.playwright.dev/?trace=<absolute_URL>`
- 「嵌入檢視」按鈕：頁內 iframe 嵌入 Trace Viewer
  - 已用 `proxy_hide_header` 移除 MinIO 自帶的 ACAO，避免重複 header 被瀏覽器拒絕
  - 純內網部署無公開網址時，仍可用「下載 Trace」+ `playwright show-trace` 離線檢視

> 步驟切片影片功能已移除（之前用 ffmpeg 切片，使用者反饋只要完整錄影即可）。如需回退，可參考 git 歷史 `92df467` 之前的版本。

依賴（已在 image 內）：

- `ffmpeg`：仍保留在 `Dockerfile.celery` 與 `Dockerfile.runner`（部分 Browser Library 內部會用到）。
- `boto3` / `redis` / `sqlalchemy`：在 `Dockerfile.runner` 內加裝，供 listener 直接連 MinIO 與 Redis。

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
- 截圖 / 錄影 / Trace：`http://localhost/results/{key}`（spawn 模式，nginx → MinIO，已開 CORS）
- 本機 Agent 上傳區（保留兼容）：`http://localhost/pics/{key}`
- Playwright Trace Viewer：<https://trace.playwright.dev/?trace=>`<absolute_url>`（自動由前端產生）

常用頁面：

- `http://localhost/`：單頁介面；於頁面上方切換案例編輯 / 測試回合 / 執行報告 / 排程 / 錄製
