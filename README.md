# AutoTest v1.0

企業級自動化測試平台。前端為單一 `index.html`（vanilla HTML/JS + TailwindCSS CDN，無 build step），由 nginx 直接掛載提供；後端採 FastAPI + Celery Worker（**orchestrator**）+ 內建排程輪詢器 + MySQL + Redis + MinIO。

執行模型：每觸發一個 testcase，Celery worker 透過 Docker SDK 起一個**獨立的 `autotest-robot-runner` 容器**（base = `ppodgorsek/robot-framework`，含 Robot Framework 7.x、robotframework-browser 19.x、Playwright + chromium、ffmpeg），跑完 case 自毀，所有截圖 / 錄影 / Trace 即時上傳到 MinIO；整個 worker 不在自己 process 內跑 robot subprocess。本機 headed 模式（`local_agent.py`）保留作為除錯用途。

## 功能

- 5 層級樹狀目錄管理測試案例（Feature → Platform → Page → Scenario → TestCase）
- 視覺化 ATDD / BDD 步驟編輯與 Data-Driven Testing（DDT）
- 測試案例記錄「驗收準則 (AC) + 前置動作 (Pre-Setup) + BDD 步驟 + DDT 資料」四區塊
- **多來源錄製 / 轉換**：WEB 可用 Playwright codegen；API 可貼 cURL；APP 可貼 Appium Python 腳本轉成步驟
- TopNav 提供 7 個導覽入口：設備資訊、環境變數、DB 資訊、Mock、測試回合、測試報告、排程、錄製
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
- **全專案環境變數 + Android/iOS 設備資訊**：TopNav「環境變數」/「設備資訊」各自一頁，環境變數支援 🎲 Faker 單列/批次產生（姓名、Email、UUID、phone、token、日期等 28 種），設備資訊頁可按「啟動虛擬機」直接起 AVD / iOS Simulator；執行時自動注入成 Robot suite variable（`${BASE_URL}` / `&{DEVICE_pixel5_emu}`），測試步驟可直接引用
- **DB 資訊與 Mock 端點管理**：TopNav「DB 資訊」維護多組 MySQL / PostgreSQL / MSSQL / Oracle / Mongo / Redis / SQLite 連線（含測試連線與 SQL 測試區）；「Mock」維護 REST 端點（可同時編輯「發出 Headers/Body」與「回應 Headers/Body」、支援 `{{name}}` / `{{uuid}}` / `{{int:1,100}}` 等 Faker 佔位符、JSON/List/Error 範本一鍵套用、附 Mock Server 啟停控制）。資料依專案存於瀏覽器 `localStorage`
- **Screenshot Diff（Playwright 風格 UI 前後比對）**：步驟 action 選 `AssertScreenshotMatch` 即啟用；首次跑自動把當下截圖存為 baseline，之後跑用 Pillow + numpy 像素 diff，超過容忍 % 即 FAIL 並產出紅色覆蓋差異圖，報告頁顯示 baseline / actual / diff 三聯比對與「設為新 baseline」捷徑
- **資料庫測試（DatabaseLibrary）增強**：除了原生 `Db.Connect/Query/Execute/RowCount`，新增 `Db.Insert/Update/Delete` 寫入語意明確化、以及 `Db.AssertRowExists/AssertNoRow/AssertValue` 三組斷言，方便驗證 INSERT/UPDATE 後的資料庫狀態
- WebSocket 即時執行日誌（編輯頁底部抽屜）
- 執行報告儀表板（通過率、趨勢圖）與步驟時間軸詳細頁
- **詳細報告依步驟類型分面板**：每個步驟依 action 前綴自動判斷為 UI / API / APP / DB（E2E 案例同一份報告內可混合），呈現不同面板：UI 顯示瀏覽器 pre/post 截圖、API 顯示 Request/Response JSON、APP 顯示手機直式框架內的 Appium 截圖、DB 顯示 SQL 與結果列；PDF 匯出沿用同樣分類呈現
- **最近執行紀錄新增「測試案例 / 目標」欄**：顯示觸發該次執行的節點 title（TESTCASE / PAGE / FEATURE / 測試回合）與 level badge；`/api/reports` 端點會補上 `source_node_id` 與 `source_title`；RUNNING 紀錄每 3 秒自動輪詢刷新，執行完成後也會主動刷一次儀表板

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
| **AssertScreenshotMatch** | Pillow 像素 diff vs baseline；超過容忍 % 即 FAIL | locator（空=整頁）, expected=容忍 %（如 `1.5`）, step UUID 自動帶入 |

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
| Db.Query | 查詢 SELECT；結果 log 出來 | input 或 locator = SQL |
| Db.Execute | 執行任意 SQL（不驗證） | input 或 locator = SQL |
| Db.RowCount | 驗證列數 | input=SQL、expected=預期列數 |
| Db.Insert / Db.Update / Db.Delete | 寫入語意明確化（行為同 Db.Execute） | input 或 locator = SQL |
| Db.AssertRowExists | WHERE 過濾的 SELECT 必須回 ≥ 1 列 | input/locator=SELECT SQL |
| Db.AssertNoRow | WHERE 過濾的 SELECT 必須回 0 列 | input/locator=SELECT SQL |
| Db.AssertValue | SELECT 單格比對固定值（支援 compare）| input=SELECT SQL、expected=值、compare=Equals/Contains/...|

> **典型「驗證寫入」流程**：
> 1. `Db.Insert` → `INSERT INTO users(name,email) VALUES('Alice','a@b.com')`
> 2. `Db.AssertRowExists` → `SELECT 1 FROM users WHERE email='a@b.com'`
> 3. `Db.AssertValue` → `SELECT name FROM users WHERE email='a@b.com'`，expected=`Alice`

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

## 全專案環境變數 + Android/iOS 設備資訊

執行測試時需要的「會跨案例共用」設定（API base URL、token、Appium 虛擬機 capabilities 等）放在「測試回合」頁的左側 sidebar 維護，**整個 project 共用一份**，所有 testcase 執行時自動注入。

### 環境變數 → Robot suite variable

```
name = BASE_URL    value = https://staging.example.com/api
name = API_TOKEN   value = eyJhbGciOi...
```

執行時 `_build_robot_file` 會在生成的 `.robot` 內加入：

```robot
*** Variables ***
${BASE_URL}     https://staging.example.com/api
${API_TOKEN}    eyJhbGciOi...
```

→ 步驟欄位內可直接寫 `${BASE_URL}/users/1`、`Bearer ${API_TOKEN}`，Robot Framework 會自動展開。命名限制：`[A-Za-z_][A-Za-z0-9_]*`（不合法的略過）。

### 設備資訊 → `&{DEVICE_<label>}` dict

每個 device row 注入成 Robot dict 變數，含 Appium capabilities：

```robot
&{DEVICE_pixel5_emu}    platformName=Android    platformVersion=13.0    deviceName=Pixel 5    automationName=UiAutomator2    avd=pixel_5_api_33
&{DEVICE_iphone15}      platformName=iOS        platformVersion=17.4    deviceName=iPhone 15  automationName=XCUITest        udid=B5A2C... 
```

→ `Mobile.Open` 步驟內可寫 `${DEVICE_pixel5_emu.platformName}` / `${DEVICE_pixel5_emu.deviceName}` 引用。`automationName` 沒填會依 platform 自動帶 `UiAutomator2` / `XCUITest`。`extra_caps_json` 可塞額外 capability 一併合併進 dict。

### API
- `GET / PUT /api/projects/{project_id}/env-vars` — list / 整批替換
- `GET / PUT /api/projects/{project_id}/devices` — 同上

PUT 是「整批替換」（delete-then-insert），前端不用維護局部 diff。

### Faker 隨機變數（環境變數頁）

環境變數表每列右側有 🎲 **Faker** 按鈕，點開後可選：

| 類別 | key | 範例 |
|---|---|---|
| 個資 | `name` / `first_name` / `last_name` / `username` | `王小明` / `John` |
| 聯絡 | `email` / `phone_tw` / `phone_us` / `address` / `city` | `abc1f2@gmail.com` / `0912345678` |
| 身分 | `uuid` / `token_hex` / `jwt_like` / `password` | — |
| 數值 | `int_0_100` / `int_1000_9999` / `price` / `bool` | — |
| 時間 | `date_today` / `datetime` / `timestamp` | — |
| 文字 | `paragraph` / `sentence` / `company` / `country` / `zipcode` / `credit_card` / `hex_color` / `url` / `ipv4` | — |

頁首「🎲 批次 Faker」按鈕可一次勾選多種類型，批次以 `FAKE_<KEY>` 為 NAME 加到清單（同名會覆蓋 value）。產生後記得按「儲存」。

### 設備資訊頁：啟動虛擬機

設備編輯右上方有「🟢 啟動虛擬機 / ⏹ 關閉」兩顆按鈕：

- 後端若有實作 `POST /api/devices/launch` / `/api/devices/stop` 就直接打 API 並回報狀態
- 沒實作時前端會把對應本機指令（`emulator -avd ...` / `xcrun simctl boot ...`）複製到剪貼簿並以 Toast 提示
- 狀態 badge：未啟動 / 啟動中… / 執行中 / 啟動失敗

---

## DB 資訊（全專案共用連線設定）

TopNav「🗄 DB 資訊」提供多組資料庫連線設定；左側主從式清單，右側是每筆連線的編輯器與 SQL 測試區。

- 支援 type：MySQL / PostgreSQL / MSSQL / Oracle / MongoDB / Redis / SQLite
- 欄位：name（限英數+底線）、type、host、port（切換 type 會自動帶預設 port）、username、password、database、charset/SSL、自訂 DSN、說明
- 「🔌 測試連線」：打 `POST /api/db/test`（未實作時前端模擬為成功並顯示擬定的 DSN）
- 「SQL / Query 測試區」：輸入任意 SQL → 「執行」打 `POST /api/db/query`，結果 JSON 顯示在下方 terminal；未實作時回傳前端模擬 rows
- Preview 面板同時顯示 Robot 注入格式（`&{DB_<name>}`）、連線 DSN、Python dict
- 資料存 `localStorage[autotest.dbconfigs.<projectId>]`（本機儲存，尚未同步到後端 DB）

---

## Mock 端點管理

TopNav「🔌 Mock」提供輕量的 Mock REST 端點設定；左側主從式清單（method 色塊 + 啟用狀態），右側為單一端點的編輯器。

欄位：
- Method（GET / POST / PUT / PATCH / DELETE / HEAD / OPTIONS）、Path、狀態碼、延遲 ms、Content-Type、啟用 checkbox、說明
- **發出（Request）**：發出 Headers（JSON，選填）、發出 Body（選填，JSON / Form 範本一鍵套）
- **回應（Response）**：回應 Headers（JSON，選填）、回應 Body（JSON / List / Error 範本、JSON 格式化按鈕）

Faker 佔位符支援（在 Headers 或 Body 內）：

```
{{name}} / {{email}} / {{uuid}} / {{token_hex}} / {{phone_tw}} / {{date_today}}
{{int:1,100}}        ← 自訂範圍整數
```

「🛰 試打」按鈕會渲染所有佔位符後，以分段格式顯示完整的 Request 與 Response 內容。

「Mock Server 啟動 / 停止」：
- 打 `POST /api/mock/toggle` body = `{action: 'start'|'stop', endpoints: [...]}`
- 後端若未實作，前端會以模擬狀態標示為「執行中 :4523 / 已停止」

資料存 `localStorage[autotest.mocks.<projectId>]`（本機儲存；後端真正的 mock server 尚未內建）。

---

## Screenshot Diff（Playwright 風格 UI 前後比對）

對 **UI / WEB / E2E** 案例步驟把 action 選 `AssertScreenshotMatch`，即啟用基於 Pillow + numpy 的像素級截圖比對。

### 機制
- 每個 step 都有穩定的 UUID（`steps_json[i].id`），baseline 以此 UUID 為 key 存 MinIO `baselines/<uuid>.png`
- spawn 容器內 `tasks.assert_screenshot_lib` 提供 Robot keyword `AssertScreenshot.Match`，被 .robot 自動呼叫
- baseline **不存在 → auto-save**：把當下截圖存為 baseline → 此次 PASS（首次跑通常是這狀態）
- baseline **存在 → diff**：載入 baseline + 當下截圖，逐像素 RGB 距離 > 30 視為差異
  - 差異 % ≤ 容忍門檻 → PASS
  - 差異 % > 容忍門檻 → FAIL，產出**紅色覆蓋差異圖**上傳 MinIO，DB 記錄 baseline / actual / diff 三個 URL + 實際 diff %

### 步驟欄位
| 欄位 | 用途 |
|---|---|
| action | `AssertScreenshotMatch` |
| locator | 空白 = 整頁；填則只截單一元素 |
| expected | 容忍 %（如 `1.5` 或 `1.5%`，預設 `1.0`）|

### Baseline 維護
- **自動**：第一次跑時 listener 自動把當下截圖當 baseline
- **手動上傳**：步驟列右側 📷 按鈕 → 開 Modal → 上傳 PNG/JPEG/WebP（會覆蓋既有）
- **報告中設定**：報告詳情頁的「把當下 actual 設為新 baseline」按鈕 → 呼叫 `POST /api/steps/{uuid}/baseline/copy-from`，把這次跑的 actual 截圖直接設為新 baseline（適合「現在的畫面才對」的情境）

### API
- `GET /api/steps/{step_uuid}/baseline` — 查現有 baseline + 門檻
- `PUT /api/steps/{step_uuid}/baseline` — multipart 上傳新 PNG（覆蓋舊的）
- `POST /api/steps/{step_uuid}/baseline/copy-from` body=`{source_url, threshold_pct}` — 從 `/results/...` URL 複製成 baseline
- `DELETE /api/steps/{step_uuid}/baseline` — 移除（下次跑會 auto-save 新的）

### 報告呈現
報告詳情頁點該步驟，右側多一塊紫色「Screenshot Diff」面板：
- 三聯比對：**Baseline | Actual | Diff（紅色覆蓋）**
- 標題顯示實際 diff %
- 「把當下 actual 設為新 baseline」按鈕（一鍵解決「baseline 過時」情境）

---

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
- 全專案環境變數：`GET / PUT /api/projects/{id}/env-vars`
- 全專案設備資訊：`GET / PUT /api/projects/{id}/devices`
- Screenshot baseline：`GET / PUT / DELETE /api/steps/{step_uuid}/baseline`、`POST .../baseline/copy-from`
- DB 連線測試 / 查詢（預留，後端尚未實作）：`POST /api/db/test`、`POST /api/db/query`
- Mock Server 啟停（預留）：`POST /api/mock/toggle`
- 虛擬機啟停（預留）：`POST /api/devices/launch`、`POST /api/devices/stop`

常用頁面：

- `http://localhost/`：單頁介面；於頁面上方切換案例編輯 / 測試回合 / 執行報告 / 排程 / 錄製
