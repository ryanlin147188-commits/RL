"""LLM model 能力對照表 — 每家哪些 model 支援「extended thinking」/「reasoning」。

設計目標:
* 統一 UI 抽象 — 對 user 而言只看到「off / low / medium / high」四檔思考度,
  backend 內部把這個 level 翻成各家對應的官方 API 參數
* hardcoded prefix match — 模型清單變化頻率不到一週一次,簡單對照表足夠
* 未來新模型出來只要加一行 prefix 即可

統一 thinking level → 各家對應:

  ┌─────────┬────────────────┬───────────────────┬────────────────────┐
  │  level  │ Anthropic      │ OpenAI            │ Google             │
  ├─────────┼────────────────┼───────────────────┼────────────────────┤
  │  off    │  (不送 field)   │ (不送 effort)      │ (不送 thinking)     │
  │  low    │  budget=1024   │ effort="low"      │ thinkingBudget=1024│
  │  medium │  budget=8192   │ effort="medium"   │ thinkingBudget=8192│
  │  high   │  budget=32768  │ effort="high"     │ thinkingBudget=32768│
  └─────────┴────────────────┴───────────────────┴────────────────────┘
"""
from __future__ import annotations

from typing import Optional

# 每家哪些 model id 前綴支援「thinking」(extended thinking / reasoning effort)
# 來源:Anthropic / OpenAI / Google 官方文件,2026-05 盤點。
_THINKING_PREFIXES: dict[str, tuple[str, ...]] = {
    "anthropic": (
        "claude-opus-4",          # 4.0 / 4.1 / 4.6 / 4.7 等 4.x opus 全系列
        "claude-sonnet-4-5",      # 4.5 以上 sonnet
        "claude-sonnet-4-6",
        "claude-3-7-sonnet",      # 第一個有 extended thinking 的 sonnet
    ),
    "openai": (
        "o1",
        "o3",
        "o4",
        "gpt-5",                  # 預留(若 OpenAI 之後 ship)
    ),
    "google": (
        "gemini-2.5",             # 2.5 系列(pro / flash)都支援
        "gemini-3",               # 預留
    ),
}

# 統一 level → 內部 budget tokens / effort
_THINKING_BUDGET_TOKENS: dict[str, int] = {
    "off": 0,
    "low": 1024,
    "medium": 8192,
    "high": 32768,
}

ALL_THINKING_LEVELS = ("off", "low", "medium", "high")


def supports_thinking(provider: str, model_id: str) -> bool:
    """該 (provider, model_id) 是否支援 thinking / reasoning。"""
    if not provider or not model_id:
        return False
    prefixes = _THINKING_PREFIXES.get(provider.lower(), ())
    m = model_id.lower()
    return any(m.startswith(p) for p in prefixes)


def thinking_levels_for(provider: str, model_id: str) -> list[dict]:
    """回 UI 用的 thinking levels 清單。若 model 不支援回空 list。"""
    if not supports_thinking(provider, model_id):
        return []
    # 統一抽象 — 所有支援 thinking 的 model 都暴露 off/low/medium/high 四檔
    return [
        {"value": "off", "label": "關閉(快速)"},
        {"value": "low", "label": "低(快速思考)"},
        {"value": "medium", "label": "中(平衡)"},
        {"value": "high", "label": "高(深度思考,較慢/較貴)"},
    ]


def budget_tokens_for(level: Optional[str]) -> int:
    """Anthropic / Google 用 budget_tokens 數字 — 統一 level 轉成數字。"""
    if not level:
        return 0
    return _THINKING_BUDGET_TOKENS.get(level.lower(), 0)


def is_active_level(level: Optional[str]) -> bool:
    """level 是否「需要送 thinking field」(off / None 都不送)。"""
    return bool(level) and level.lower() not in ("off", "", "none")


def normalize_level(level: Optional[str]) -> Optional[str]:
    """把 user 傳的 thinking_config 標準化;不認識的回 None。"""
    if not level:
        return None
    v = str(level).strip().lower()
    if v in ALL_THINKING_LEVELS:
        return v
    return None
