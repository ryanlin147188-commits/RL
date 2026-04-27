# 一鍵 build 全部 4 個生產 image (Windows PowerShell)
#
# 用法:
#   .\build\produce.ps1                          # tag=1.0.0,no registry
#   .\build\produce.ps1 -Tag 1.0.1
#   .\build\produce.ps1 -Tag 1.0.0 -Registry "myregistry.example.com/autotest/"

param(
    [string]$Tag = "1.0.0",
    [string]$Registry = ""
)

$ErrorActionPreference = "Stop"

# 進到 repo root
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
Set-Location $RepoRoot

Write-Host "==> Building autotest-* images, tag=$Tag, registry=$($Registry -as [string])" -ForegroundColor Cyan

function Build-One {
    param([string]$Name, [string]$Dockerfile)
    $full = "${Registry}autotest-${Name}:${Tag}"
    Write-Host "─── $Name → $full ───" -ForegroundColor Yellow
    docker build -f $Dockerfile -t $full .
    if ($LASTEXITCODE -ne 0) { throw "Build failed for $Name" }
    Write-Host ""
}

Build-One -Name "backend"  -Dockerfile "build/Dockerfile.backend.production"
Build-One -Name "celery"   -Dockerfile "build/Dockerfile.celery.production"
Build-One -Name "runner"   -Dockerfile "build/Dockerfile.runner.production"
Build-One -Name "frontend" -Dockerfile "build/Dockerfile.frontend.production"

Write-Host "==> All images built. Summary:" -ForegroundColor Green
docker images --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}' |
    Select-String "${Registry}autotest-(backend|celery|runner|frontend)" |
    Sort-Object

Write-Host ""
Write-Host "==> Next steps:" -ForegroundColor Cyan
if ($Registry) {
    Write-Host "    Push images to registry:"
    Write-Host "      docker push ${Registry}autotest-backend:${Tag}"
    Write-Host "      docker push ${Registry}autotest-celery:${Tag}"
    Write-Host "      docker push ${Registry}autotest-runner:${Tag}"
    Write-Host "      docker push ${Registry}autotest-frontend:${Tag}"
    Write-Host ""
    Write-Host "    Customer 端執行:"
    Write-Host "      `$env:AUTOTEST_TAG=`"$Tag`"; `$env:REGISTRY=`"$Registry`""
    Write-Host "      docker compose -f build/docker-compose.production.yml pull"
    Write-Host "      docker compose -f build/docker-compose.production.yml up -d"
} else {
    Write-Host "    Local smoke test:"
    Write-Host "      `$env:AUTOTEST_TAG=`"$Tag`""
    Write-Host "      docker compose -f build/docker-compose.production.yml up -d"
}
