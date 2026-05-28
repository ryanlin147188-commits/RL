"""Per-model token 價格表(USD per 1M tokens)。

數字以 2026 Q2 公開定價為準;遇到未列出的模型回 ``(0.0, 0.0)``,並由呼叫端
log 警告,避免使用者看到「免費」的錯覺。

更新節奏:供應商每 3~6 個月會調價或出新 SKU。這個檔案是純資料,改完不用
碰 provider 邏輯。新模型上線時,在這裡加一行即可。
"""
from __future__ import annotations

from typing import Final

# (input_per_1m, output_per_1m, cache_read_per_1m, cache_write_per_1m)
# cache 欄位只有 Anthropic 有效;其他家填 0。
_PRICES: Final[dict[str, tuple[float, float, float, float]]] = {
    # ── Anthropic ────────────────────────────────────────────────────
    "claude-opus-4-7": (15.0, 75.0, 1.50, 18.75),
    "claude-opus-4-6": (15.0, 75.0, 1.50, 18.75),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30, 3.75),
    "claude-haiku-4-5-20251001": (1.0, 5.0, 0.10, 1.25),
    # ── OpenAI ───────────────────────────────────────────────────────
    "gpt-4o": (2.50, 10.0, 0.0, 0.0),
    "gpt-4o-mini": (0.15, 0.60, 0.0, 0.0),
    "gpt-4.1": (2.0, 8.0, 0.0, 0.0),
    "gpt-4.1-mini": (0.40, 1.60, 0.0, 0.0),
    "o3-mini": (1.10, 4.40, 0.0, 0.0),
    # ── Google ───────────────────────────────────────────────────────
    "gemini-2.5-pro": (1.25, 10.0, 0.0, 0.0),
    "gemini-2.5-flash": (0.075, 0.30, 0.0, 0.0),
    "gemini-2.0-flash": (0.075, 0.30, 0.0, 0.0),
}


def compute_cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """根據 token 用量算出本次 chat 的成本(USD)。

    Anthropic 計費規則:cached tokens 不再算 input,要從 input_tokens 扣除
    再分別套用 cache 單價。這裡假設呼叫端傳進來的 input_tokens 已經是
    「不含 cache 部分」的純 fresh input(Anthropic API 回應的 ``input_tokens``
    本來就符合這個約定)。
    """
    price = _PRICES.get(model)
    if price is None:
        return 0.0
    in_p, out_p, cache_r_p, cache_w_p = price
    return (
        input_tokens * in_p
        + output_tokens * out_p
        + cache_read_tokens * cache_r_p
        + cache_write_tokens * cache_w_p
    ) / 1_000_000.0
