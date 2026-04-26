"""AI 測試案例生成服務。

策略：
- 從 DB 取出對應 provider 的「預設 / 啟用」AiTokenConfig
- 把 Requirement 內容組成 prompt → 呼叫 provider HTTP API
- 解析回應為標準化 JSON：[{title, ac, steps_md}]
- 統一所有 provider 使用 OpenAI-compatible JSON 形態（OpenAI / Local 直接吃；
  Anthropic / Google 走自家格式但 service 內封裝）

回傳格式（前端套用用）：
{
  "provider": "OpenAI",
  "model": "gpt-4o-mini",
  "generated": [
    {
      "title": "登入成功 - 正確帳密",
      "ac": "Given ... When ... Then ...",
      "steps_md": "## 測試步驟\\n1. ...\\n2. ..."
    },
    ...
  ]
}
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Optional

import httpx
from sqlalchemy import asc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_token_config import AiProvider, AiTokenConfig
from app.models.requirement import Requirement

log = logging.getLogger(__name__)

# 系統 prompt：要求 AI 一律輸出 JSON 陣列、結構固定
_SYSTEM_PROMPT = """你是一位資深的 QA 測試工程師，擅長 ATDD / BDD（Given-When-Then）的測試案例設計。

任務：根據使用者提供的「需求」，生成 N 個簡短但完整的測試案例骨架。

**嚴格要求：你的回應必須是「合法的 JSON 陣列」，不能包含任何解釋文字 / Markdown 圍欄 / 註解。**

每個元素的格式：
{
  "title": "測試案例標題（一行，不要超過 40 字）",
  "ac": "驗收條件（Given ... When ... Then ... 風格，可多行）",
  "steps_md": "Markdown 格式的測試步驟（含步驟編號、預期結果）"
}

範例：
[
  {
    "title": "登入成功 - 正確帳密",
    "ac": "Given 使用者已註冊\\nWhen 輸入正確帳號密碼\\nThen 進入首頁",
    "steps_md": "## 測試步驟\\n1. 開啟登入頁\\n2. 輸入 admin / admin123\\n3. 點選「登入」\\n\\n## 預期結果\\n- 跳轉到首頁\\n- 上方顯示「歡迎，admin」"
  }
]

請涵蓋：正向情境 1-2 個 + 邊界 / 失敗情境 1-2 個。語言與需求一致（需求用中文 → 回中文）。"""


def _user_prompt(requirement: Requirement, n: int) -> str:
    parts = [f"請依下列需求產出 {n} 個測試案例骨架。"]
    parts.append(f"\n# 需求 {requirement.code}：{requirement.title}")
    if requirement.description:
        parts.append(f"\n## 描述\n{requirement.description}")
    parts.append("\n再次提醒：只輸出合法的 JSON 陣列，沒有圍欄、沒有解釋。")
    return "\n".join(parts)


# ── Provider 適配器 ───────────────────────────────────────────────────

async def _call_openai_compat(
    *, base_url: str, api_key: Optional[str], model: str, system: str, user: str
) -> str:
    """OpenAI-compatible Chat Completions API（也適用本地 Ollama / vLLM / LMStudio）。"""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": 2048,
    }
    url = base_url.rstrip("/") + "/chat/completions"
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
    return data["choices"][0]["message"]["content"]


async def _call_anthropic(
    *, base_url: str, api_key: str, model: str, system: str, user: str
) -> str:
    """Anthropic Claude Messages API。"""
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    payload = {
        "model": model,
        "max_tokens": 2048,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "temperature": 0.2,
    }
    url = base_url.rstrip("/") + "/v1/messages"
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
    # Anthropic 回 content: [{type:"text", text:"..."}]
    parts = data.get("content", [])
    return "".join(p.get("text", "") for p in parts if p.get("type") == "text")


async def _call_google(
    *, base_url: str, api_key: str, model: str, system: str, user: str
) -> str:
    """Google Gemini generateContent API。"""
    headers = {"Content-Type": "application/json"}
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048},
    }
    url = (
        base_url.rstrip("/")
        + f"/models/{model}:generateContent?key={api_key}"
    )
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
    candidates = data.get("candidates") or []
    if not candidates:
        return ""
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts)


# ── Token 選擇 ──────────────────────────────────────────────────────

async def pick_token(db: AsyncSession, preferred_provider: Optional[str] = None) -> AiTokenConfig:
    """選一個可用 token：
    1) 若指定 provider，優先用該 provider 內 default 且 enabled 的；其次任何 enabled
    2) 若沒指定 provider，挑全系統第一個 default + enabled，再退而求其次
    """
    stmt = (
        select(AiTokenConfig)
        .where(AiTokenConfig.enabled.is_(True))
        .order_by(AiTokenConfig.is_default.desc(), asc(AiTokenConfig.created_at))
    )
    if preferred_provider:
        try:
            prov_enum = AiProvider(preferred_provider)
            stmt = stmt.where(AiTokenConfig.provider == prov_enum)
        except ValueError:
            pass
    rows = (await db.execute(stmt)).scalars().all()
    if not rows:
        raise RuntimeError("沒有可用的 AI Token；請先到「設定 → AI Token」新增並啟用一個")
    return rows[0]


# ── 內容解析 ────────────────────────────────────────────────────────

def _extract_json_array(text: str) -> list[dict[str, Any]]:
    """模型有時會在 JSON 前後加 ```json 圍欄；做寬鬆萃取。"""
    if not text:
        return []
    # 拿掉 markdown fence
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE | re.MULTILINE)
    # 找出第一個 [...] 區塊
    m = re.search(r"\[[\s\S]*\]", cleaned)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        out.append({
            "title": str(item.get("title") or "").strip()[:300],
            "ac": str(item.get("ac") or "").strip(),
            "steps_md": str(item.get("steps_md") or "").strip(),
        })
    return out


# ── Public API ──────────────────────────────────────────────────────

async def generate_testcases_from_requirement(
    db: AsyncSession,
    requirement: Requirement,
    *,
    n: int = 3,
    provider: Optional[str] = None,
) -> dict[str, Any]:
    n = max(1, min(int(n or 3), 10))
    token = await pick_token(db, preferred_provider=provider)
    system = _SYSTEM_PROMPT
    user = _user_prompt(requirement, n)
    base_url = token.base_url or _default_base_url(token.provider)
    model = token.model or _default_model(token.provider)
    log.info(
        "ai_test_gen: provider=%s model=%s req=%s n=%s",
        token.provider, model, requirement.code, n,
    )

    if token.provider == AiProvider.OPENAI or token.provider == AiProvider.LOCAL:
        text = await _call_openai_compat(
            base_url=base_url, api_key=token.api_key, model=model, system=system, user=user,
        )
    elif token.provider == AiProvider.ANTHROPIC:
        if not token.api_key:
            raise RuntimeError("Anthropic 需要 api_key")
        text = await _call_anthropic(
            base_url=base_url, api_key=token.api_key, model=model, system=system, user=user,
        )
    elif token.provider == AiProvider.GOOGLE:
        if not token.api_key:
            raise RuntimeError("Google Gemini 需要 api_key")
        text = await _call_google(
            base_url=base_url, api_key=token.api_key, model=model, system=system, user=user,
        )
    else:
        raise RuntimeError(f"未知 provider: {token.provider}")

    items = _extract_json_array(text)
    if not items:
        # 回傳原始輸出方便除錯
        return {
            "provider": token.provider.value if hasattr(token.provider, "value") else str(token.provider),
            "model": model,
            "generated": [],
            "raw": text[:2000],
            "error": "AI 回應無法解析為 JSON 陣列；請嘗試其他 provider 或調整 prompt",
        }
    return {
        "provider": token.provider.value if hasattr(token.provider, "value") else str(token.provider),
        "model": model,
        "generated": items,
    }


def _default_base_url(provider: AiProvider) -> str:
    return {
        AiProvider.OPENAI: "https://api.openai.com/v1",
        AiProvider.ANTHROPIC: "https://api.anthropic.com",
        AiProvider.GOOGLE: "https://generativelanguage.googleapis.com/v1beta",
        AiProvider.LOCAL: "http://localhost:11434/v1",
    }.get(provider, "")


def _default_model(provider: AiProvider) -> str:
    return {
        AiProvider.OPENAI: "gpt-4o-mini",
        AiProvider.ANTHROPIC: "claude-3-5-sonnet-20241022",
        AiProvider.GOOGLE: "gemini-1.5-pro",
        AiProvider.LOCAL: "llama3.1",
    }.get(provider, "")
