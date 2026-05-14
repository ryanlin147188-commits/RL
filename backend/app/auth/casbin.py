"""Casbin Enforcer singleton + helpers。

進程內 Casbin enforcer:RBAC-with-domains 模型(model file 在
``app/auth/casbin_model.conf``),policy 持久化到 PostgreSQL 的
``casbin_rule`` 表(casbin-sqlalchemy-adapter 1.5.x)。

設計取捨:

* **同步 Enforcer**:pycasbin 1.36 沒有 async API,enforce 本身 O(policy size)
  純記憶體運算,微秒級。要在 FastAPI async handler 內呼叫時直接 ``enforce()``
  即可 — 沒必要 wrap 進 thread executor。policy reload(從 DB 重抓)才會
  有 IO,放在 background task / endpoint mutate 後手動觸發。
* **同步 DB adapter**:adapter 用 SQLAlchemy 同步 engine。我們開一個獨立的
  sync engine(走 ``SYNC_DATABASE_URL``),只給 Casbin 用。小 pool(5 connections)
  夠 — adapter 只在 startup / policy mutation 時才打 DB。
* **CASBIN_ENABLED gate**:跟 Casdoor 一樣 opt-in。為 False 時 ``get_enforcer``
  回 None,``enforce`` 函式短路回 False,讓 shadow-mode 自己決定要不要 log。
* **單例 + lock**:在 lifespan 內呼叫 ``init_enforcer``;之後從 module-level
  ``_enforcer`` 拿。Hot-reload(``load_policy``)會被 mutate API call(Phase
  2.4 的 sync 層)觸發,讀寫透過 enforcer 自己的 RLock 保護。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Opt-in gate — Phase 2 起就要能在沒有 Casdoor 的情況下單獨打開做 shadow,
# 所以給它一支獨立的 env 而不是綁 CASDOOR_ENABLED。
_ENABLED_RAW = os.environ.get("CASBIN_ENABLED", "False").strip().lower()
_CASBIN_ENABLED: bool = _ENABLED_RAW in {"true", "1", "yes", "on"}

# 獨立的 shadow gate(Phase 3.1):需要 enforcer 已 init 且 policy 已 seed,
# 才會在 require_permission 內跑 shadow comparison。預設 False,讓操作者明確
# 切開來,避免 seed 前的「Casbin 永遠 deny」洗 log。
_SHADOW_RAW = os.environ.get("CASBIN_SHADOW_ENABLED", "False").strip().lower()
_CASBIN_SHADOW_ENABLED: bool = _SHADOW_RAW in {"true", "1", "yes", "on"}

# Model 檔在這個模組旁邊,進 image 時跟著 app/auth/ 一起 COPY 進去。
_MODEL_PATH: Path = Path(__file__).resolve().parent / "casbin_model.conf"

_enforcer = None  # type: ignore[var-annotated]


def is_enabled() -> bool:
    return _CASBIN_ENABLED


def is_shadow_enabled() -> bool:
    return _CASBIN_SHADOW_ENABLED


def get_enforcer():
    """回 enforcer 物件;未初始化或 disabled 時回 None。"""
    return _enforcer


def init_enforcer(force: bool = False) -> None:
    """在 FastAPI lifespan 內呼叫一次。

    建一個獨立的 sync SQLAlchemy engine 給 adapter 用 — adapter 在 init
    時會自動 create ``casbin_rule`` 表(checkfirst,跟既有表共存)。

    為什麼不寫 Alembic migration 來建表:adapter 自己會建,自己 ORM 化
    casbin_rule 物件,我們手刻 migration 反而要追 adapter 內部欄位定義
    隨版本飄移。保留「init 時 auto-create」單一真相。

    ``force=True``:即使 ``CASBIN_ENABLED=False`` 也照樣 init — 給 CLI seed
    指令用,讓操作者可以在還沒切開 gate 的情況下先把 policy 灌進 DB。
    """
    global _enforcer
    if not _CASBIN_ENABLED and not force:
        logger.info("CASBIN_ENABLED=False — skipping Casbin enforcer init")
        return
    if _enforcer is not None:
        return

    import casbin
    from casbin_sqlalchemy_adapter import Adapter
    from sqlalchemy import create_engine

    from app.config import settings

    if not _MODEL_PATH.exists():
        raise RuntimeError(
            f"Casbin model file not found at {_MODEL_PATH} — "
            f"請確認 app/auth/casbin_model.conf 有跟著 image 一起打包"
        )

    # 給 Casbin 專用的小 pool。pool_pre_ping 避免長連線被 PG 砍掉而不自知。
    sync_engine = create_engine(
        settings.SYNC_DATABASE_URL,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=3600,
    )
    adapter = Adapter(sync_engine)
    enf = casbin.Enforcer(str(_MODEL_PATH), adapter)
    # auto-save policy(預設 True),mutate API 直接寫進 DB。
    enf.enable_auto_save(True)
    # auto-build role links 一樣是預設 True,在 add_policy 後自動補 g。
    enf.enable_auto_build_role_links(True)
    _enforcer = enf
    logger.info(
        "Casbin enforcer initialised (model=%s, policies=%d, grants=%d)",
        _MODEL_PATH.name,
        len(enf.get_policy()),
        len(enf.get_grouping_policy()),
    )


def shutdown_enforcer() -> None:
    """lifespan 結束時呼叫,釋放 adapter engine。
    pycasbin 沒有官方 close API;adapter 內部 SQLAlchemy engine 在這裡 dispose。"""
    global _enforcer
    if _enforcer is None:
        return
    try:
        adapter = getattr(_enforcer, "adapter", None)
        engine = getattr(adapter, "_engine", None) or getattr(adapter, "engine", None)
        if engine is not None:
            engine.dispose()
    except Exception as e:
        logger.warning("Casbin adapter dispose failed: %s", e)
    _enforcer = None


# ── Enforce helpers ────────────────────────────────────────────────────


def enforce(sub: str, dom: str, obj: str, act: str) -> bool:
    """進程內 enforce。enforcer 未啟用時短路回 False(讓 shadow-mode 自己看
    要怎麼處理 — 強制走 require_casbin 的 caller 會直接 403,shadow caller
    則只 log divergence)。"""
    enf = _enforcer
    if enf is None:
        return False
    return bool(enf.enforce(sub, dom, obj, act))


def reload_policy() -> None:
    """從 DB 重新拉 policy。Phase 2.4 的 sync 層在每次 rebuild 後呼叫,
    Phase 6 的 periodic reconcile job 也用這支。"""
    enf = _enforcer
    if enf is None:
        return
    enf.load_policy()


# ── Domain string helpers — 跟 casbin_sync 共用 ────────────────────────
# 把「組 dom 字串」這個小邏輯放在一個地方,policy writer (sync layer) +
# require_casbin (Phase 2.2) 都從這裡 import,避免兩邊各自拼出不一致的格式。

def org_domain(org_id: Optional[str]) -> str:
    """組成 org-level 的 Casbin domain 字串。``None`` → ``"global"``。"""
    return f"org:{org_id}" if org_id else "global"


def project_domain(project_id: str) -> str:
    return f"project:{project_id}"
