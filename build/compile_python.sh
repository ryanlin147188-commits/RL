#!/bin/sh
# 在 builder stage 內跑;假設 cwd = /build,/build/backend/ 是完整 repo 副本。
# 流程:
#   1) Cython 編譯第 1 層模組(產 .so 在 backend/ 各位置)
#   2) 第 2 層用 compileall -b 產 PEP 488 bytecode-only .pyc
#   3) 把整個 backend/ 樹複製到 dist/backend/,然後 strip 掉第 1 + 2 層的原 .py
set -eu

cd /build

echo "==> Step 1: Cython compile (第 1 層 → .so)"
# 必須從 backend/ 內執行,讓 Cython 用相對 module path 落地 .so
cd backend && python ../cython_setup.py build_ext --inplace
cd /build

echo "==> Step 2: bytecode-only compile (第 2 層 → .pyc)"
python -m compileall -b -f -q backend/app/routers
python -m compileall -b -f -q \
    backend/app/middleware.py \
    backend/app/audit.py \
    backend/app/rate_limit.py \
    backend/tasks/execution_tasks.py \
    backend/tasks/celery_app.py \
    backend/tasks/robot_container.py

echo "==> Step 3: 組裝 dist/"
mkdir -p dist
cp -r backend dist/backend

# 清掉 dist/ 內第 1 層原 .py(.so 已經在同位置)
echo "==> Step 4a: strip 第 1 層 .py(保留 .so)"
# 從 cython_setup.py 抽出模組清單(已經是相對 backend/ 的路徑)
PY1_LIST=$(grep -E '^\s*"[^"]+\.py"' cython_setup.py | sed -E 's/.*"([^"]+\.py)".*/\1/' | grep -v '^$')
for src in $PY1_LIST; do
    dist_src="dist/backend/$src"
    if [ -f "$dist_src" ]; then
        echo "  rm $dist_src"
        rm -f "$dist_src"
    fi
done

# 清掉 dist/ 內第 2 層原 .py(.pyc 已存在於同層)
echo "==> Step 4b: strip 第 2 層 .py(保留 .pyc)"
find dist/backend/app/routers -name "*.py" -delete
rm -f \
    dist/backend/app/middleware.py \
    dist/backend/app/audit.py \
    dist/backend/app/rate_limit.py \
    dist/backend/tasks/execution_tasks.py \
    dist/backend/tasks/celery_app.py \
    dist/backend/tasks/robot_container.py

# 順手清 __pycache__/
find dist -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# 把 Cython 中間產物清掉
echo "==> Step 5: 清 Cython 中間檔(.c / build/)"
find dist -name "*.c" -delete 2>/dev/null || true
rm -rf dist/backend/build 2>/dev/null || true

echo "==> Done. dist/backend 結構摘要:"
echo "── .so files ──"
find dist/backend -name "*.so" | sort
echo "── routers (應只有 .pyc)──"
find dist/backend/app/routers -type f | head -10
echo "── 剩下的 .py(應只有 main/config/database/models/schemas/auth/dependencies)──"
find dist/backend -name "*.py" | sort
