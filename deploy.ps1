# =============================================================================
# AutoTest 一鍵部署腳本 (Windows PowerShell)
# -----------------------------------------------------------------------------
#   用法：  .\deploy.ps1                部署並啟動全部服務
#           .\deploy.ps1 -Stop          停止但保留資料
#           .\deploy.ps1 -Reset         停止並清空所有資料（破壞性，需再確認）
#           .\deploy.ps1 -Logs          即時跟著 compose logs
#           .\deploy.ps1 -Status        顯示容器狀態
# -----------------------------------------------------------------------------
#   若出現「無法執行指令碼...執行原則」警告，執行一次：
#     Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
# =============================================================================
[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$Reset,
    [switch]$Logs,
    [switch]$Status
)

$ErrorActionPreference = 'Stop'

# ── 彩色輸出 helper ──────────────────────────────────────────────────
function Write-Info    { param($msg) Write-Host "[INFO]  $msg" -ForegroundColor Cyan }
function Write-Success { param($msg) Write-Host "[OK]    $msg" -ForegroundColor Green }
function Write-Warn    { param($msg) Write-Host "[WARN]  $msg" -ForegroundColor Yellow }
function Write-Err     { param($msg) Write-Host "[ERROR] $msg" -ForegroundColor Red }
function Write-Step    { param($msg) Write-Host ""; Write-Host "▶ $msg" -ForegroundColor Cyan -NoNewline; Write-Host "" }

# ── 切到腳本所在目錄（允許從任何位置執行）─────────────────────────
Set-Location -Path $PSScriptRoot

# ── 檢查 Docker ──────────────────────────────────────────────────────
function Test-DockerEnvironment {
    Write-Step "檢查 Docker 環境"
    $dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $dockerCmd) {
        Write-Err "找不到 docker 指令。請先安裝 Docker Desktop：https://docs.docker.com/get-docker/"
        exit 1
    }
    try { docker info 2>$null | Out-Null } catch {}
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Docker daemon 沒在跑。請開啟 Docker Desktop 後再試。"
        exit 1
    }
    try { docker compose version 2>$null | Out-Null } catch {}
    if ($LASTEXITCODE -ne 0) {
        Write-Err "找不到 docker compose v2。請更新 Docker Desktop。"
        exit 1
    }
    $dv = (docker --version) -replace 'Docker version ', '' -replace ',.*', ''
    $cv = docker compose version --short
    Write-Success "Docker $dv / Compose $cv"
}

# ── 檢查專案目錄 ─────────────────────────────────────────────────────
function Test-ProjectDir {
    if (-not (Test-Path 'docker-compose.yml')) { Write-Err "找不到 docker-compose.yml；請確認腳本放在專案根目錄。"; exit 1 }
    if (-not (Test-Path 'backend')) { Write-Err "找不到 backend/ 資料夾。"; exit 1 }
}

# ── 隨機 secret 產生器 ───────────────────────────────────────────────
function New-RandomHex {
    param([int]$Bytes = 32)
    $b = New-Object byte[] $Bytes
    $rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
    try { $rng.GetBytes($b) } finally { $rng.Dispose() }
    -join ($b | ForEach-Object { $_.ToString('x2') })
}

function New-FernetKey {
    # Fernet key = url-safe base64(32 random bytes),保留 padding `=` 也接受
    $b = New-Object byte[] 32
    $rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
    try { $rng.GetBytes($b) } finally { $rng.Dispose() }
    [Convert]::ToBase64String($b).Replace('+', '-').Replace('/', '_')
}

# ── 建立 / 補齊 .env ─────────────────────────────────────────────────
function Initialize-EnvFile {
    $envPath = Join-Path $PSScriptRoot '.env'
    if (-not (Test-Path $envPath)) {
        Write-Info "未偵測到 .env,自動產生隨機密碼/secret..."
        $now = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        $dbPwd  = New-RandomHex -Bytes 24
        $s3Pwd  = New-RandomHex -Bytes 24
        $jwt    = New-RandomHex -Bytes 32
        $fernet = New-FernetKey
        $envContent = @"
# AutoTest 部署設定(自動產生於 $now)
# 此檔內含隨機密碼,請妥善保管,切勿 commit 進 git。
DB_USER=admin
DB_PASSWORD=$dbPwd
DB_NAME=autotest_db
BASE_URL=http://localhost
ALLOWED_ORIGINS=http://localhost
STORAGE_BACKEND=s3
S3_ROOT_USER=admin
S3_ROOT_PASSWORD=$s3Pwd
AUTOTEST_JWT_SECRET=$jwt
AUTOTEST_FERNET_KEY=$fernet
"@
        # 強制 UTF-8 無 BOM,docker-compose 讀取才不會拿到怪字元
        [System.IO.File]::WriteAllText($envPath, $envContent, (New-Object System.Text.UTF8Encoding $false))
        Write-Success ".env 已建立(全部 secret 隨機產生)"
        return
    }
    Write-Info ".env 已存在,檢查必要欄位..."
    $existing = Get-Content $envPath -Encoding UTF8
    $required = @(
        @{ Key = 'DB_USER';              Value = 'admin' }
        @{ Key = 'DB_PASSWORD';          Value = (New-RandomHex -Bytes 24) }
        @{ Key = 'DB_NAME';              Value = 'autotest_db' }
        @{ Key = 'BASE_URL';             Value = 'http://localhost' }
        @{ Key = 'ALLOWED_ORIGINS';      Value = 'http://localhost' }
        @{ Key = 'STORAGE_BACKEND';      Value = 's3' }
        @{ Key = 'S3_ROOT_USER';         Value = 'admin' }
        @{ Key = 'S3_ROOT_PASSWORD';     Value = (New-RandomHex -Bytes 24) }
        @{ Key = 'AUTOTEST_JWT_SECRET';  Value = (New-RandomHex -Bytes 32) }
        @{ Key = 'AUTOTEST_FERNET_KEY';  Value = (New-FernetKey) }
    )
    $appended = 0
    $newLines = New-Object System.Collections.ArrayList
    foreach ($r in $required) {
        $pattern = '^' + [regex]::Escape($r.Key) + '='
        if (-not ($existing -match $pattern)) {
            [void]$newLines.Add(("{0}={1}" -f $r.Key, $r.Value))
            $appended++
        }
    }
    if ($appended -gt 0) {
        $appendBlock = ($newLines -join "`n") + "`n"
        [System.IO.File]::AppendAllText($envPath, $appendBlock, (New-Object System.Text.UTF8Encoding $false))
        Write-Warn (".env 已附加 {0} 個缺少的 key(舊值未變動)" -f $appended)
    } else {
        Write-Success ".env 完整"
    }
}

# ── 建 Robot Runner 容器 image ───────────────────────────────────────
function New-RunnerImage {
    docker image inspect 'autotest-robot-runner:1.0.0' 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Info "autotest-robot-runner 已存在（跳過重建；需更新請先 docker rmi autotest-robot-runner:1.0.0）"
        return
    }
    Write-Info "建 Robot Runner 容器 image（第一次約 3-5 分鐘）..."
    docker build -f backend/Dockerfile.runner -t autotest-robot-runner:1.0.0 backend/
    if ($LASTEXITCODE -ne 0) { Write-Err "Runner image 建置失敗。"; exit 1 }
    Write-Success "Runner image 已建置"
}

# ── 建 Docker 模式錄製 image（autotest-recorder） ────────────────────
# Phase 1:容器內跑 Xvfb + noVNC + Playwright codegen,讓使用者透過瀏覽器
# iframe 在伺服器側錄製,免裝 Playwright。
function New-RecorderImage {
    docker image inspect 'autotest-recorder:1.0.0' 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Info "autotest-recorder 已存在（跳過重建；需更新請先 docker rmi autotest-recorder:1.0.0）"
        return
    }
    Write-Info "建 Recorder 容器 image（含 noVNC + Playwright，第一次約 3-5 分鐘）..."
    docker build -f backend/Dockerfile.recorder -t autotest-recorder:1.0.0 backend/
    if ($LASTEXITCODE -ne 0) { Write-Err "Recorder image 建置失敗。"; exit 1 }
    Write-Success "Recorder image 已建置"
}

# ── 建 API 模式錄製 image (autotest-recorder-api：mitmproxy + HAR addon) ────
function New-RecorderApiImage {
    docker image inspect 'autotest-recorder-api:1.0.0' 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Info "autotest-recorder-api 已存在（跳過重建；需更新請先 docker rmi autotest-recorder-api:1.0.0）"
        return
    }
    Write-Info "建 Recorder-API 容器 image (mitmproxy；第一次約 1-2 分鐘)..."
    docker build -f backend/Dockerfile.recorder-api -t autotest-recorder-api:1.0.0 backend/
    if ($LASTEXITCODE -ne 0) { Write-Err "Recorder-API image 建置失敗。"; exit 1 }
    Write-Success "Recorder-API image 已建置"
}

# ── 建 MCP image (autotest-mcp:Playwright MCP server) ──────────────
function New-McpImage {
    docker image inspect 'autotest-mcp:1.0.0' 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Info "autotest-mcp 已存在（跳過重建;需更新請先 docker rmi autotest-mcp:1.0.0）"
        return
    }
    Write-Info "建 MCP 容器 image (Playwright MCP;第一次約 2-3 分鐘)..."
    docker build -f backend/Dockerfile.mcp -t autotest-mcp:1.0.0 backend/
    if ($LASTEXITCODE -ne 0) { Write-Err "MCP image 建置失敗。"; exit 1 }
    Write-Success "MCP image 已建置"
}

# ── 啟動 Compose ─────────────────────────────────────────────────────
function Start-ComposeStack {
    Write-Step "啟動服務（docker compose up -d --build）"
    docker compose up -d --build
    if ($LASTEXITCODE -ne 0) { Write-Err "compose up 失敗。"; exit 1 }
    Write-Success "Compose 已啟動"
}

# ── 等待 frontend 可訪問 ─────────────────────────────────────────────
function Wait-ForReady {
    Write-Step "等待服務就緒"
    $maxWait = 120
    $waited = 0
    while ($waited -lt $maxWait) {
        try {
            $resp = Invoke-WebRequest -Uri 'http://localhost/' -Method Head -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
            if ($resp.StatusCode -eq 200) {
                Write-Success "前端可訪問 <http://localhost/>"
                return
            }
        } catch {}
        Start-Sleep -Seconds 2
        $waited += 2
        Write-Host '.' -NoNewline
    }
    Write-Host ''
    Write-Warn "等待 ${maxWait}s 後仍沒回應；請檢查 'docker compose logs frontend backend'。"
}

# 驗證 PostgreSQL 連線(POSTGRES_USER / POSTGRES_PASSWORD 由 image 在首次啟動建立)
function Initialize-PostgresAdmin {
    # 從 .env 讀取 DB_USER / DB_NAME(密碼不需要,pg_isready 不用密碼)
    $dbUser = 'admin'
    $dbName = 'autotest_db'
    $envPath = Join-Path $PSScriptRoot '.env'
    if (Test-Path $envPath) {
        foreach ($line in Get-Content $envPath -Encoding UTF8) {
            if ($line -match '^DB_USER=(.+)$') { $dbUser = $matches[1].Trim() }
            elseif ($line -match '^DB_NAME=(.+)$') { $dbName = $matches[1].Trim() }
        }
    }
    Write-Step ("驗證 PostgreSQL 連線({0} / {1})" -f $dbUser, $dbName)
    # 注意:$args 是 PowerShell 自動變數,用 $cmdArgs 避免衝突
    $cmdArgs = @('exec', 'autotest-postgres', 'pg_isready', '-U', $dbUser, '-d', $dbName)
    docker @cmdArgs 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Success "PostgreSQL 接受連線" }
    else { Write-Warn "尚無法連 PostgreSQL;若仍在啟動中可忽略,或檢查 docker logs autotest-postgres" }
}

# ── 開啟瀏覽器 ───────────────────────────────────────────────────────
function Open-Browser { Start-Process 'http://localhost/' }

# ── 印出橫幅 ─────────────────────────────────────────────────────────
function Show-Banner {
    Write-Host ''
    Write-Host '════════════════════════════════════════════════════════════' -ForegroundColor Green
    Write-Host '    ✨  AutoTest 部署完成！ ✨' -ForegroundColor Green
    Write-Host '════════════════════════════════════════════════════════════' -ForegroundColor Green
    Write-Host ''
    Write-Host '  🌐 前端  : '       -NoNewline; Write-Host 'http://localhost/' -ForegroundColor Cyan
    Write-Host '  📘 API  : '        -NoNewline; Write-Host 'http://localhost:8000/docs' -ForegroundColor Cyan -NoNewline; Write-Host '  (Swagger UI)'
    Write-Host '  🗄  SeaweedFS S3: ' -NoNewline; Write-Host 'http://localhost:8333/' -ForegroundColor Cyan -NoNewline; Write-Host '   (帳密見 .env)'
    Write-Host '  📊 日誌  : '        -NoNewline; Write-Host 'http://localhost:9428/select/vmui/' -ForegroundColor Cyan -NoNewline; Write-Host '  (VictoriaLogs)'
    Write-Host ''
    Write-Host '  首次部署:資料庫沒有任何使用者,請執行以下指令建立 admin:' -ForegroundColor Yellow
    Write-Host '    docker compose exec backend python -m app.cli create-admin' -ForegroundColor Cyan
    Write-Host ''
    Write-Host '  常用指令:'
    Write-Host '    .\deploy.ps1 -Status   看容器狀態'
    Write-Host '    .\deploy.ps1 -Logs     看即時 log'
    Write-Host '    .\deploy.ps1 -Stop     停掉服務(保留資料)'
    Write-Host '    .\deploy.ps1 -Reset    停掉並清空所有資料(破壞性)'
    Write-Host ''
    Write-Host '  資料庫 / S3 密碼存放在 .env(已加入 .gitignore;切勿 commit)。' -ForegroundColor Yellow
    Write-Host ''
}

# ── 子命令分派 ───────────────────────────────────────────────────────
if ($Stop) {
    Test-DockerEnvironment; Test-ProjectDir
    Write-Info '停止服務...'
    docker compose down
    Write-Success '已停止（資料保留在 volumes 裡，下次 deploy 可直接恢復）'
    exit 0
}
if ($Reset) {
    Test-DockerEnvironment; Test-ProjectDir
    Write-Warn '此動作會刪除所有 PostgreSQL / SeaweedFS volumes，測試資料、報告、截圖都會消失！'
    $yn = Read-Host '確定要繼續嗎？[y/N]'
    if ($yn -notmatch '^[Yy]$') { Write-Info '已取消。'; exit 0 }
    docker compose down -v
    Write-Success '已重置（下次 deploy 會以全新狀態啟動）'
    exit 0
}
if ($Logs) {
    Test-DockerEnvironment; Test-ProjectDir
    docker compose logs -f
    exit 0
}
if ($Status) {
    Test-DockerEnvironment; Test-ProjectDir
    docker compose ps
    exit 0
}

# ── 預設流程：部署 ───────────────────────────────────────────────────
Test-DockerEnvironment
Test-ProjectDir
Initialize-EnvFile
New-RunnerImage
New-RecorderImage
New-RecorderApiImage
New-McpImage
Start-ComposeStack
Wait-ForReady
Initialize-PostgresAdmin
Show-Banner
Open-Browser
