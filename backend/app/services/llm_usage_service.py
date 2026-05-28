"""Agent token usage 寫入服務 — 成本可見度紅線的後端落地。

兩種使用方式:
1. **手動**:呼叫端拿到 ``ChatResult`` 之後自己呼叫 ``record_usage()`` 寫一筆
2. **包覆**:呼叫 ``chat_with_usage_log()``,內部跑 chat() 再寫 usage,
   失敗時不寫(token 沒消耗到)。Phase 1 的 agent executor 一律走這支

寫入失敗的策略:**吞掉並 log error**。LLM chat 已經跑完、token 已經被算錢,
寫 usage 表失敗就只是「我們看不到這筆」,不該讓使用者整個對話 crash。
Audit 等級用 ERROR,讓 ops 能監控。

不在這層做的事:
* Casbin / 權限檢查 — 由呼叫端的 router 把關
* Budget cap 強制 — 這層只記錄;Phase 1+ 才加「超過 $N 拒絕 chat」邏輯
* prompt / response 全文 — 那是 agent_messages 的事,這裡只記 usage
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from app.llm.base import ChatResult, LLMProvider, Message, ToolSpec
from app.models.agent_token_usage import AgentTokenUsage

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)


async def record_usage(
    db: "AsyncSession",
    *,
    organization_id: Optional[str],
    user_id: Optional[str],
    session_id: Optional[str],
    result: ChatResult,
) -> Optional[AgentTokenUsage]:
    """寫一筆 token usage 明細。失敗時吞例外、回 None,不讓上層 chat 流程 crash。

    呼叫端負責提交事務(``await db.commit()``);本函式只做 flush 進 session,
    與 EmailConfig / 其他 service 寫法一致。
    """
    try:
        row = AgentTokenUsage(
            id=str(uuid.uuid4()),
            organization_id=organization_id,
            user_id=user_id,
            session_id=session_id,
            provider=result.provider,
            model=result.model,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            cache_read_tokens=result.usage.cache_read_tokens,
            cache_write_tokens=result.usage.cache_write_tokens,
            # ChatResult.usage.cost_usd 是 float;轉 Decimal 用字串避免浮點誤差
            cost_usd=Decimal(f"{result.usage.cost_usd:.6f}"),
            stop_reason=result.stop_reason,
            response_id=result.raw_response_id,
        )
        db.add(row)
        await db.flush()
        return row
    except Exception:  # noqa: BLE001
        # 不 raise — 寫不進去也不要讓 chat 結果丟掉
        log.exception(
            "record_usage failed (provider=%s model=%s tokens=%d/%d cost=$%.6f)",
            result.provider,
            result.model,
            result.usage.input_tokens,
            result.usage.output_tokens,
            result.usage.cost_usd,
        )
        return None


async def chat_with_usage_log(
    db: "AsyncSession",
    llm: LLMProvider,
    *,
    organization_id: Optional[str],
    user_id: Optional[str],
    session_id: Optional[str],
    messages: list[Message],
    model: str,
    system: Optional[str] = None,
    tools: Optional[list[ToolSpec]] = None,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    timeout: float = 60.0,
    cache_system_and_tools: bool = True,
    thinking_level: Optional[str] = None,
) -> ChatResult:
    """跑 chat() 並寫 usage 記錄。任何 LLMError 直接 propagate(由上層處理重試 / 顯示)。

    這是 Phase 1 agent executor 預定使用的入口。chat 跑成功才寫 usage —
    失敗(LLMError)時 provider 端沒扣 token,跳過寫入合理。
    """
    result = await llm.chat(
        messages,
        model=model,
        system=system,
        tools=tools,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
        cache_system_and_tools=cache_system_and_tools,
        thinking_level=thinking_level,
    )
    await record_usage(
        db,
        organization_id=organization_id,
        user_id=user_id,
        session_id=session_id,
        result=result,
    )
    return result
