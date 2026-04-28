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

# Sprint 1.3 — 系統 prompt 改成「直接輸出 steps_json」(GeneratedStep 結構)
# 同時保留 steps_md 給人類閱讀;但前端優先用 steps_json 一鍵套用。
# action 取值參考 robot_runner._translate_step:Goto / Click / Fill / Press /
# AssertText / AssertVisible / AssertChecked / Upload / Http.Get / Http.Post / ...
_SYSTEM_PROMPT = """你是一位資深的 QA 測試工程師,擅長 ATDD / BDD(Given-When-Then)的測試案例設計。

任務:根據使用者提供的「需求」,生成 N 個簡短但完整的測試案例。

**嚴格要求:你的回應必須是「合法的 JSON 陣列」,不能包含任何解釋文字 / Markdown 圍欄 / 註解。**

每個元素的格式:
{
  "title": "測試案例標題(一行,不超過 40 字)",
  "ac": "驗收條件(Given ... When ... Then ... 風格,可多行)",
  "steps_md": "Markdown 格式的測試步驟(供閱讀)",
  "steps_json": [
    {
      "keyword": "Given|When|Then|And",
      "description": "步驟描述",
      "action": "Goto | Click | Fill | Press | AssertText | AssertVisible | AssertChecked | Upload | Http.Get | Http.Post | ...",
      "locator": "CSS / role= / text= / xpath / URL(Http.* 用)",
      "input": "輸入值或 body(可空)",
      "condition": "Equals | Contains | StartsWith | EndsWith | Regex | GreaterThan | LessThan | IsVisible | IsHidden | IsChecked",
      "expected": "預期結果(支援 ${var} 變數 或 {{= 表達式 }} 動態運算式)"
    }
  ]
}

action 速查:
- Goto:locator=URL,action="Goto"
- Click:locator=CSS / role=button[name="X"]
- Fill:locator=CSS,input=要填的字
- AssertText:locator=元素 css,condition=Equals|Contains,expected=文字
- AssertVisible:locator=元素 css,condition=IsVisible,expected=true
- Http.Get / Post / Put / Delete:locator=URL,input=body(POST 用),expected=狀態碼

範例:
[
  {
    "title": "登入成功 - 正確帳密",
    "ac": "Given 使用者已註冊\\nWhen 輸入正確帳號密碼\\nThen 進入首頁",
    "steps_md": "## 步驟\\n1. 開啟登入頁\\n2. 輸入 admin/admin123\\n3. 點選登入\\n## 預期\\n- 跳轉到首頁",
    "steps_json": [
      {"keyword": "Given", "description": "開啟登入頁", "action": "Goto", "locator": "https://example.com/login", "input": "", "condition": "Equals", "expected": ""},
      {"keyword": "When", "description": "輸入帳號", "action": "Fill", "locator": "#username", "input": "admin", "condition": "Equals", "expected": ""},
      {"keyword": "When", "description": "輸入密碼", "action": "Fill", "locator": "#password", "input": "admin123", "condition": "Equals", "expected": ""},
      {"keyword": "When", "description": "點選登入", "action": "Click", "locator": "button[type=submit]", "input": "", "condition": "Equals", "expected": ""},
      {"keyword": "Then", "description": "首頁標題顯示歡迎", "action": "AssertText", "locator": "h1.title", "input": "", "condition": "Contains", "expected": "歡迎"}
    ]
  }
]

請涵蓋:正向情境 1-2 個 + 邊界 / 失敗情境 1-2 個。語言與需求一致(需求用中文 → 回中文)。
若需求過於抽象無法給出明確 locator,steps_json 仍要列出但 locator 用合理猜測(例:`#login-btn`)。"""


def _user_prompt(requirement: Requirement, n: int) -> str:
    parts = [f"請依下列需求產出 {n} 個測試案例骨架。"]
    parts.append(f"\n# 需求 {requirement.code}：{requirement.title}")
    if requirement.description:
        parts.append(f"\n## 描述\n{requirement.description}")
    parts.append("\n再次提醒：只輸出合法的 JSON 陣列，沒有圍欄、沒有解釋。")
    return "\n".join(parts)


def _user_prompt_from_text(text: str, n: int) -> str:
    """Sprint 2.3 — 從純文字(AI Chat 對話內容 / 任意需求描述)生案例。"""
    parts = [f"請依下列描述產出 {n} 個測試案例骨架。"]
    parts.append(f"\n# 需求描述\n{text.strip()}")
    parts.append("\n再次提醒:只輸出合法的 JSON 陣列,沒有圍欄、沒有解釋。")
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

async def pick_token(
    db: AsyncSession,
    preferred_provider: Optional[str] = None,
    organization_id: Optional[str] = None,
) -> AiTokenConfig:
    """選一個可用 token（按 org 過濾）：
    1) 若指定 provider，優先用該 provider 內 default 且 enabled 的；其次任何 enabled
    2) 若沒指定 provider，挑全系統第一個 default + enabled，再退而求其次
    """
    stmt = (
        select(AiTokenConfig)
        .where(AiTokenConfig.enabled.is_(True))
        .order_by(AiTokenConfig.is_default.desc(), asc(AiTokenConfig.created_at))
    )
    if organization_id is not None:
        stmt = stmt.where(AiTokenConfig.organization_id == organization_id)
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
        # steps_json 為 GeneratedStep 結構陣列;允許空(LLM 解析失敗時前端 fallback steps_md)
        raw_steps = item.get("steps_json")
        steps_json: list[dict] = []
        if isinstance(raw_steps, list):
            for s in raw_steps:
                if not isinstance(s, dict):
                    continue
                steps_json.append({
                    "keyword": str(s.get("keyword") or "When").strip()[:10],
                    "description": str(s.get("description") or "").strip()[:300],
                    "action": str(s.get("action") or "").strip()[:40],
                    "locator": str(s.get("locator") or "").strip()[:500],
                    "input": str(s.get("input") or "").strip()[:2000],
                    "condition": str(s.get("condition") or "Equals").strip()[:40],
                    "expected": str(s.get("expected") or "").strip()[:500],
                })
        out.append({
            "title": str(item.get("title") or "").strip()[:300],
            "ac": str(item.get("ac") or "").strip(),
            "steps_md": str(item.get("steps_md") or "").strip(),
            "steps_json": steps_json,
        })
    return out


# ── Public API ──────────────────────────────────────────────────────

async def generate_testcases_from_requirement(
    db: AsyncSession,
    requirement: Requirement,
    *,
    n: int = 3,
    provider: Optional[str] = None,
    organization_id: Optional[str] = None,
) -> dict[str, Any]:
    n = max(1, min(int(n or 3), 10))
    token = await pick_token(db, preferred_provider=provider, organization_id=organization_id)
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


async def generate_testcases_from_text(
    db: AsyncSession,
    text: str,
    *,
    n: int = 3,
    provider: Optional[str] = None,
    organization_id: Optional[str] = None,
) -> dict[str, Any]:
    """Sprint 2.3 — 從純文字(AI Chat 對話 / 任意描述)生 N 個測試案例。

    跟 generate_testcases_from_requirement 同邏輯,只是輸入是 text 而非 Requirement。
    """
    n = max(1, min(int(n or 3), 10))
    text = (text or "").strip()
    if not text:
        raise RuntimeError("text 不能為空")
    token = await pick_token(db, preferred_provider=provider, organization_id=organization_id)
    system = _SYSTEM_PROMPT
    user = _user_prompt_from_text(text, n)
    base_url = token.base_url or _default_base_url(token.provider)
    model = token.model or _default_model(token.provider)
    log.info("ai_test_gen(text): provider=%s model=%s n=%s len=%s",
             token.provider, model, n, len(text))

    if token.provider == AiProvider.OPENAI or token.provider == AiProvider.LOCAL:
        out = await _call_openai_compat(
            base_url=base_url, api_key=token.api_key, model=model, system=system, user=user,
        )
    elif token.provider == AiProvider.ANTHROPIC:
        if not token.api_key:
            raise RuntimeError("Anthropic 需要 api_key")
        out = await _call_anthropic(
            base_url=base_url, api_key=token.api_key, model=model, system=system, user=user,
        )
    elif token.provider == AiProvider.GOOGLE:
        if not token.api_key:
            raise RuntimeError("Google Gemini 需要 api_key")
        out = await _call_google(
            base_url=base_url, api_key=token.api_key, model=model, system=system, user=user,
        )
    else:
        raise RuntimeError(f"未知 provider: {token.provider}")

    items = _extract_json_array(out)
    if not items:
        return {
            "provider": token.provider.value if hasattr(token.provider, "value") else str(token.provider),
            "model": model,
            "generated": [],
            "raw": out[:2000],
            "error": "AI 回應無法解析為 JSON 陣列;請嘗試其他 provider 或調整 prompt",
        }
    return {
        "provider": token.provider.value if hasattr(token.provider, "value") else str(token.provider),
        "model": model,
        "generated": items,
    }


# Sprint 3.1 — 增強 prompt:看現有 step 列表,補多條件斷言 / capture / 變數
_ENHANCE_SYSTEM_PROMPT = """你是一位資深 QA 工程師,擅長把粗糙錄製成的測試腳本「增強」成更穩健的測試。

任務:檢視使用者提供的「Playwright codegen 原始腳本」+「已解析的 step 陣列」,輸出增強版 step 陣列。

**增強策略:**
1. **加 capture step**:遇到登入/認證 token / 動態 ID / 訂單編號 → 加 Capture step 抓進 ${var}
2. **加多條件斷言**:除了 codegen 預設的 Equals / Contains 外,適當加 IsVisible / IsChecked / Regex 比對
3. **動態運算式**:若預期值需要計算(如 `${count} + 1`),用 `{{= ${var} + 1 }}` 寫
4. **改善 expected**:codegen 的斷言常常太絕對(例如 to_have_text "Welcome admin"),改成 Contains "Welcome" 更穩
5. **保留**所有原始 step 的 action / locator(不要亂改),只能在後面**新增** Capture / Assert step

**嚴格要求:回應必須是合法的 JSON 陣列(每個元素是 step),不能有 Markdown 圍欄、註解、解釋文字。**

每個 step 格式同 GeneratedStep:
{"keyword": "Given|When|Then|And", "description": "...", "action": "...", "locator": "...", "input": "...", "condition": "Equals|Contains|...", "expected": "..."}

允許的 action(部分):Goto / Click / Fill / Press / AssertText / AssertVisible / AssertChecked / Capture / Http.Get / Http.Post / ...
條件:Equals / Contains / StartsWith / EndsWith / Regex / IsVisible / IsChecked / GreaterThan / LessThan
"""


def _enhance_user_prompt(script_text: str, current_steps: list[dict]) -> str:
    parts = ["請把下面的測試案例增強(加 Capture / 多條件斷言 / 動態運算式)。"]
    parts.append("\n# 原始 Playwright codegen 腳本(供參考意圖)")
    parts.append("```python")
    parts.append((script_text or "")[:8000])
    parts.append("```")
    parts.append("\n# 目前已解析的 step 陣列(增強的基礎)")
    parts.append("```json")
    parts.append(json.dumps(current_steps[:80], ensure_ascii=False, indent=2))
    parts.append("```")
    parts.append("\n再次提醒:只輸出合法 JSON 陣列(增強後的 step list),沒有圍欄沒有解釋。")
    return "\n".join(parts)


async def enhance_steps_with_ai(
    db: AsyncSession,
    *,
    script_text: str,
    current_steps: list[dict],
    provider: Optional[str] = None,
    organization_id: Optional[str] = None,
) -> dict[str, Any]:
    """Sprint 3.1 — AI 增強:看 codegen 腳本 + 已解析 step → 推斷意圖加 capture / 多條件 / 動態運算式。

    回傳 {provider, model, enhanced_steps: list[dict], original_count, enhanced_count, error?}
    """
    if not (script_text or "").strip() and not current_steps:
        raise RuntimeError("沒有腳本或步驟可增強")
    token = await pick_token(db, preferred_provider=provider, organization_id=organization_id)
    system = _ENHANCE_SYSTEM_PROMPT
    user = _enhance_user_prompt(script_text or "", current_steps or [])
    base_url = token.base_url or _default_base_url(token.provider)
    model = token.model or _default_model(token.provider)
    log.info("ai_enhance: provider=%s model=%s steps=%s", token.provider, model, len(current_steps or []))

    if token.provider == AiProvider.OPENAI or token.provider == AiProvider.LOCAL:
        out = await _call_openai_compat(
            base_url=base_url, api_key=token.api_key, model=model, system=system, user=user,
        )
    elif token.provider == AiProvider.ANTHROPIC:
        if not token.api_key:
            raise RuntimeError("Anthropic 需要 api_key")
        out = await _call_anthropic(
            base_url=base_url, api_key=token.api_key, model=model, system=system, user=user,
        )
    elif token.provider == AiProvider.GOOGLE:
        if not token.api_key:
            raise RuntimeError("Google Gemini 需要 api_key")
        out = await _call_google(
            base_url=base_url, api_key=token.api_key, model=model, system=system, user=user,
        )
    else:
        raise RuntimeError(f"未知 provider: {token.provider}")

    # 解析回傳:期待是 step 陣列(不是 case 陣列)
    try:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", out.strip(),
                         flags=re.IGNORECASE | re.MULTILINE)
        m = re.search(r"\[[\s\S]*\]", cleaned)
        data = json.loads(m.group(0)) if m else []
    except (ValueError, json.JSONDecodeError, AttributeError):
        data = []

    enhanced: list[dict] = []
    if isinstance(data, list):
        for s in data:
            if not isinstance(s, dict):
                continue
            enhanced.append({
                "keyword": str(s.get("keyword") or "When").strip()[:10],
                "description": str(s.get("description") or "").strip()[:300],
                "action": str(s.get("action") or "").strip()[:40],
                "locator": str(s.get("locator") or "").strip()[:500],
                "input": str(s.get("input") or "").strip()[:2000],
                "condition": str(s.get("condition") or "Equals").strip()[:40],
                "expected": str(s.get("expected") or "").strip()[:500],
            })

    result: dict[str, Any] = {
        "provider": token.provider.value if hasattr(token.provider, "value") else str(token.provider),
        "model": model,
        "original_count": len(current_steps or []),
        "enhanced_count": len(enhanced),
        "enhanced_steps": enhanced,
    }
    if not enhanced:
        result["error"] = "AI 回應無法解析為 step 陣列;原始 raw 已截短"
        result["raw"] = out[:2000]
    return result


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
