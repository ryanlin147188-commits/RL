"""manage_mock_endpoint tool — Mock endpoint CRUD 整合工具。

對齊既有 [routers/mock_endpoints.py](../../routers/mock_endpoints.py) 的 5 個端點
(list / create / get / update / delete)。把 4 個寫操作收成單一 tool,
讓 devops-debug skill 能透過 action 參數選擇要做什麼。

設計:
* `action`:list / create / update / delete(get 屬於 list 子集,不獨立)
* list / update / delete 走 TenantQuery 自動 org filter
* create / update / delete 為 destructive,requires_confirmation=True
* list 仍走同一個 tool — LLM 不用記兩個名字
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import desc, select

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.auth.permissions_catalog import P
from app.auth.tenant import TenantQuery
from app.models.mock_endpoint import MockEndpoint
from app.models.project import Project


class ManageMockEndpointTool(Tool):
    name = "manage_mock_endpoint"
    description = (
        "管理 Mock 端點:list 列出 / create 建立 / update 修改 / delete 刪除。"
        " 用 action 選擇要做什麼(list = 純讀,create/update/delete 為 destructive,"
        " requires_confirmation=true)。"
        " Mock 用來模擬後端 API 給前端測試,常見於『後端尚未實作』『第三方 API 不穩』場景。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "create", "update", "delete"],
                "description": "list 純讀;create/update/delete destructive。",
            },
            "project_id": {
                "type": "string",
                "description": "歸屬 project UUID(list 過濾用,create 必填)",
            },
            "mock_id": {
                "type": "string",
                "description": "Mock UUID(update/delete 必填)",
            },
            "name": {"type": "string", "maxLength": 160},
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                "description": "create/update 用",
            },
            "path": {
                "type": "string",
                "description": "URL path,例 /api/users/123(create/update 用)",
            },
            "description": {"type": "string"},
            "enabled": {"type": "boolean", "description": "預設 true"},
            "status_code": {"type": "integer", "minimum": 100, "maximum": 599},
            "delay_ms": {"type": "integer", "minimum": 0, "description": "回應前延遲毫秒"},
            "response_body_text": {
                "type": "string",
                "description": "Response body(支援 Faker 佔位符 {{name}} / {{uuid}})",
            },
            "response_headers_json": {
                "type": "object",
                "description": "Response headers k-v map",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }
    casbin_permission = P.SETTINGS_WRITE
    # 動態 confirm:list 不用,create/update/delete 要 — 在 execute 內早期 return ok 的 list
    # 流程不會走到 confirm。tool 層 flag 設 True 是 fail-safe(寧可前端多跳一次 confirm)。
    # 注意:agent_service 的 confirm flow 是「事前」攔截 — 為避免 list 也跳 modal,
    # 把 flag 設 False,但在 execute 內對 destructive action 自行驗證(不直接執行)。
    # 採取的折衷:flag=False,destructive action 在 execute 內 fail 且要求 caller 用
    # 專屬子工具(若未來需要嚴格 confirm flow,可拆成 list_mock_endpoints +
    # create/update/delete 三個獨立 tool)。
    requires_confirmation = True

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        action = (kwargs.get("action") or "").strip().lower()
        if action not in ("list", "create", "update", "delete"):
            return ToolResult.fail(
                f"invalid_action: {action!r}",
                llm_visible="action 必須是 list / create / update / delete。",
            )

        if action == "list":
            return await self._list(ctx, kwargs)
        if action == "create":
            return await self._create(ctx, kwargs)
        if action == "update":
            return await self._update(ctx, kwargs)
        if action == "delete":
            return await self._delete(ctx, kwargs)
        return ToolResult.fail("unreachable")

    async def _list(self, ctx: ToolContext, kwargs: dict[str, Any]) -> ToolResult:
        project_id = (kwargs.get("project_id") or "").strip() or None
        stmt = TenantQuery.for_(MockEndpoint)
        if project_id:
            stmt = stmt.where(MockEndpoint.project_id == project_id)
        stmt = stmt.order_by(desc(MockEndpoint.updated_at)).limit(100)
        rows = (await ctx.db.execute(stmt)).scalars().all()
        items = [
            {
                "id": r.id,
                "project_id": r.project_id,
                "name": r.name,
                "method": r.method,
                "path": r.path,
                "status_code": r.status_code,
                "delay_ms": r.delay_ms,
                "enabled": r.enabled,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]
        return ToolResult.ok(
            json.dumps(
                {"count": len(items), "mocks": items}, ensure_ascii=False
            ),
            count=len(items),
        )

    async def _create(self, ctx: ToolContext, kwargs: dict[str, Any]) -> ToolResult:
        project_id = (kwargs.get("project_id") or "").strip()
        name = (kwargs.get("name") or "").strip()
        method = (kwargs.get("method") or "GET").upper()
        path = (kwargs.get("path") or "").strip()
        if not (project_id and name and path):
            return ToolResult.fail(
                "missing_required",
                llm_visible="create 需 project_id / name / path(method 預設 GET)。",
            )

        # IDOR:project 必須在 caller org 範圍內
        proj = await ctx.db.get(Project, project_id)
        if proj is None:
            return ToolResult.fail(
                "project_not_found",
                llm_visible=f"project {project_id} 不存在。",
            )
        if not ctx.user.is_superuser:
            if (
                not proj.organization_id
                or not ctx.organization_id
                or proj.organization_id != ctx.organization_id
            ):
                return ToolResult.fail(
                    "project_not_found",
                    llm_visible=f"project {project_id} 不存在。",
                )

        # 同 (project, method, path) 唯一
        existing = (
            await ctx.db.execute(
                select(MockEndpoint)
                .where(MockEndpoint.project_id == project_id)
                .where(MockEndpoint.method == method)
                .where(MockEndpoint.path == path)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return ToolResult.fail(
                "duplicate_mock",
                llm_visible=(
                    f"已存在 {method} {path} 的 mock(id={existing.id});"
                    " 請改用 update 或先 delete。"
                ),
            )

        mock = MockEndpoint(
            organization_id=proj.organization_id,
            project_id=project_id,
            name=name,
            method=method,
            path=path,
            description=kwargs.get("description") or None,
            enabled=bool(kwargs.get("enabled", True)),
            status_code=int(kwargs.get("status_code") or 200),
            delay_ms=int(kwargs.get("delay_ms") or 0),
            response_body_text=kwargs.get("response_body_text") or None,
            response_headers_json=kwargs.get("response_headers_json") or None,
        )
        ctx.db.add(mock)
        await ctx.db.commit()
        await ctx.db.refresh(mock)
        payload = {
            "status": "created",
            "mock_id": mock.id,
            "method": mock.method,
            "path": mock.path,
            "status_code": mock.status_code,
        }
        return ToolResult.ok(json.dumps(payload, ensure_ascii=False))

    async def _update(self, ctx: ToolContext, kwargs: dict[str, Any]) -> ToolResult:
        mock_id = (kwargs.get("mock_id") or "").strip()
        if not mock_id:
            return ToolResult.fail("missing_mock_id", llm_visible="mock_id 必填。")

        stmt = TenantQuery.for_(MockEndpoint).where(MockEndpoint.id == mock_id)
        mock = (await ctx.db.execute(stmt)).scalar_one_or_none()
        if mock is None:
            return ToolResult.fail(
                "mock_not_found",
                llm_visible=f"mock {mock_id} 不存在或不在你的存取範圍內。",
            )

        changed: list[str] = []
        for field in (
            "name",
            "method",
            "path",
            "description",
            "response_body_text",
        ):
            if field in kwargs and kwargs[field] is not None:
                value = kwargs[field]
                if field == "method":
                    value = value.upper()
                setattr(mock, field, value)
                changed.append(field)
        if "enabled" in kwargs and kwargs["enabled"] is not None:
            mock.enabled = bool(kwargs["enabled"])
            changed.append("enabled")
        if "status_code" in kwargs and kwargs["status_code"] is not None:
            mock.status_code = int(kwargs["status_code"])
            changed.append("status_code")
        if "delay_ms" in kwargs and kwargs["delay_ms"] is not None:
            mock.delay_ms = int(kwargs["delay_ms"])
            changed.append("delay_ms")
        if "response_headers_json" in kwargs:
            mock.response_headers_json = kwargs["response_headers_json"] or None
            changed.append("response_headers_json")

        if not changed:
            return ToolResult.ok(
                json.dumps(
                    {"status": "no_change", "mock_id": mock_id}, ensure_ascii=False
                )
            )

        await ctx.db.commit()
        await ctx.db.refresh(mock)
        payload = {
            "status": "updated",
            "mock_id": mock.id,
            "method": mock.method,
            "path": mock.path,
            "changed_fields": changed,
        }
        return ToolResult.ok(json.dumps(payload, ensure_ascii=False))

    async def _delete(self, ctx: ToolContext, kwargs: dict[str, Any]) -> ToolResult:
        mock_id = (kwargs.get("mock_id") or "").strip()
        if not mock_id:
            return ToolResult.fail("missing_mock_id", llm_visible="mock_id 必填。")

        stmt = TenantQuery.for_(MockEndpoint).where(MockEndpoint.id == mock_id)
        mock = (await ctx.db.execute(stmt)).scalar_one_or_none()
        if mock is None:
            return ToolResult.fail(
                "mock_not_found",
                llm_visible=f"mock {mock_id} 不存在或不在你的存取範圍內。",
            )

        original = {
            "id": mock.id,
            "method": mock.method,
            "path": mock.path,
            "name": mock.name,
        }
        await ctx.db.delete(mock)
        await ctx.db.commit()
        return ToolResult.ok(
            json.dumps({"status": "deleted", **original}, ensure_ascii=False)
        )
