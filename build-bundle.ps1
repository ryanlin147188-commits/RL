# =============================================================================
# build-bundle.ps1 — 一鍵打包 AutoTest 為單一 Docker image
#
# 產出：autotest_v1.0:latest（以及 :v1.0）
#
# 用法：
#   .\build-bundle.ps1             # 建置 bundle image
#   .\build-bundle.ps1 -Run        # 建置完並直接啟動
#   .\build-bundle.ps1 -Save       # 建置完另存成 .tar 以便離線散佈
# =============================================================================
[CmdletBinding()]
param(
    [switch]$Run,
    [switch]$Save
)

$ErrorActionPreference = 'Stop'

function Write-Info    { param($msg) Write-Host "[INFO]  $msg" -ForegroundColor Cyan }
function Write-Success { param($msg) Write-Host "[OK]    $msg" -ForegroundColor Green }
function Write-Warn    { param($msg) Write-Host "[WARN]  $msg" -ForegroundColor Yellow }
function Write-Err     { param($msg) Write-Host "[ERROR] $msg" -ForegroundColor Red }
function Write-Header  { param($msg) Write-Host ""; Write-Host "▶ $msg" -ForegroundColor Cyan; Write-Host "" }

Set-Location -Path $PSScriptRoot

$ImageRepo = 'autotest_v1.0'
$ImageTag = 'latest'
$ImageTagV = 'v1.0'
$Image = "${ImageRepo}:${ImageTag}"
$ImageV = "${ImageRepo}:${ImageTagV}"
$TarFile = 'autotest_v1.0-bundle.tar'

# 檢查 Docker
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Write-Err '找不到 docker 指令'; exit 1 }
try { docker info 2>$null | Out-Null } catch {}
if ($LASTEXITCODE -ne 0) { Write-Err 'Docker daemon 沒在跑，請啟動 Docker Desktop'; exit 1 }

Write-Header "Build $Image（以 Ubuntu 24.04 為基底，打包整個專案）"
docker build -f Dockerfile.bundle -t $Image -t $ImageV .
if ($LASTEXITCODE -ne 0) { Write-Err 'Build 失敗'; exit 1 }
Write-Success "已建立：$Image 與 $ImageV"

$size = (docker images --format '{{.Size}}' $Image | Select-Object -First 1)
Write-Info "Image 大小：$size"

if ($Save) {
    Write-Header "匯出 image 為 $TarFile（離線散佈用）"
    docker save -o $TarFile $Image
    $tarSize = (Get-Item $TarFile).Length / 1MB
    Write-Success ("已輸出：{0} ({1:N1} MB)" -f $TarFile, $tarSize)
    Write-Info "在目標機器載入：docker load -i $TarFile"
}

if ($Run) {
    Write-Header '啟動 bundle（掛 host docker.sock 與工作目錄）'
    $pwdPath = (Get-Location).Path
    docker run --rm -it `
        -v '/var/run/docker.sock:/var/run/docker.sock' `
        -v "${pwdPath}:${pwdPath}" -w $pwdPath `
        $Image install
}

# ── 使用說明 ─────────────────────────────────────────────────────────
Write-Host ''
Write-Host '══════════════════════════════════════════════════════════' -ForegroundColor Green
Write-Host '  AutoTest v1.0 Bundle 已建立完成'                          -ForegroundColor Green
Write-Host '══════════════════════════════════════════════════════════' -ForegroundColor Green
Write-Host ''
Write-Host '  一鍵部署到新機器：' -ForegroundColor White
Write-Host ''
Write-Host '    docker run --rm -it `' -ForegroundColor Gray
Write-Host '        -v /var/run/docker.sock:/var/run/docker.sock `' -ForegroundColor Gray
Write-Host '        -v "${PWD}:${PWD}" -w $PWD `' -ForegroundColor Gray
Write-Host "        $Image" -ForegroundColor Gray
Write-Host ''
Write-Host '  只解壓不部署：' -ForegroundColor White
Write-Host ''
Write-Host "    docker run --rm -v `"`${PWD}:`${PWD}`" -w `$PWD $Image extract" -ForegroundColor Gray
Write-Host ''
Write-Host '  離線散佈：' -ForegroundColor White
Write-Host ''
Write-Host '    .\build-bundle.ps1 -Save    # 產出 ' -NoNewline -ForegroundColor Gray
Write-Host $TarFile -ForegroundColor Gray
Write-Host '    # 在另一台機器：' -ForegroundColor Gray
Write-Host "    docker load -i $TarFile" -ForegroundColor Gray
Write-Host ''
