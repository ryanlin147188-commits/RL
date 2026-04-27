"""Cython 編譯設定 — 把核心商業邏輯模組編譯成 .so 二進位。

只挑「真的是商業祕方」的模組,避免:
- SQLAlchemy `Mapped[]` / `mapped_column()` 重反射的 model 檔
- Pydantic v2 `BaseModel` / `model_config` 重反射的 schema 檔
- FastAPI `Depends()` 簽章反射的 dependency 檔

build 流程(由 build/compile_python.sh 觸發):
    cd /build/backend && python ../cython_setup.py build_ext --inplace

關鍵:必須從 backend/ 內執行,因為 Cython 以 module qualified name(tasks.robot_runner、
app.services.markdown_service)決定 .so inplace 落地位置。從 /build 跑會嘗試
建立 /build/tasks/robot_runner.so 但該資料夾不存在。

產出物:每個 .py 旁會多一個 .cpython-<py-ver>-<arch>-linux-gnu.so;
compile_python.sh 之後會把 .so cp 到 dist/ 並刪除原 .py。
"""
from __future__ import annotations

from pathlib import Path

from setuptools import setup
from Cython.Build import cythonize


# 第 1 層:Cython 編譯(.so 二進位)
# 路徑相對 backend/(setup.py 從 backend/ 內執行)
MODULES_TO_COMPILE = [
    # 核心:Robot Framework 翻譯 + 像素 diff(平台最秘方)
    "tasks/robot_runner.py",
    "tasks/assert_screenshot_lib.py",
    "tasks/robot_listener.py",
    # 業務 services
    "app/services/markdown_service.py",
    "app/services/execution_service.py",
    "app/services/schedule_service.py",
    "app/services/tree_service.py",
    "app/services/ai_test_gen.py",
    "app/services/oidc_service.py",
    "app/services/storage_service.py",
    # 認證 / 加密邏輯
    "app/auth/security.py",
    "app/auth/crypto.py",
]


def _existing(paths: list[str]) -> list[str]:
    """過濾掉不存在的檔案,避免 build 因缺檔失敗(例如 dev 改名)。"""
    out = []
    for p in paths:
        if Path(p).exists():
            out.append(p)
        else:
            print(f"[cython_setup] skip missing: {p}")
    return out


setup(
    name="autotest_compiled",
    ext_modules=cythonize(
        _existing(MODULES_TO_COMPILE),
        compiler_directives={
            "language_level": "3",
            # FastAPI / SQLAlchemy / Celery 內部廣泛用 keyword arguments;
            # Cython 預設會對 def 做最佳化但拒絕 **kwargs,必須 always_allow_keywords
            "always_allow_keywords": True,
            # 保留 docstring(否則 FastAPI 自動產的 Swagger 描述會掉)
            "embedsignature": True,
        },
    ),
    script_args=["build_ext", "--inplace"],
)
