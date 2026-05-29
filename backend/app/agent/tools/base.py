"""Tool 抽象基底 + ToolContext + ToolResult。

每個 tool 是一個 class,override ``execute()`` 後 instance 透過 ``REGISTRY.register()``
掛到全域 registry。LLM 看到的是 ``to_toolspec()`` 把這層抽象翻成 LLM 抽象層的
``ToolSpec``(input_schema 直接重用 JSON Schema)。

設計約定:
* ``input_schema`` 用 JSON Schema(三家 LLM 都認;Anthropic 直接吃,OpenAI 走
  function.parameters,Google 在 provider 層自動剝 additionalProperties)
* ``execute()`` 拿 ``**kwargs`` 而非 dict — 讓 IDE 與 type checker 能看到參數
* 任何 raise 由 ``executor`` 在 send_message 迴圈內 catch 轉成 ``ToolResult(error=...)``,
  Tool 自己不該 try/except 包整個 execute
* ``casbin_permission`` 是字串(P.REPORT_READ 那種值);None = 任何登入者可用
* ``requires_confirmation``:destructive action(刪資料、跑生產環境測試)設 True,
  Phase 1c 的 UI 會插入二次確認 modal
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

from app.llm.base import ToolSpec

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.user import User


@dataclass
class ToolContext:
    """單次 tool 執行的上下文。

    ``session_id`` 是 agent_sessions.id,給 audit log / Celery task 串聯用。
    ``organization_id`` 從 user.organization_id 推,放在 ctx 方便 tool 內直接拿
    而不用每次都摸 user。
    """

    db: "AsyncSession"
    user: "User"
    organization_id: Optional[str]
    session_id: str


@dataclass
class ToolResult:
    """Tool 執行結果。

    ``content`` 是「餵給 LLM 看的字串」— 通常是 JSON 字串(LLM 解析力很好,結構
    化資料用 JSON 比自然語言精準)。
    ``error`` 非 None 時 ``content`` 也應給 LLM 一段可讀的失敗訊息;不要把
    stacktrace 直接餵給 LLM 浪費 token。
    ``metadata`` 給前端 UI 用(例如:回一個報告連結讓使用者點;LLM 看不到)。
    """

    content: str
    error: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, content: str, **meta: Any) -> "ToolResult":
        return cls(content=content, metadata=meta)

    @classmethod
    def fail(cls, error_msg: str, llm_visible: Optional[str] = None) -> "ToolResult":
        return cls(content=llm_visible or f"Tool failed: {error_msg}", error=error_msg)


class Tool(ABC):
    """所有 tool 的抽象基底。

    Subclass 必填:``name`` / ``description`` / ``input_schema`` / ``execute``。
    """

    name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = {}
    # 需要的 Casbin permission key(如 ``"report.read"``)。None = 任何登入者可用。
    casbin_permission: Optional[str] = None
    # destructive action 旗標;Phase 1c 的 UI 會在 dispatch 前跳二次確認 modal。
    requires_confirmation: bool = False
    # Phase 1c-1:非同步 tool(派 Celery 後立刻回 task_id,不等結果)。
    # ``ToolResult.metadata`` 需含 ``task_id``;agent_service 會把它寫到
    # ``agent_messages.task_id`` 供前端 polling / WS 訂閱。LLM 看到的 content
    # 仍是 JSON 字串(含 status=queued + task_id),自然會給使用者「已排程」回覆。
    is_async: bool = False
    # 每個 user 同時 in-flight 上限(None = 不限)。Phase 1c-1 對 ``run_test_case``
    # 設為 3,呼應你提的「executor 上限 3 / recorder 上限 2」防止 LLM 在
    # tool-use loop 內把 robot-runner 容器派爆。同步輕量 tool(query_report 等)
    # 留 None 不限。
    concurrency_limit_per_user: Optional[int] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Subclass 必須提供 name/description/input_schema
        # 跳過 Tool 基底自身;也跳過 MCPToolAdapterBase — 它是 factory 用的 abstract
        # base,真正的 MCP tool subclass 是 make_mcp_tool_adapter() 動態生成時才填
        # name/description/input_schema(會通過底下的檢查),Base 本身 name="" 是
        # placeholder,不該被 enforce。
        if cls.__name__ in ("Tool", "MCPToolAdapterBase"):
            return
        for attr in ("name", "description"):
            if not getattr(cls, attr, ""):
                raise TypeError(
                    f"Tool subclass {cls.__name__} 必須設定 class-level ``{attr}``"
                )
        if not getattr(cls, "input_schema", None):
            raise TypeError(
                f"Tool subclass {cls.__name__} 必須設定 class-level ``input_schema``"
            )

    @abstractmethod
    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        ...

    def to_toolspec(self) -> ToolSpec:
        """轉成 LLM 抽象層的 ToolSpec(送進 chat() 的 tools=)。"""
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            requires_confirmation=self.requires_confirmation,
        )
