"""AI 對話 REST endpoints — 右下角浮動圓點 → 開啟對話框。

每位使用者擁有自己的 conversations(用 owner=username 隔離);
LLM provider 設定沿用既有 ai_token_configs(沒設定 = 用 is_default 那組)。

API:
- GET    /api/ai/conversations              列出我的對話
- POST   /api/ai/conversations              建立新對話
- GET    /api/ai/conversations/{id}         取單筆對話 + 全部訊息
- PUT    /api/ai/conversations/{id}         改 title
- DELETE /api/ai/conversations/{id}         刪掉
- POST   /api/ai/conversations/{id}/messages  送 user 訊息 → 回 assistant 訊息
"""
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.ai_conversation import AiConversation, AiMessage
from app.models.ai_token_config import AiProvider, AiTokenConfig
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.ai_chat import (
    AiConversationCreate,
    AiConversationDetail,
    AiConversationResponse,
    AiConversationUpdate,
    AiMessageResponse,
    SendMessageRequest,
    SendMessageResponse,
)

router = APIRouter()


_SYSTEM_PROMPT_DEFAULT = (
    "你是 RL 自動化測試平台的內建 AI 助理。請用繁體中文回答,"
    "回應要簡潔有用。當使用者問測試案例設計、Robot Framework 語法、"
    "BDD/AC 撰寫、缺陷分析、API/SQL 自動化時,請直接給可執行的範例。"
)


# ───────────────────────── 輔助:挑 provider ──────────────────────────

async def _resolve_provider(
    db: AsyncSession, user: User, provider_config_id: Optional[str]
) -> Optional[AiTokenConfig]:
    """挑出要用的 AI provider 設定。優先順序:
    1) 指定的 provider_config_id(若使用者有權限)
    2) is_default=True 的設定(限該 organization)
    3) 任何 enabled=True 的設定
    """
    stmt = select(AiTokenConfig).where(AiTokenConfig.enabled.is_(True))
    if user.organization_id:
        stmt = stmt.where(AiTokenConfig.organization_id == user.organization_id)
    if provider_config_id:
        c = (await db.execute(stmt.where(AiTokenConfig.id == provider_config_id))).scalar_one_or_none()
        if c:
            return c
    # 預設
    c = (await db.execute(stmt.where(AiTokenConfig.is_default.is_(True)))).scalar_one_or_none()
    if c:
        return c
    # 隨便一個
    return (await db.execute(stmt.order_by(AiTokenConfig.created_at))).scalars().first()


# ───────────────────────── 輔助:呼叫 LLM ──────────────────────────────

async def _call_chat_completion(
    cfg: AiTokenConfig, history: list[dict], system_prompt: str
) -> tuple[str, Optional[int]]:
    """呼叫 chat completion。Provider 由 ai_provider_map.resolve() 自動推算 base_url 與 API 風格。
    回 (content, tokens_used)。
    """
    from app.services.ai_provider_map import resolve

    spec = resolve(cfg.provider, base_url_override=cfg.base_url)
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)

    if spec.style == "anthropic":
        non_system = [m for m in messages if m["role"] != "system"]
        headers = {"Content-Type": "application/json"}
        if cfg.api_key:
            headers[spec.auth_header] = (spec.auth_prefix or "") + cfg.api_key
        if spec.extra_headers:
            headers.update(spec.extra_headers)
        payload = {
            "model": cfg.model or "claude-3-5-sonnet-latest",
            "max_tokens": 2048,
            "system": system_prompt,
            "messages": non_system,
            "temperature": 0.5,
        }
        url = spec.base_url.rstrip("/") + "/messages"
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
        parts = data.get("content", [])
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        usage = data.get("usage") or {}
        tokens = (usage.get("input_tokens", 0) or 0) + (usage.get("output_tokens", 0) or 0)
        return text, tokens or None

    # OpenAI-compatible(也吃 Ollama / DeepSeek / Groq / OpenRouter / 自架...)
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers[spec.auth_header] = (spec.auth_prefix or "") + cfg.api_key
    if spec.extra_headers:
        headers.update(spec.extra_headers)
    payload = {
        "model": cfg.model or "gpt-4o-mini",
        "messages": messages,
        "temperature": 0.5,
        "max_tokens": 2048,
    }
    # 思考程度(o1/o3 系列才支援;其他模型 provider 端會忽略或回 400,我們忽略後者)
    if cfg.reasoning_effort and cfg.reasoning_effort.lower() in ("low", "medium", "high"):
        payload["reasoning_effort"] = cfg.reasoning_effort.lower()
    url = spec.base_url.rstrip("/") + "/chat/completions"
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage") or {}
    tokens = usage.get("total_tokens")
    return text, tokens


# ───────────────────────── 輔助:序列化 ──────────────────────────────

async def _enrich_conversation(db: AsyncSession, conv: AiConversation) -> dict:
    last_msg = (
        await db.execute(
            select(AiMessage)
            .where(AiMessage.conversation_id == conv.id)
            .order_by(desc(AiMessage.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    cnt_rows = (
        await db.execute(
            select(AiMessage.id).where(AiMessage.conversation_id == conv.id)
        )
    ).all()
    return {
        "id": conv.id,
        "owner": conv.owner,
        "organization_id": conv.organization_id,
        "title": conv.title,
        "provider_config_id": conv.provider_config_id,
        "created_at": conv.created_at,
        "updated_at": conv.updated_at,
        "message_count": len(cnt_rows),
        "last_message_preview": (last_msg.content[:80] if last_msg else None),
    }


# ────────────────────────────── routes ──────────────────────────────

@router.get(
    "/ai/conversations",
    response_model=list[AiConversationResponse],
    tags=["V · AI"],
)
async def list_conversations(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(AiConversation)
            .where(AiConversation.owner == user.username)
            .order_by(desc(AiConversation.updated_at))
            .limit(50)
        )
    ).scalars().all()
    return [await _enrich_conversation(db, c) for c in rows]


@router.post(
    "/ai/conversations",
    response_model=AiConversationResponse,
    status_code=201,
    tags=["V · AI"],
)
async def create_conversation(
    payload: AiConversationCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = AiConversation(
        owner=user.username,
        organization_id=user.organization_id,
        title=(payload.title or "新對話")[:200],
        provider_config_id=payload.provider_config_id,
    )
    db.add(conv)
    await db.flush()
    await db.refresh(conv)
    return await _enrich_conversation(db, conv)


@router.get(
    "/ai/conversations/{conv_id}",
    response_model=AiConversationDetail,
    tags=["V · AI"],
)
async def get_conversation(
    conv_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = await db.get(AiConversation, conv_id)
    if not conv or conv.owner != user.username:
        raise HTTPException(404, "Conversation not found")
    msgs = (
        await db.execute(
            select(AiMessage)
            .where(AiMessage.conversation_id == conv_id)
            .order_by(AiMessage.created_at)
        )
    ).scalars().all()
    base = await _enrich_conversation(db, conv)
    base["messages"] = [AiMessageResponse.model_validate(m).model_dump() for m in msgs]
    return base


@router.put(
    "/ai/conversations/{conv_id}",
    response_model=AiConversationResponse,
    tags=["V · AI"],
)
async def update_conversation(
    conv_id: str,
    payload: AiConversationUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = await db.get(AiConversation, conv_id)
    if not conv or conv.owner != user.username:
        raise HTTPException(404, "Conversation not found")
    if payload.title is not None:
        conv.title = payload.title[:200]
    if payload.provider_config_id is not None:
        conv.provider_config_id = payload.provider_config_id
    await db.flush()
    await db.refresh(conv)
    return await _enrich_conversation(db, conv)


@router.delete(
    "/ai/conversations/{conv_id}",
    status_code=204,
    tags=["V · AI"],
)
async def delete_conversation(
    conv_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = await db.get(AiConversation, conv_id)
    if not conv or conv.owner != user.username:
        raise HTTPException(404, "Conversation not found")
    await db.delete(conv)
    await db.flush()


@router.post(
    "/ai/conversations/{conv_id}/messages",
    response_model=SendMessageResponse,
    tags=["V · AI"],
)
@limiter.limit("60/hour")
async def send_message(
    request: Request,
    conv_id: str,
    payload: SendMessageRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = await db.get(AiConversation, conv_id)
    if not conv or conv.owner != user.username:
        raise HTTPException(404, "Conversation not found")
    if not (payload.content or "").strip():
        raise HTTPException(400, "content 不能為空")

    # 1) 寫入 user message
    user_msg = AiMessage(
        conversation_id=conv_id,
        role="user",
        content=payload.content.strip(),
    )
    db.add(user_msg)
    await db.flush()

    # 2) 如果 conversation 還是預設標題,用首則 user 訊息開頭當 title
    if conv.title == "新對話":
        conv.title = payload.content.strip()[:60]

    # 3) 取 history(包含剛存的 user_msg)
    history_rows = (
        await db.execute(
            select(AiMessage)
            .where(AiMessage.conversation_id == conv_id)
            .order_by(AiMessage.created_at)
        )
    ).scalars().all()
    history = [{"role": m.role, "content": m.content} for m in history_rows]

    # 4) 找 provider
    cfg = await _resolve_provider(db, user, conv.provider_config_id)
    if not cfg:
        raise HTTPException(
            400,
            "尚未設定任何 AI provider;請至設定 → AI Token 加入一個並設為預設",
        )

    # 5) 呼叫 LLM
    try:
        assistant_text, tokens = await _call_chat_completion(cfg, history, _SYSTEM_PROMPT_DEFAULT)
    except httpx.HTTPStatusError as e:
        # 把 provider 端錯誤原狀帶回前端;status code 也對齊讓使用者好理解
        upstream = e.response.status_code if e.response is not None else 502
        body = (e.response.text[:400] if e.response is not None else "") or "(empty response body)"
        provider_name = cfg.provider.value if hasattr(cfg.provider, "value") else str(cfg.provider)
        if upstream == 401 or upstream == 403:
            raise HTTPException(401, f"{provider_name} API key 驗證失敗(provider 回 {upstream})。請至「設定 → AI Token」確認 key 是否正確 / 過期。")
        if upstream == 429:
            raise HTTPException(429, f"{provider_name} 速率限制(provider 回 429)。稍後再試,或升級你的 plan。")
        if upstream == 404:
            raise HTTPException(400, f"{provider_name} 找不到模型「{cfg.model}」(provider 回 404)。請至 AI Token 確認 model 名稱正確,或按「重抓模型清單」更新。")
        raise HTTPException(502, f"{provider_name} 回 {upstream}: {body}")
    except httpx.TimeoutException:
        raise HTTPException(504, f"{cfg.provider} 連線逾時(60s)。檢查 base_url 與網路。")
    except httpx.RequestError as e:
        raise HTTPException(504, f"無法連線到 AI provider:{type(e).__name__}: {e}")
    except Exception as e:
        raise HTTPException(502, f"AI 呼叫失敗:{type(e).__name__}: {e}")

    # 6) 寫入 assistant message
    asst_msg = AiMessage(
        conversation_id=conv_id,
        role="assistant",
        content=assistant_text,
        tokens_used=tokens,
        provider=cfg.provider.value if hasattr(cfg.provider, "value") else str(cfg.provider),
        model=cfg.model,
    )
    db.add(asst_msg)
    await db.flush()
    await db.refresh(user_msg)
    await db.refresh(asst_msg)

    return SendMessageResponse(
        user_message=AiMessageResponse.model_validate(user_msg),
        assistant_message=AiMessageResponse.model_validate(asst_msg),
    )
