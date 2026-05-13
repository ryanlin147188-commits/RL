"""Celery task: 從 Casdoor 整批同步 user / role 狀態。

Phase 6.3 of the Casdoor + Casbin migration plan。webhook 是即時 fast-path,
這支 task 是兜底 — 每 5 分鐘整批 reconcile,讓「漏掉一個 webhook 而導致的
不一致」最多撐 5 分鐘就會自動修正。

排程位置:``celery_app.beat_schedule``(見 ``tasks/celery_app.py``)。要啟動
periodic 排程需要在 celery worker 進程加 ``-B`` 旗標(或另開一個 ``celery beat``
進程)。在 ``backend/celery_entrypoint.sh`` 已預設 ``--without-mingle --without-gossip``,
此 task 也跟一般 worker 任務一起被 dispatch,沒有 IO 競爭。

要把 reconcile 暫時關掉:``CASDOOR_RECONCILE_ENABLED=False``。
"""
from __future__ import annotations

import asyncio
import logging
import os

from tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    raw = os.environ.get("CASDOOR_RECONCILE_ENABLED", "False").strip().lower()
    return raw in {"true", "1", "yes", "on"}


@celery_app.task(name="tasks.casdoor_reconcile.run")
def run_casdoor_reconcile() -> dict:
    """beat-triggered Celery task。內部開一條短命 async loop 跑 reconcile_all。

    為什麼不直接 ``async def`` 當 task:Celery 4/5 對 async task 支援不完整,
    內部 wrap event_loop 比較吃力;另起一條 loop 跑單一 reconcile 更乾淨。
    """
    if not _enabled():
        return {"skipped": True, "reason": "CASDOOR_RECONCILE_ENABLED=False"}

    async def _runner():
        # 動態 import — 避免 celery autodiscover 時把 app.* 一起拉起來(會去
        # 連 DB / Valkey 才能 import_module)
        import asyncio

        from app.auth import casbin as _casbin
        from app.database import AsyncSessionLocal
        from app.services.casdoor_sync import reconcile_all

        # Celery worker 沒走 FastAPI lifespan,enforcer 不會自動 init。
        # 在每次任務開頭以 force=True 確保 worker 進程內也有可用的 enforcer
        # (init_enforcer 內部 _enforcer is not None 檢查讓重跑無副作用)。
        if _casbin.is_enabled():
            await asyncio.to_thread(_casbin.init_enforcer, True)
        else:
            logger.info("Casbin disabled — reconcile will skip rebuild_all_policies")

        async with AsyncSessionLocal() as session:
            return await reconcile_all(session)

    try:
        return asyncio.run(_runner())
    except Exception:
        logger.exception("casdoor_reconcile task failed")
        return {"error": "see worker log"}
