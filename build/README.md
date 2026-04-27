# 生產 image build pipeline (Tier 2 程式碼保護)

把核心商業邏輯編譯成 `.so` 二進位、前端 JS 重度混淆,以便客戶 `docker pull` 取得 image 時**不會看到原始碼**。

> ⚠ **這份不是給客戶看的**。給維運 / 開發者用,客戶只會收到 image + 一份 `docker-compose.production.yml`。

---

## 它做什麼

| 層級 | 模組 | 處理方式 | 反編譯成本 |
|---|---|---|---|
| 第 1 層 | `tasks/robot_runner.py`、`tasks/robot_listener.py`、`tasks/assert_screenshot_lib.py`、`app/services/*.py`、`app/auth/security.py`、`app/auth/crypto.py` | **Cython → `.so`**(C-extension 二進位) | 高(需要 IDA / Ghidra,專業攻擊者也要好幾天)|
| 第 2 層 | `app/routers/*.py`、`app/middleware.py`、`app/audit.py`、`app/rate_limit.py`、`tasks/celery_app.py`、`tasks/execution_tasks.py`、`tasks/robot_container.py` | **bytecode-only `.pyc`**(刪 `.py`) | 中(uncompyle6 半小時可還原 80%) |
| 第 3 層 | `app/main.py`、`app/config.py`、`app/database.py`、`app/models/*.py`、`app/schemas/*.py`、`app/auth/dependencies.py` | 留 `.py`(Pydantic / SQLAlchemy 反射重) | 低(明文)— 但這些檔案的祕方=0,schema 本來就在 Swagger 看得到 |
| 前端 | `frontend/index.html` `<script>` 區塊 | **`javascript-obfuscator`** 重度混淆(stringArray + control flow flattening + dead code injection) | 中-高(deobfuscator 半天,但 hot path 仍極度難讀) |

詳細的「為什麼不用 PyArmor / WebAssembly / license server」解釋見 [`alm-transient-iverson.md` 計畫文件](../../../Users/fasta/.claude/plans/alm-transient-iverson.md)(本機 plan 檔)。

---

## 怎麼 build

### 一鍵腳本

```bash
# bash
./build/produce.sh 1.0.0
# 或指定 registry prefix:
./build/produce.sh 1.0.0 myregistry.example.com/autotest/
```

```powershell
# PowerShell
.\build\produce.ps1 -Tag 1.0.0
# 或:
.\build\produce.ps1 -Tag 1.0.0 -Registry "myregistry.example.com/autotest/"
```

腳本會逐一 build 4 個 image:

| Image | Dockerfile | 預估 build 時間 | 預估大小 |
|---|---|---|---|
| `autotest-backend:<tag>` | `Dockerfile.backend.production` | 2-3 min | ~ 750 MB |
| `autotest-celery:<tag>` | `Dockerfile.celery.production` | 6-8 min(含 Playwright base) | ~ 2 GB |
| `autotest-runner:<tag>` | `Dockerfile.runner.production` | 4-6 min | ~ 2.5 GB |
| `autotest-frontend:<tag>` | `Dockerfile.frontend.production` | 30-45 sec | ~ 50 MB |

### Build 內部流程(每個 image 都是 multi-stage)

1. **Builder stage**:裝 `build-essential` + `Cython`,跑 `compile_python.sh`:
   - `cython_setup.py build_ext --inplace` → 第 1 層產出 `.so`
   - `python -m compileall -b -f` → 第 2 層產出 `.pyc`
   - 把 `dist/backend/` 組好,刪除第 1 + 2 層的原 `.py`
2. **Runtime stage**:從 builder COPY `dist/backend/` 過去;builder 的 build tools / Cython / 中間 `.c` 檔不會帶到 runtime。

### 編譯後 image 內容驗證

```bash
# Backend image 內找 .so(應該有 12 個)
docker run --rm --entrypoint sh autotest-backend:1.0.0 -c \
    'find /app -name "*.so" | wc -l'
# 12

# Backend image 內找 routers .py(應該為 0)
docker run --rm --entrypoint sh autotest-backend:1.0.0 -c \
    'find /app/app/routers -name "*.py" | wc -l'
# 0

# Backend image 內找 robot_runner.py(應該為 0)
docker run --rm --entrypoint sh autotest-backend:1.0.0 -c \
    'find /app -name "robot_runner.py"'
# (空)

# Frontend image 內找原內部變數(應該為 0)
docker run --rm --entrypoint sh autotest-frontend:1.0.0 -c \
    'grep -c "_homeTodos\|_kanbanLastDefects\|_defectsCache" /usr/share/nginx/html/index.html'
# 0
```

---

## 怎麼測

### 本機 smoke test

```bash
# 1) build 全部
./build/produce.sh local

# 2) export tag(對應到上面 build 出來的 1.0.0)
export AUTOTEST_TAG=local

# 3) 啟動全套
docker compose -f build/docker-compose.production.yml up -d

# 4) 等 30 秒讓 init_db 跑完,然後測 API
sleep 30
curl -s -X POST http://localhost/api/auth/login \
    -H 'Content-Type: application/json' \
    -d '{"username":"admin","password":"admin123"}'

# 5) 進瀏覽器 http://localhost,把 5 大主流程都點一輪:
#    建專案 → 寫案例 → 跑 → 看報告 → 看 RTM 追溯鏈
```

### Image 體積對比(僅供參考)

| | Dev 版(原 `.py`)| 生產版(`.so` + `.pyc`)| 差異 |
|---|---|---|---|
| backend | 720 MB | 750 MB | +30 MB(`.so` 略大,加上 setuptools build artifacts)|
| celery | 1.95 GB | 2.05 GB | +100 MB |
| runner | 2.4 GB | 2.55 GB | +150 MB |
| frontend | 45 MB | 50 MB | +5 MB(混淆後 JS 大 2.5x) |

---

## 怎麼 push 到 registry

### Docker Hub(公開)

```bash
docker tag autotest-backend:1.0.0 yourdockerhubusername/autotest-backend:1.0.0
docker push yourdockerhubusername/autotest-backend:1.0.0
# ... 其他三個同樣
```

### 私有 registry(推薦)

只給付費客戶帳密,降低 image 散佈風險:

```bash
docker login myregistry.example.com
./build/produce.sh 1.0.0 myregistry.example.com/autotest/
docker push myregistry.example.com/autotest/autotest-backend:1.0.0
# ... 其他三個同樣
```

### GitHub Container Registry

```bash
echo $GITHUB_TOKEN | docker login ghcr.io -u <username> --password-stdin
./build/produce.sh 1.0.0 ghcr.io/yourorg/
docker push ghcr.io/yourorg/autotest-backend:1.0.0
```

---

## 客戶端怎麼用

寄給客戶的東西:**只有一個 `docker-compose.production.yml`**(他們 pull image,不需要 source code)。

```bash
# 客戶 step 1:登入你的 registry(如果是私有)
docker login myregistry.example.com

# 客戶 step 2:設定環境變數
export AUTOTEST_TAG=1.0.0
export REGISTRY=myregistry.example.com/autotest/

# 客戶 step 3:pull + 啟動
docker compose -f docker-compose.production.yml pull
docker compose -f docker-compose.production.yml up -d
```

---

## 安全保證 ≠ 不可破

老實說:

- **Cython `.so` 不是真正不可逆**。專業攻擊者用 IDA Pro / Ghidra 配合 Cython runtime symbols,還是可以還原成 C 風格的 pseudo-code(雖然不會回到原 Python),需要好幾天 + 專業技能。**一般競爭對手不會做**
- **Pydantic schemas / SQLAlchemy models 留 `.py`**,意味著表結構、API 形狀、enum 值全看得到。但這些東西本來就會在 `/docs`(Swagger UI)看到,**保護它們沒有意義**
- **Frontend JS 即使重度混淆**,熟悉 obfuscator 的人用 `webcrack` / `de4js` 半天也能還原成 readable JS。但 hot path 的核心邏輯混在亂碼 + dead code 裡,**靜態閱讀仍是極度痛苦**
- **沒有 license server / phone-home**,所以一旦客戶有 image,合約結束後對方可繼續離線跑。要擋這個只能上 Tier 3。**Tier 2 的核心保護是「raise the cost」,不是「prevent」**

---

## 加碼防禦(未來可選)

| 升級 | 工作量 | 效果 |
|---|---|---|
| **PyArmor 商業版($299/年)** 取代第 2 層 `.pyc` | 半天 | bytecode 加密 + 30 天 license expiry,提高第 2 層保護到接近第 1 層 |
| **License server** + phone-home 啟動驗證 | 1-2 週 | 客戶離線不能跑;合約結束自動凍結 |
| **WebAssembly** 重寫前端 hot-path 邏輯 | 2-3 週 | 完全擋住 JS deobfuscator(.wasm 不容易反編譯到 readable code) |
| **Cosign signed images** | 1 天 | 客戶端可驗證 image 沒被竄改 |

需要這些升級時請改 [`build/Dockerfile.*.production`](.) 跟 `build/cython_setup.py`,並更新本檔。

---

## 維護 checklist

當你新增 backend 模組時,問自己 3 個問題:

1. **這是商業祕方嗎?**(關鍵演算法、複雜流程、客戶很想抄的)→ 加進 [`build/cython_setup.py`](cython_setup.py) 的 `MODULES_TO_COMPILE`
2. **Pydantic / SQLAlchemy / FastAPI 反射很重嗎?**(`Mapped[]`、`mapped_column()`、`Depends()`、`model_config`)→ 留 `.py`(第 3 層)
3. **介於兩者之間?**(普通 CRUD wrapper)→ 加進 [`build/compile_python.sh`](compile_python.sh) 的第 2 層 compileall 列表

當你新增前端功能時:
- 如果用 `onclick="myNewFunc()"` 在 HTML inline 引用全域 function → **必須在 [`build/obfuscate_frontend.mjs`](obfuscate_frontend.mjs) 的 `reservedNames` 加入該 function 名稱**(否則 obfuscator 會混淆掉它,inline handler 就找不到)

---

## 故障排除

| 症狀 | 可能原因 | 對策 |
|---|---|---|
| `docker compose up` 後 backend 一直 restart,log 顯示 `ModuleNotFoundError: No module named 'tasks.robot_runner'` | `.so` 檔的 Python 版本跟 runtime 不對(builder 用 3.11,runtime 用 3.13) | 確認 `Dockerfile.*.production` 的 builder + runtime Python 版本一致 |
| Runner 容器啟動立刻 exit | `tasks/robot_container.py` 沒被正確編譯成 `.pyc`,且原 `.py` 已刪 | 檢查 `compile_python.sh` 是否處理該檔;ENTRYPOINT 必須用 `python3 -m tasks.robot_container`(module form) |
| 前端打開後某些按鈕沒反應 | obfuscator 把 inline `onclick=` 引用的全域 function 名混淆掉了 | 在 [`obfuscate_frontend.mjs`](obfuscate_frontend.mjs) 的 `reservedNames` 加入該 function 名 |
| Cython 編譯某個 service 失敗 | 該檔用了 `Mapped[]` / `model_config` / 動態 import 等 Cython 不支援的 pattern | 把該檔退回第 2 層(從 `cython_setup.py` 移除,加進 `compile_python.sh` 的 compileall 列表) |
| Build 速度很慢 | 沒用 BuildKit cache;Playwright / ppodgorsek base 沒 cache | 確保 Docker Desktop 開了 BuildKit;首次 build 慢正常,之後會快 |
