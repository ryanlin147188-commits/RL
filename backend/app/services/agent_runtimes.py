"""Agent runtime registry + token capability gating。

Settings 頁的 agent 下拉、`pick_token_for_user` 的篩選都讀這支。

新增 runtime:在 `AGENT_RUNTIMES` 加 entry,並改 `_check()` 對應該 runtime 的
token 條件。Phase 1 只列出 hermes / openclaw 兩個;openclaw 真正 wire 起來在
Phase 3,此時 capability check 已就位,UI 會在使用者建立 OAuth credential 後
自動 enable 該選項。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from app.models.ai_token_config import AiTokenConfig


@dataclass(frozen=True)
class AgentRuntimeSpec:
    key: str            # 'hermes' / 'openclaw'
    label: str          # UI 顯示名
    description: str    # tooltip
    required: str       # 給 UI tooltip 解釋「為什麼不能選」


AGENT_RUNTIMES: list[AgentRuntimeSpec] = [
    AgentRuntimeSpec(
        key="hermes",
        label="Hermes (ACP)",
        description="預設 agent runtime,跑在 per-user workspace,支援所有 OpenAI-compat / Anthropic / Google 等 API token。",
        required="任一啟用的 AI Token(OpenAI / Anthropic / Google / DeepSeek / Groq / 自架...)",
    ),
    AgentRuntimeSpec(
        key="openclaw",
        label="OpenClaw",
        description="本地 personal-assistant runtime,走 OpenClaw CLI(內部以 OPENAI_API_KEY 呼叫 OpenAI-compatible API)。沒 OpenAI token 時實際 chat 會 fallback 回 Hermes。",
        required="provider=`OpenAI` 的 AI Token(sk-...);也接受 `openai-oauth` 若日後接通 OAuth",
    ),
]


def _token_supports_hermes(t: AiTokenConfig) -> bool:
    # Hermes 對 token 的最低要求:啟用 + 有 api_key(本地 ollama 也行,base_url
    # 由 ai_provider_map 推算)。reasoning model 不要求。
    return bool(t.enabled and (t.api_key or (t.provider or "").lower() in {"ollama", "lmstudio"}))


def _token_supports_openclaw(t: AiTokenConfig) -> bool:
    # OpenClaw sidecar 真的只能餵 OPENAI_API_KEY(supervisor 走 openclaw agent
    # --local,讀 OPENAI_API_KEY env)。Anthropic / Google 的 key 格式不相容,
    # 所以只認 provider=OpenAI(以及 openai-oauth,若日後接通)。
    if not t.enabled or not t.api_key:
        return False
    return (t.provider or "").lower() in {"openai", "openai-oauth"}


def check_agent_capabilities(tokens: Iterable[AiTokenConfig]) -> list[dict]:
    """回傳每個 runtime 的支援狀態(給前端下拉用)。

    回傳格式:
        [{key, label, description, required, supported, reason}, ...]

    `supported=False` 時 `reason` 給 UI tooltip;前端應該把該項置灰(value 仍
    可送回,後端會 reject)。
    """
    tokens = list(tokens)
    result: list[dict] = []
    for spec in AGENT_RUNTIMES:
        if spec.key == "hermes":
            ok = any(_token_supports_hermes(t) for t in tokens)
        elif spec.key == "openclaw":
            ok = any(_token_supports_openclaw(t) for t in tokens)
        else:
            ok = False
        result.append({
            "key": spec.key,
            "label": spec.label,
            "description": spec.description,
            "required": spec.required,
            "supported": ok,
            "reason": None if ok else f"未偵測到符合條件的 Token:{spec.required}",
        })
    return result


def resolve_preferred_agent(
    user_preference: Optional[str],
    tokens: Iterable[AiTokenConfig],
) -> str:
    """把使用者偏好(可能是 NULL / 已失效)折算成實際要跑的 runtime key。

    規則:
      1) 使用者有偏好且該 runtime 仍 supported → 用偏好
      2) 否則 → 第一個 supported 的(順序見 AGENT_RUNTIMES)
      3) 都不 supported(沒任何 token)→ fallback 'hermes'(觸發時呼叫方應該
         先擋,但保底不讓 None 流出)
    """
    caps = {c["key"]: c["supported"] for c in check_agent_capabilities(tokens)}
    if user_preference and caps.get(user_preference):
        return user_preference
    for spec in AGENT_RUNTIMES:
        if caps.get(spec.key):
            return spec.key
    return AGENT_RUNTIMES[0].key
