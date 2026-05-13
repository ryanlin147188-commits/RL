"""Casdoor webhook receiver。

掛在 ``POST /api/auth/casdoor-webhook``,Casdoor 在使用者建立 / 更新 / 刪除 /
角色指派變動時打過來。本端只負責:

* 驗證 ``X-Casdoor-Webhook-Token`` 跟 ``CASDOOR_WEBHOOK_TOKEN`` 環境變數一致
  (簡單共享 secret;Casdoor admin UI 可以在 webhook 設定的 ``headers`` 欄位
  填這個 token)
* 用 Valkey 紀錄 ``recordId`` 做 idempotency(60 分鐘 dedup window),Casdoor
  retry 機制可能會在網路抖動時送重複事件
* 把事件丟到 :mod:`app.services.casdoor_sync` 走 fast-path 同步單筆

延遲容忍度高的事件(不在 Casdoor 直接重送的支援範圍內)由 Celery beat 每 5
分鐘整批 reconcile 兜底 — 漏掉一個 webhook 不會永遠不一致。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.casdoor_sync import apply_single_user_event

logger = logging.getLogger(__name__)
router = APIRouter()

_WEBHOOK_TOKEN = os.environ.get("CASDOOR_WEBHOOK_TOKEN", "").strip()
_DEDUP_TTL_SEC = 3600
_DEDUP_PREFIX = "casdoor:webhook:seen:"


async def _is_duplicate(record_id: str) -> bool:
    """Valkey SET NX 做一次性 mark。回 True 代表「先前已經處理過」。"""
    if not record_id:
        return False
    try:
        from app.auth.revocation import _get_async_redis

        client = await _get_async_redis()
        # SET NX:不存在才寫;返回 None / False 代表 key 已存在 → 重複事件
        was_set = await client.set(
            f"{_DEDUP_PREFIX}{record_id}", "1", ex=_DEDUP_TTL_SEC, nx=True,
        )
        return not bool(was_set)
    except Exception:
        # cache 出問題 → 寧可處理重複也不要漏(apply_single_user_event 自身
        # 是 upsert,重跑無副作用)
        logger.exception("casdoor webhook dedup cache miss")
        return False


def _verify_token(received: Optional[str]) -> None:
    if not _WEBHOOK_TOKEN:
        # 未設定 → 拒絕,逼 operator 顯式配置,避免「未驗證 webhook 也照吃」
        raise HTTPException(
            503,
            "CASDOOR_WEBHOOK_TOKEN 未設定;webhook 預設關閉,請在 .env 配置後重啟 backend",
        )
    if not received or received.strip() != _WEBHOOK_TOKEN:
        raise HTTPException(401, "webhook token 不符")


@router.post("/auth/casdoor-webhook", status_code=204, tags=["U · 認證"])
async def casdoor_webhook(
    request: Request,
    x_casdoor_webhook_token: Optional[str] = Header(None, convert_underscores=True),
    db: AsyncSession = Depends(get_db),
):
    """Casdoor 端在 ``Application → Webhook`` 設好以下:

    * URL: ``http://<host>/api/auth/casdoor-webhook``
    * Method: POST
    * Content type: application/json
    * Custom headers: ``X-Casdoor-Webhook-Token: <CASDOOR_WEBHOOK_TOKEN>``
    * Events: ``add-user`` / ``update-user`` / ``delete-user`` / ``update-role``

    payload 對應 Casdoor 自己定義的 ``Record`` 結構,本端只用到:
        - ``action``: 事件類型
        - ``object``: 變動後的物件(``user`` / ``role`` JSON)
        - ``id``: Casdoor 記錄編號,給 idempotency 用

    回 204:Casdoor 視為成功;任何 4xx/5xx Casdoor 會 retry 數次。
    """
    _verify_token(x_casdoor_webhook_token)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "body 不是合法 JSON")

    record_id = str(body.get("id") or "")
    action = (body.get("action") or "").strip()
    obj = body.get("object") or {}

    if await _is_duplicate(record_id):
        logger.info("casdoor webhook %s skipped(duplicate id=%s)", action, record_id)
        return None

    if action in {"add-user", "update-user", "delete-user"}:
        try:
            await apply_single_user_event(db, action, obj)
        except Exception:
            logger.exception("casdoor webhook user fast-path failed")
            raise HTTPException(500, "internal sync failure")
        return None

    if action in {"add-role", "update-role", "delete-role"}:
        # role 變動的 fast-path 比較囉嗦(members + permissions 都可能動),
        # 直接觸發整批 reconcile;Casbin 重灌一次便宜,反正 5 分鐘的 beat
        # 也是這樣做。
        try:
            from app.services.casdoor_sync import reconcile_all

            await reconcile_all(db)
        except Exception:
            logger.exception("casdoor webhook role full-reconcile failed")
            raise HTTPException(500, "internal sync failure")
        return None

    # 未知事件 — log + 204(避免 Casdoor 因為我們沒實作就一直 retry)
    logger.info("casdoor webhook: ignored action=%s id=%s", action, record_id)
    return None
