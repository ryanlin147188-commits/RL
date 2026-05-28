"""LLM provider config Pydantic schemas。

API key 永遠不從 Response 回傳明碼:沿用 EmailConfig 模式,用
``has_api_key: bool`` 給 UI 顯示「目前已設定」的狀態。
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# 三家對應字串(與 LLMProvider.provider_name 一致)
ProviderName = Literal["anthropic", "openai", "google"]
ALLOWED_PROVIDERS = ("anthropic", "openai", "google")


class LlmProviderConfigBase(BaseModel):
    """共用欄位 base — 不含 api_key 本身。"""

    provider: ProviderName
    base_url: Optional[str] = Field(
        default=None,
        max_length=500,
        description=(
            "只有 provider=openai 才需要;指向 OpenAI-compatible 本地推論伺服器。"
            " 例:http://ollama:11434/v1/chat/completions"
        ),
    )
    default_model: Optional[str] = Field(
        default=None,
        max_length=120,
        description="該 provider 的預設模型;呼叫端可 override。",
    )
    enabled: bool = False


class LlmProviderConfigUpdate(LlmProviderConfigBase):
    """upsert payload。

    ``api_key``:
    * 傳明文(非空)= 用 Fernet 加密後寫入
    * 傳 None / 空字串 = **不動**現有值(沿用 EmailConfig 的「空 = 不動」慣例,
      避免使用者只想改 enabled 卻誤清掉 key)
    * 想清掉 key 必須走 DELETE endpoint,語意清楚
    """

    api_key: Optional[str] = Field(
        default=None,
        description="明文 API key;留空 = 不動現有值。",
    )
    thinking_config: Optional[dict] = Field(
        default=None,
        description='思考度設定。格式 {"level": "off"|"low"|"medium"|"high"}。傳 None = 不動既有值。',
    )

    @field_validator("base_url")
    @classmethod
    def _strip_base_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        return v or None


class LlmProviderConfigResponse(BaseModel):
    """**永遠不包含 api_key 明碼**。前端只能看到 has_api_key + key_prefix 遮罩字串。

    ``key_prefix`` 例:"sk-ant-a***XyZw"(前 8 + *** + 後 4),純展示用途。
    Legacy row(0044 migration 之前建的)沒有 prefix → 回 None,前端可顯示
    「請重新存一次 key」提示使用者更新。
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: Optional[str]
    provider: ProviderName
    base_url: Optional[str]
    default_model: Optional[str]
    thinking_config: Optional[dict] = None
    enabled: bool
    has_api_key: bool
    key_prefix: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class LlmProviderTestRequest(BaseModel):
    """POST /api/llm/providers/{provider}/test — 用剛存的 key 跑一個 ping。

    不在 server 端 hardcode prompt;讓 caller 帶或用 default。caller 帶 None
    時用 ``"ping"`` 一字,把回應裁前 200 字回傳。
    """

    model: Optional[str] = Field(default=None, description="覆寫該 provider 的預設模型")
    prompt: Optional[str] = Field(default=None, max_length=500)


class LlmProviderTestResponse(BaseModel):
    ok: bool
    model: str
    provider: ProviderName
    sample_text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    error: Optional[str] = None


class LlmProviderListModelsRequest(BaseModel):
    """list-models 可選帶一把臨時 key(若 UI 還沒儲存)。不帶 → 用 DB 內存的。"""

    api_key: Optional[str] = Field(default=None, description="臨時 key;留空 → 用 DB 已存的")
    base_url: Optional[str] = Field(default=None, description="OpenAI-compatible 本地推論才需要")


class LlmModelInfo(BaseModel):
    id: str
    label: str
    supports_thinking: bool
    thinking_levels: list[dict]


class LlmProviderListModelsResponse(BaseModel):
    provider: ProviderName
    count: int
    models: list[LlmModelInfo]
    error: Optional[str] = None
