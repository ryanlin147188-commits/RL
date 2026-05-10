"""Platform MCP server — 把後端 API 暴露成 MCP tool 給 Hermes ACP LLM 用。

設計同 mem0/mem0_proxy.py:
- FastMCP streamable HTTP 子 app,掛在 backend `/platform-mcp/` 之下
- Hermes provision 時把這個 URL 加到 mcp_servers list,LLM 收到對應 tool schema
- Auth:`X-Sidecar-Auth` 與 hermes/mem0 共用同一個 SIDECAR_AUTH_TOKEN(內網限定)
- 使用者識別:`X-Platform-User: <username>` header,backend 用這個解出 ORM User

Tools(目前僅 MVP,後續再加 testcase / defect / 等):
- create_project(name, description?) → 建專案,回 project id + name
- list_projects() → 列當前 user org 內可看到的所有專案

注意:**不要加 `from __future__ import annotations`** — FastMCP 的 from_function 用
issubclass(param.annotation, Context) 檢查 tool 函式參數,若 annotations 是字串
(future annotations 模式)會炸 TypeError。其他 router 用 future annotations
的不影響,只有這個 module 例外。
"""
import logging
import uuid
from contextvars import ContextVar
from typing import Optional

from fastapi import Request
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.defect import Defect, DefectPriority, DefectSeverity, DefectStatus
from app.models.execution_report import ExecutionReport, ReportStatus
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.recording import RecordingSession
from app.models.requirement import (
    Requirement,
    RequirementPriority,
    RequirementSource,
    RequirementStatus,
)
from app.models.test_document import DocumentCategory, TestDocument
from app.models.test_milestone import MilestoneStatus, TestMilestone
from app.models.test_plan import TestPlan, TestPlanStatus
from app.models.test_version import TestVersion
from app.models.todo_item import TodoItem, TodoItemType, TodoPriority, TodoStatus
from app.models.tree_node import LevelType, TreeNode
from app.models.user import User

LOG = logging.getLogger(__name__)

# 由 auth_middleware 注入的當前呼叫者 username — FastMCP tool 內部沒辦法直接拿
# request headers,用 contextvar 是最乾淨的橋(同 mem0_proxy.py 的做法)。
_current_platform_user: ContextVar[str] = ContextVar("_current_platform_user", default="")


# ── auth & user-context middleware ────────────────────────────────────────
async def _platform_mcp_auth(request: Request, call_next):
    """ASGI middleware:驗 X-Sidecar-Auth + 注入 user contextvar。

    對 /platform-mcp/* 範圍生效;其他路徑直通(由 FastAPI 主 app 自己的 auth 處理)。
    """
    path = request.url.path
    if not path.startswith("/platform-mcp"):
        return await call_next(request)

    expected = settings.SIDECAR_AUTH_TOKEN
    if not expected:
        return _http_text(503, "platform_mcp disabled: SIDECAR_AUTH_TOKEN not configured")

    incoming = request.headers.get("x-sidecar-auth", "")
    if incoming != expected:
        return _http_text(401, "invalid X-Sidecar-Auth")

    username = request.headers.get("x-platform-user", "").strip()
    if not username:
        return _http_text(400, "missing X-Platform-User header")

    token = _current_platform_user.set(username)
    try:
        return await call_next(request)
    finally:
        _current_platform_user.reset(token)


def _http_text(status: int, msg: str):
    """Tiny helper — 從 middleware 直接回 plain-text response,不引入 starlette dep 寫死。"""
    from starlette.responses import PlainTextResponse
    return PlainTextResponse(msg, status_code=status)


# ── FastMCP server 與 tool 實作 ────────────────────────────────────────────
# Module-level — 讓 main.py lifespan 拿來 enter session_manager.run()
_mcp_server_instance = None


def get_mcp_server():
    """讓 main.py lifespan 抓到目前的 FastMCP server(沒成功建就回 None)。"""
    return _mcp_server_instance


def _build_mcp_app():
    """組裝 platform-mcp 子 app。失敗會 raise(讓 backend 啟動時 fail-fast)。"""
    global _mcp_server_instance
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    sec = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        # backend 在 docker network 裡的 service 名 / loopback;允許 hermes 用
        # `backend:8000` 連進來 + 開發時 localhost 直連。
        allowed_hosts=[
            "backend", "backend:8000",
            "127.0.0.1", "127.0.0.1:8000",
            "localhost", "localhost:8000",
        ],
        allowed_origins=["*"],
    )
    server = FastMCP(name="rl-platform", transport_security=sec)
    _mcp_server_instance = server

    # ── tool: create_project ───────────────────────────────────────────
    # 注意:**不要用 Optional[str]** — FastMCP 的 from_function 用 issubclass()
    # 檢查 param annotation,Optional[str] 是 typing 構造不是 class,會炸 TypeError。
    # 用 `str = ""` + 內部把空值當 None 處理,行為等價且 schema 乾淨。
    @server.tool()
    async def create_project(name: str, description: str = "") -> str:
        """建立新測試專案到當前使用者的組織。

        當使用者要求「新增/建立/開一個 ___ 專案」這類動作時呼叫。建好之後使用者
        在「專案目錄」立即可見,並自動成為該專案 member(以呼叫者組織內預設角色)。

        Args:
            name: 專案名稱(必填,1–80 字)
            description: 專案說明(選填,可留空字串)
        """
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing — refusing to create project."

        name = (name or "").strip()
        if not name:
            return "Failed: project name is required."
        if len(name) > 80:
            return f"Failed: project name too long ({len(name)} > 80)."

        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."

            # 同名檢查 — 同一 org 內不允許重複專案名(避免 LLM 重複建立)
            stmt = select(Project).where(
                Project.name == name,
                Project.organization_id == user.organization_id,
            )
            existing = (await db.execute(stmt)).scalar_one_or_none()
            if existing:
                return (
                    f"專案「{name}」已存在(id={existing.id}),"
                    "未重複建立。如要新增同名專案請改用其他名稱。"
                )

            project = Project(
                name=name,
                organization_id=user.organization_id,
                description=(description or "").strip() or None,
                owner=user.username,
                status="InProgress",
            )
            db.add(project)
            await db.flush()
            db.add(ProjectMember(
                project_id=project.id,
                username=user.username,
                role_id=None,
                status="active",
            ))
            await db.commit()
            return (
                f"✓ 已建立專案「{name}」(id={project.id})。"
                "使用者重新整理「專案目錄」即可看到。"
            )

    # ── tool: list_projects ────────────────────────────────────────────
    @server.tool()
    async def list_projects(limit: int = 30) -> str:
        """列出當前使用者組織內可看到的所有專案。回傳 markdown 列表。

        Args:
            limit: 最多回傳幾筆(預設 30,上限 100)
        """
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing — refusing to list projects."

        try:
            limit = max(1, min(100, int(limit)))
        except (TypeError, ValueError):
            limit = 30

        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."

            stmt = select(Project)
            if not user.is_superuser:
                stmt = stmt.where(Project.organization_id == user.organization_id)
            stmt = stmt.order_by(Project.created_at.desc()).limit(limit)
            rows = (await db.execute(stmt)).scalars().all()

        if not rows:
            return "(目前沒有任何專案)"
        lines = [f"找到 {len(rows)} 個專案:"]
        for p in rows:
            d = (p.description or "").strip()
            line = f"- **{p.name}** (id=`{p.id}`, status={p.status or 'N/A'})"
            if d:
                line += f" — {d[:80]}"
            lines.append(line)
        return "\n".join(lines)

    # ── tool: list_defects ─────────────────────────────────────────────
    @server.tool()
    async def list_defects(project_id: str = "", limit: int = 30) -> str:
        """列出當前使用者可看見的缺陷。可用 project_id 過濾,留空則跨組織內所有專案。

        Args:
            project_id: 限定專案(留空 = 不限)
            limit: 最多回傳幾筆(預設 30,上限 100)
        """
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing — refusing to list defects."
        try:
            limit = max(1, min(100, int(limit)))
        except (TypeError, ValueError):
            limit = 30

        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."

            stmt = select(Defect).order_by(desc(Defect.created_at))
            pid = (project_id or "").strip()
            if pid:
                stmt = stmt.where(Defect.project_id == pid)
            stmt = _scope_for_user(stmt, Defect, user)
            stmt = stmt.limit(limit)
            rows = (await db.execute(stmt)).scalars().all()

        if not rows:
            return "(沒有符合條件的缺陷)"
        lines = [f"找到 {len(rows)} 筆缺陷:"]
        for d in rows:
            sev = getattr(d.severity, "value", d.severity) or "?"
            st = getattr(d.status, "value", d.status) or "?"
            lines.append(
                f"- **{d.code} {d.title}** (id=`{d.id}`, severity={sev}, status={st})"
            )
        return "\n".join(lines)

    # ── tool: create_defect ────────────────────────────────────────────
    @server.tool()
    async def create_defect(
        project_id: str,
        title: str,
        description: str = "",
        severity: str = "Minor",
        priority: str = "P2",
    ) -> str:
        """在指定專案下建立一筆缺陷紀錄(預設 Minor / P2 / New)。

        Args:
            project_id: 所屬專案 id(必填,可先呼叫 list_projects 取得)
            title: 缺陷標題(必填,1–200 字)
            description: 缺陷說明(選填)
            severity: Critical / Major / Minor / Trivial(預設 Minor)
            priority: P0 / P1 / P2 / P3(預設 P2)
        """
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing — refusing to create defect."
        title = (title or "").strip()
        pid = (project_id or "").strip()
        if not pid:
            return "Failed: project_id is required."
        if not title:
            return "Failed: defect title is required."
        if len(title) > 200:
            return f"Failed: title too long ({len(title)} > 200)."

        sev_e = _enum_or_default(DefectSeverity, severity, DefectSeverity.MINOR)
        pri_e = _enum_or_default(DefectPriority, priority, DefectPriority.P2)

        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."
            project = await _project_in_scope(db, pid, user)
            if project is None:
                return f"Failed: project id={pid} not found or not accessible."

            # 自動產 code(DEF-NNN);用一個粗略 count + 1 即可,真衝突 DB 唯一鍵會 raise
            cnt = (await db.execute(
                select(Defect).where(Defect.project_id == pid)
            )).all()
            code = f"DEF-{len(cnt) + 1:03d}"

            defect = Defect(
                project_id=pid,
                code=code,
                title=title,
                description=(description or "").strip() or None,
                severity=sev_e,
                priority=pri_e,
                status=DefectStatus.NEW,
                reporter=user.username,
                attachments_json=[],
            )
            db.add(defect)
            await db.commit()
            return (
                f"✓ 已在專案「{project.name}」建立缺陷「{code} {title}」"
                f"(severity={sev_e.value}, priority={pri_e.value})。"
            )

    # ── tool: list_documents ───────────────────────────────────────────
    @server.tool()
    async def list_documents(project_id: str = "", limit: int = 30) -> str:
        """列出可看見的測試文件。可用 project_id 過濾。

        Args:
            project_id: 限定專案(留空 = 不限)
            limit: 最多回傳幾筆(預設 30,上限 100)
        """
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing — refusing to list documents."
        try:
            limit = max(1, min(100, int(limit)))
        except (TypeError, ValueError):
            limit = 30

        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."

            stmt = select(TestDocument).order_by(desc(TestDocument.updated_at))
            pid = (project_id or "").strip()
            if pid:
                stmt = stmt.where(TestDocument.project_id == pid)
            stmt = _scope_for_user(stmt, TestDocument, user)
            stmt = stmt.limit(limit)
            rows = (await db.execute(stmt)).scalars().all()

        if not rows:
            return "(沒有符合條件的測試文件)"
        lines = [f"找到 {len(rows)} 份測試文件:"]
        for doc in rows:
            cat = getattr(doc.category, "value", doc.category) or "?"
            lines.append(
                f"- **{doc.code} {doc.title}** (id=`{doc.id}`, category={cat})"
            )
        return "\n".join(lines)

    # ── tool: create_document ──────────────────────────────────────────
    @server.tool()
    async def create_document(
        project_id: str,
        title: str,
        content_md: str = "",
        category: str = "Note",
    ) -> str:
        """建立測試文件(支援 Markdown 內容)。category 可選 Strategy / Guide /
        Runbook / Checklist / Note / Other(預設 Note)。

        Args:
            project_id: 所屬專案 id(必填)
            title: 文件標題(必填,1–300 字)
            content_md: 文件內容(Markdown,選填)
            category: 文件類別(預設 Note)
        """
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing — refusing to create document."
        title = (title or "").strip()
        pid = (project_id or "").strip()
        if not pid:
            return "Failed: project_id is required."
        if not title:
            return "Failed: document title is required."
        if len(title) > 300:
            return f"Failed: title too long ({len(title)} > 300)."

        cat_e = _enum_or_default(DocumentCategory, category, DocumentCategory.NOTE)

        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."
            project = await _project_in_scope(db, pid, user)
            if project is None:
                return f"Failed: project id={pid} not found or not accessible."

            cnt = (await db.execute(
                select(TestDocument).where(TestDocument.project_id == pid)
            )).all()
            code = f"DOC-{len(cnt) + 1:03d}"

            doc = TestDocument(
                project_id=pid,
                code=code,
                title=title,
                category=cat_e,
                content_md=(content_md or "").strip() or None,
                owner=user.username,
            )
            db.add(doc)
            await db.commit()
            return (
                f"✓ 已在專案「{project.name}」建立測試文件「{code} {title}」"
                f"(category={cat_e.value})。"
            )

    # ── tool: search_documents ─────────────────────────────────────────
    @server.tool()
    async def search_documents(query: str, limit: int = 20) -> str:
        """以關鍵字搜尋測試文件(比對 title / summary / content_md,case-insensitive)。

        Args:
            query: 搜尋關鍵字
            limit: 最多回傳幾筆(預設 20,上限 50)
        """
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing — refusing to search documents."
        q = (query or "").strip()
        if not q:
            return "Failed: query is empty."
        try:
            limit = max(1, min(50, int(limit)))
        except (TypeError, ValueError):
            limit = 20

        like = f"%{q}%"
        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."
            stmt = (
                select(TestDocument)
                .where(or_(
                    TestDocument.title.ilike(like),
                    TestDocument.summary.ilike(like),
                    TestDocument.content_md.ilike(like),
                ))
                .order_by(desc(TestDocument.updated_at))
            )
            stmt = _scope_for_user(stmt, TestDocument, user).limit(limit)
            rows = (await db.execute(stmt)).scalars().all()

        if not rows:
            return f'(沒有符合「{q}」的測試文件)'
        lines = [f'找到 {len(rows)} 份符合「{q}」的測試文件:']
        for doc in rows:
            cat = getattr(doc.category, "value", doc.category) or "?"
            snippet = ""
            for src in (doc.summary or "", doc.content_md or "", doc.title or ""):
                if q.lower() in (src or "").lower():
                    snippet = (src or "").strip()[:120]
                    break
            line = f"- **{doc.code} {doc.title}** (id=`{doc.id}`, category={cat})"
            if snippet:
                line += f" — {snippet}"
            lines.append(line)
        return "\n".join(lines)

    # ── tool: list_testcases ───────────────────────────────────────────
    @server.tool()
    async def list_testcases(project_id: str, limit: int = 50) -> str:
        """列出指定專案中的測試案例(testcase 葉節點)。

        Args:
            project_id: 所屬專案 id(必填,先用 list_projects 取得)
            limit: 最多回傳幾筆(預設 50,上限 200)
        """
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing — refusing to list testcases."
        pid = (project_id or "").strip()
        if not pid:
            return "Failed: project_id is required."
        try:
            limit = max(1, min(200, int(limit)))
        except (TypeError, ValueError):
            limit = 50

        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."
            project = await _project_in_scope(db, pid, user)
            if project is None:
                return f"Failed: project id={pid} not found or not accessible."

            stmt = (
                select(TreeNode)
                .where(
                    TreeNode.project_id == pid,
                    TreeNode.level_type == LevelType.TESTCASE,
                )
                .order_by(TreeNode.sort_order, TreeNode.name)
                .limit(limit)
            )
            rows = (await db.execute(stmt)).scalars().all()

        if not rows:
            return f"(專案「{project.name}」目前沒有測試案例)"
        lines = [f'專案「{project.name}」共 {len(rows)} 個測試案例:']
        for n in rows:
            st = n.content_status or "approved"
            lines.append(f"- **{n.name}** (id=`{n.id}`, status={st})")
        return "\n".join(lines)

    # ── tool: create_simple_testcase ───────────────────────────────────
    @server.tool()
    async def create_simple_testcase(
        project_id: str,
        scenario_path: str,
        name: str,
    ) -> str:
        """在指定專案下建測試案例,中間階層(FEATURE/PLATFORM/PAGE/SCENARIO)
        若不存在會自動串好。scenario_path 用「/」分隔,4 段:
        FEATURE/PLATFORM/PAGE/SCENARIO,例如「登入/Web/登入頁/正向流程」。

        Args:
            project_id: 所屬專案 id
            scenario_path: 4 段「/」分隔路徑,對應 FEATURE/PLATFORM/PAGE/SCENARIO
            name: 測試案例名稱
        """
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing — refusing to create testcase."
        pid = (project_id or "").strip()
        name = (name or "").strip()
        if not pid or not name:
            return "Failed: project_id and name are required."
        parts = [p.strip() for p in (scenario_path or "").split("/") if p.strip()]
        if len(parts) != 4:
            return (
                "Failed: scenario_path 必須是 4 段 (FEATURE/PLATFORM/PAGE/SCENARIO),"
                f"目前 {len(parts)} 段。"
            )

        levels = [LevelType.FEATURE, LevelType.PLATFORM, LevelType.PAGE, LevelType.SCENARIO]
        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."
            project = await _project_in_scope(db, pid, user)
            if project is None:
                return f"Failed: project id={pid} not found or not accessible."

            parent_id: Optional[str] = None
            for label, lv in zip(parts, levels):
                stmt = select(TreeNode).where(
                    TreeNode.project_id == pid,
                    TreeNode.parent_id == parent_id,
                    TreeNode.level_type == lv,
                    TreeNode.name == label,
                )
                node = (await db.execute(stmt)).scalar_one_or_none()
                if node is None:
                    node = TreeNode(
                        project_id=pid, parent_id=parent_id,
                        level_type=lv, name=label, sort_order=0,
                    )
                    db.add(node)
                    await db.flush()
                parent_id = node.id

            tc = TreeNode(
                project_id=pid, parent_id=parent_id,
                level_type=LevelType.TESTCASE, name=name, sort_order=0,
            )
            db.add(tc)
            await db.commit()
            return (
                f"✓ 已在專案「{project.name}」建立測試案例「{name}」"
                f"(路徑:{' / '.join(parts)},id=`{tc.id}`)。"
            )

    # ── tool: update_defect_status ─────────────────────────────────────
    @server.tool()
    async def update_defect_status(defect_id: str, new_status: str) -> str:
        """更新缺陷狀態。new_status ∈ New/Assigned/InProgress/InReview/
        ReworkRequired/Verified/Closed。

        Args:
            defect_id: 缺陷 id
            new_status: 新狀態
        """
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing."
        did = (defect_id or "").strip()
        if not did:
            return "Failed: defect_id is required."

        st = _enum_or_default(DefectStatus, new_status, None)
        if st is None:
            return f"Failed: invalid status '{new_status}'."

        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."
            defect = await db.get(Defect, did)
            if defect is None:
                return f"Failed: defect id={did} not found."
            project = await _project_in_scope(db, defect.project_id, user)
            if project is None:
                return f"Failed: defect's project not accessible."
            defect.status = st
            await db.commit()
            return f"✓ 缺陷「{defect.code} {defect.title}」已切到 {st.value}。"

    # ── tool: list_requirements ────────────────────────────────────────
    @server.tool()
    async def list_requirements(project_id: str = "", limit: int = 30) -> str:
        """列出需求 (RTM)。可選 project_id 過濾。"""
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing."
        try:
            limit = max(1, min(100, int(limit)))
        except (TypeError, ValueError):
            limit = 30

        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."
            stmt = select(Requirement).order_by(desc(Requirement.created_at))
            pid = (project_id or "").strip()
            if pid:
                stmt = stmt.where(Requirement.project_id == pid)
            stmt = _scope_for_user(stmt, Requirement, user).limit(limit)
            rows = (await db.execute(stmt)).scalars().all()

        if not rows:
            return "(沒有符合條件的需求)"
        lines = [f"找到 {len(rows)} 筆需求:"]
        for r in rows:
            pri = getattr(r.priority, "value", r.priority) or "?"
            st = getattr(r.status, "value", r.status) or "?"
            lines.append(f"- **{r.code} {r.title}** (id=`{r.id}`, priority={pri}, status={st})")
        return "\n".join(lines)

    # ── tool: create_requirement ───────────────────────────────────────
    @server.tool()
    async def create_requirement(
        project_id: str,
        title: str,
        description: str = "",
        priority: str = "Should",
        source: str = "PRD",
    ) -> str:
        """建立需求。priority ∈ Must/Should/Could/Wont(預設 Should)、
        source ∈ PRD/Customer/Regulatory/Security/Internal(預設 PRD)。
        """
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing."
        pid = (project_id or "").strip()
        title = (title or "").strip()
        if not pid or not title:
            return "Failed: project_id and title are required."
        if len(title) > 300:
            return f"Failed: title too long ({len(title)} > 300)."

        pri_e = _enum_or_default(RequirementPriority, priority, RequirementPriority.SHOULD)
        src_e = _enum_or_default(RequirementSource, source, RequirementSource.PRD)

        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."
            project = await _project_in_scope(db, pid, user)
            if project is None:
                return f"Failed: project id={pid} not accessible."

            cnt = (await db.execute(
                select(Requirement).where(Requirement.project_id == pid)
            )).all()
            code = f"REQ-{len(cnt) + 1:03d}"
            req = Requirement(
                project_id=pid, code=code, title=title,
                description=(description or "").strip() or None,
                priority=pri_e, source=src_e,
                status=RequirementStatus.NEW,
                owner=user.username,
            )
            db.add(req)
            await db.commit()
            return (
                f"✓ 已在專案「{project.name}」建立需求「{code} {title}」"
                f"(priority={pri_e.value}, source={src_e.value})。"
            )

    # ── tool: list_milestones ──────────────────────────────────────────
    @server.tool()
    async def list_milestones(project_id: str = "", limit: int = 30) -> str:
        """列出測試時程 milestone。可選 project_id 過濾。"""
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing."
        try:
            limit = max(1, min(100, int(limit)))
        except (TypeError, ValueError):
            limit = 30

        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."
            stmt = select(TestMilestone).order_by(TestMilestone.start_date)
            pid = (project_id or "").strip()
            if pid:
                stmt = stmt.where(TestMilestone.project_id == pid)
            stmt = _scope_for_user(stmt, TestMilestone, user).limit(limit)
            rows = (await db.execute(stmt)).scalars().all()

        if not rows:
            return "(沒有符合條件的時程)"
        lines = [f"找到 {len(rows)} 筆時程:"]
        for m in rows:
            st = getattr(m.status, "value", m.status) or "?"
            lines.append(
                f"- **{m.name}** (id=`{m.id}`, {m.start_date} → {m.end_date}, status={st})"
            )
        return "\n".join(lines)

    # ── tool: create_milestone ─────────────────────────────────────────
    @server.tool()
    async def create_milestone(
        project_id: str,
        name: str,
        start_date: str,
        end_date: str,
        description: str = "",
    ) -> str:
        """建立測試時程。日期格式 YYYY-MM-DD。

        Args:
            project_id: 所屬專案 id
            name: 時程名稱
            start_date: 開始日期 (YYYY-MM-DD)
            end_date: 結束日期 (YYYY-MM-DD)
            description: 說明(選填)
        """
        from datetime import date as _date
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing."
        pid = (project_id or "").strip()
        name = (name or "").strip()
        if not pid or not name:
            return "Failed: project_id and name are required."

        def _parse(s: str):
            try:
                y, m, d = s.split("-")
                return _date(int(y), int(m), int(d))
            except Exception:
                return None
        sd, ed = _parse(start_date), _parse(end_date)
        if sd is None or ed is None:
            return "Failed: start_date / end_date must be YYYY-MM-DD."

        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."
            project = await _project_in_scope(db, pid, user)
            if project is None:
                return f"Failed: project id={pid} not accessible."

            ms = TestMilestone(
                project_id=pid, name=name,
                description=(description or "").strip() or None,
                start_date=sd, end_date=ed,
                status=MilestoneStatus.NEW,
                owner=user.username,
            )
            db.add(ms)
            await db.commit()
            return f"✓ 已建立時程「{name}」({sd} → {ed})。"

    # ── tool: list_test_versions ───────────────────────────────────────
    @server.tool()
    async def list_test_versions(project_id: str = "", limit: int = 30) -> str:
        """列出測試版號。可選 project_id 過濾。"""
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing."
        try:
            limit = max(1, min(100, int(limit)))
        except (TypeError, ValueError):
            limit = 30

        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."
            stmt = select(TestVersion).order_by(desc(TestVersion.created_at))
            pid = (project_id or "").strip()
            if pid:
                stmt = stmt.where(TestVersion.project_id == pid)
            stmt = _scope_for_user(stmt, TestVersion, user).limit(limit)
            rows = (await db.execute(stmt)).scalars().all()

        if not rows:
            return "(沒有測試版號)"
        lines = [f"找到 {len(rows)} 個版號:"]
        for v in rows:
            lines.append(
                f"- **{v.version_label}** ({v.platform}) — id=`{v.id}`, status={v.status}"
            )
        return "\n".join(lines)

    # ── tool: create_test_version ──────────────────────────────────────
    @server.tool()
    async def create_test_version(
        project_id: str,
        version_label: str,
        platform: str = "WEB",
        description: str = "",
    ) -> str:
        """建立測試版號。platform ∈ WEB/API/APP(預設 WEB)。

        Args:
            project_id: 所屬專案 id
            version_label: 版號(例:v1.2.3 / 2026-Q2)
            platform: WEB / API / APP
            description: 說明(選填)
        """
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing."
        pid = (project_id or "").strip()
        ver = (version_label or "").strip()
        if not pid or not ver:
            return "Failed: project_id and version_label are required."
        plat = (platform or "WEB").strip().upper()
        if plat not in ("WEB", "API", "APP"):
            plat = "WEB"

        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."
            project = await _project_in_scope(db, pid, user)
            if project is None:
                return f"Failed: project id={pid} not accessible."
            tv = TestVersion(
                id=str(uuid.uuid4()),
                project_id=pid, platform=plat, version_label=ver,
                description=(description or "").strip() or None,
                status="released",
            )
            db.add(tv)
            await db.commit()
            return f"✓ 已建立版號「{ver}」({plat})。"

    # ── tool: list_test_plans ──────────────────────────────────────────
    @server.tool()
    async def list_test_plans(project_id: str = "", limit: int = 30) -> str:
        """列出測試計畫。"""
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing."
        try:
            limit = max(1, min(100, int(limit)))
        except (TypeError, ValueError):
            limit = 30

        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."
            stmt = select(TestPlan).order_by(desc(TestPlan.updated_at))
            pid = (project_id or "").strip()
            if pid:
                stmt = stmt.where(TestPlan.project_id == pid)
            stmt = _scope_for_user(stmt, TestPlan, user).limit(limit)
            rows = (await db.execute(stmt)).scalars().all()

        if not rows:
            return "(沒有測試計畫)"
        lines = [f"找到 {len(rows)} 份測試計畫:"]
        for p in rows:
            st = getattr(p.status, "value", p.status) or "?"
            lines.append(f"- **{p.code} {p.title}** (id=`{p.id}`, status={st})")
        return "\n".join(lines)

    # ── tool: create_test_plan ─────────────────────────────────────────
    @server.tool()
    async def create_test_plan(
        project_id: str,
        title: str,
        test_strategy_text: str = "",
        scope_in_text: str = "",
    ) -> str:
        """建立測試計畫(基本欄位:範圍 + 策略)。"""
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing."
        pid = (project_id or "").strip()
        title = (title or "").strip()
        if not pid or not title:
            return "Failed: project_id and title are required."

        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."
            project = await _project_in_scope(db, pid, user)
            if project is None:
                return f"Failed: project id={pid} not accessible."
            cnt = (await db.execute(
                select(TestPlan).where(TestPlan.project_id == pid)
            )).all()
            code = f"TP-{len(cnt) + 1:03d}"
            plan = TestPlan(
                project_id=pid, code=code, title=title,
                scope_in_text=(scope_in_text or "").strip() or None,
                test_strategy_text=(test_strategy_text or "").strip() or None,
                status=TestPlanStatus.NEW,
                owner=user.username,
            )
            db.add(plan)
            await db.commit()
            return f"✓ 已建立測試計畫「{code} {title}」。"

    # ── tool: list_todos ───────────────────────────────────────────────
    @server.tool()
    async def list_todos(limit: int = 30, status_filter: str = "") -> str:
        """列出當前使用者組織內的待辦項。可用 status_filter 過濾(例:New / InProgress)。"""
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing."
        try:
            limit = max(1, min(100, int(limit)))
        except (TypeError, ValueError):
            limit = 30

        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."
            stmt = select(TodoItem).order_by(desc(TodoItem.created_at))
            if not user.is_superuser and user.organization_id:
                stmt = stmt.where(TodoItem.organization_id == user.organization_id)
            sf = (status_filter or "").strip()
            if sf:
                st = _enum_or_default(TodoStatus, sf, None)
                if st is not None:
                    stmt = stmt.where(TodoItem.status == st)
            rows = (await db.execute(stmt.limit(limit))).scalars().all()

        if not rows:
            return "(沒有待辦事項)"
        lines = [f"找到 {len(rows)} 筆待辦:"]
        for t in rows:
            st = getattr(t.status, "value", t.status) or "?"
            it = getattr(t.item_type, "value", t.item_type) or "?"
            pri = getattr(t.priority, "value", t.priority) or "?"
            lines.append(f"- [{it}] **{t.title}** (id=`{t.id}`, status={st}, priority={pri})")
        return "\n".join(lines)

    # ── tool: create_todo ──────────────────────────────────────────────
    @server.tool()
    async def create_todo(
        title: str,
        description: str = "",
        item_type: str = "Task",
        priority: str = "P2",
        project_id: str = "",
    ) -> str:
        """建立待辦事項。item_type ∈ Feature/Task/Bug/Spike(預設 Task)、
        priority ∈ P0/P1/P2/P3(預設 P2)、project_id 留空 = 個人 todo。"""
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing."
        title = (title or "").strip()
        if not title:
            return "Failed: title is required."
        if len(title) > 300:
            return f"Failed: title too long ({len(title)} > 300)."

        it_e = _enum_or_default(TodoItemType, item_type, TodoItemType.TASK)
        pri_e = _enum_or_default(TodoPriority, priority, TodoPriority.P2)

        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."
            pid = (project_id or "").strip() or None
            if pid:
                project = await _project_in_scope(db, pid, user)
                if project is None:
                    return f"Failed: project id={pid} not accessible."

            todo = TodoItem(
                organization_id=user.organization_id,
                project_id=pid,
                title=title,
                description=(description or "").strip() or None,
                status=TodoStatus.NEW,
                priority=pri_e,
                item_type=it_e,
            )
            db.add(todo)
            await db.commit()
            return (
                f"✓ 已建立 [{it_e.value}] 待辦「{title}」"
                f"(priority={pri_e.value}{', project='+pid if pid else ''})。"
            )

    # ── tool: list_recordings ──────────────────────────────────────────
    @server.tool()
    async def list_recordings(project_id: str = "", limit: int = 20) -> str:
        """列出錄製 session(WEB / API / APP 各種類型)。"""
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing."
        try:
            limit = max(1, min(50, int(limit)))
        except (TypeError, ValueError):
            limit = 20

        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."
            stmt = select(RecordingSession).order_by(desc(RecordingSession.created_at))
            pid = (project_id or "").strip()
            if pid:
                stmt = stmt.where(RecordingSession.project_id == pid)
            stmt = _scope_for_user(stmt, RecordingSession, user).limit(limit)
            rows = (await db.execute(stmt)).scalars().all()

        if not rows:
            return "(沒有錄製紀錄)"
        lines = [f"找到 {len(rows)} 筆錄製紀錄:"]
        for r in rows:
            url = (r.target_url or "")[:60]
            lines.append(
                f"- **{r.id[:8]}…** status={r.status} target={url}"
            )
        return "\n".join(lines)

    # ── tool: start_recording_session ──────────────────────────────────
    @server.tool()
    async def start_recording_session(project_id: str, target_url: str) -> str:
        """建立一個瀏覽器錄製 session。實際的「開瀏覽器 + 錄」需要使用者在前端
        「錄製」分頁點擊;這裡只做 DB 紀錄並回傳 session_id 與前端深連結。

        Args:
            project_id: 所屬專案 id
            target_url: 要錄的目標 URL(必須是 http(s):// 開頭)
        """
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing."
        pid = (project_id or "").strip()
        url = (target_url or "").strip()
        if not pid or not url:
            return "Failed: project_id and target_url are required."
        if not (url.startswith("http://") or url.startswith("https://")):
            return "Failed: target_url 必須是 http:// 或 https:// 開頭。"

        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."
            project = await _project_in_scope(db, pid, user)
            if project is None:
                return f"Failed: project id={pid} not accessible."
            sid = str(uuid.uuid4())
            session = RecordingSession(
                id=sid, project_id=pid, target_url=url, status="PENDING",
            )
            db.add(session)
            await db.commit()

        return (
            f"✓ 已建立錄製 session(id=`{sid}`,target={url})。\n"
            f"請在前端切到「**錄製**」分頁,挑選此 session 點「啟動容器」即會開啟"
            f"瀏覽器(noVNC 嵌在頁內);完成後點「停止」存腳本即可。"
        )

    # ── tool: convert_recording_to_steps ───────────────────────────────
    @server.tool()
    async def convert_recording_to_steps(recording_id: str) -> str:
        """把錄好的 RecordingSession(Playwright codegen 腳本 / HAR)轉成
        平台的 step 陣列 — 通常接 `create_simple_testcase` 後再人工把這些 step
        貼進測試案例編輯器。
        """
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing."
        rid = (recording_id or "").strip()
        if not rid:
            return "Failed: recording_id is required."

        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."
            session = await db.get(RecordingSession, rid)
            if session is None:
                return f"Failed: recording id={rid} not found."
            project = await _project_in_scope(db, session.project_id, user)
            if project is None:
                return "Failed: recording's project not accessible."
            if not session.script_text:
                return (
                    "Failed: recording 還沒上傳腳本。請先在前端「錄製」分頁完成"
                    "錄製並停止容器,腳本才會存進 DB。"
                )

        # 解析 — 重用既有的轉換 helper(裡面分 HAR vs Playwright Python)
        from app.routers.recordings import _parse_har_to_steps, _parse_script
        text = session.script_text.lstrip()
        if text.startswith("{") and '"log"' in text[:200] and '"entries"' in text[:500]:
            steps = _parse_har_to_steps(session.script_text)
        else:
            steps = _parse_script(session.script_text)
        if not steps:
            return "(腳本解析後沒抽到任何 step,可能格式不認得)"

        lines = [f"從 recording `{rid[:8]}…` 抽到 {len(steps)} 個 step:"]
        for i, s in enumerate(steps[:30], 1):
            action = s.get("action", "?")
            target = s.get("target", "")
            value = s.get("value", "")
            line = f"  {i}. {action}"
            if target:
                line += f" target={target}"
            if value:
                line += f" value={value[:40]}"
            lines.append(line)
        if len(steps) > 30:
            lines.append(f"  …(另有 {len(steps) - 30} 個 step 未列出)")
        lines.append(
            "\n要把這些 step 寫進測試案例:先 `create_simple_testcase(project_id, "
            "scenario_path, name)` 拿到 testcase_id,再請使用者在前端把 step 貼進去"
            "(目前 step JSON 寫入需走前端 UI,MCP 還沒暴露)。"
        )
        return "\n".join(lines)

    # ── tool: execute_testcase ─────────────────────────────────────────
    @server.tool()
    async def execute_testcase(testcase_id: str, ddt_expand: bool = False) -> str:
        """觸發測試案例執行(docker 模式)。回傳 task_id 與 report_id;使用者可
        在前端「測試報告」分頁看進度,或叫 `get_execution_status(task_id)` 查。

        Args:
            testcase_id: testcase 葉節點 id(可由 list_testcases 取得)
            ddt_expand: True = 把 DDT 每筆都跑一次,False = 只跑首筆(預設)
        """
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing."
        tid = (testcase_id or "").strip()
        if not tid:
            return "Failed: testcase_id is required."

        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."
            node = await db.get(TreeNode, tid)
            if node is None or node.level_type != LevelType.TESTCASE:
                return f"Failed: testcase id={tid} not found (or not a leaf testcase)."
            project = await _project_in_scope(db, node.project_id, user)
            if project is None:
                return "Failed: testcase's project not accessible."

            # 重用既有的 collect_execution_plan + create_report 流程
            from app.services.execution_planner import collect_execution_plan
            from app.routers.reports import create_report
            try:
                plan = await collect_execution_plan(db, node_ids=[tid], user=user)
            except Exception as e:  # noqa: BLE001
                return f"Failed: execution planning error — {type(e).__name__}: {e}"
            setup_ids = plan["setup_testcase_ids"]
            main_ids = plan["main_testcase_ids"]
            project_id = plan["project_id"]
            total = len(setup_ids) + len(main_ids)
            task_id = str(uuid.uuid4())
            try:
                report = await create_report(
                    db, project_id, "Manual", total, task_id,
                    execution_mode="docker", source_node_id=tid,
                    source_node_ids=None, ddt_expand=bool(ddt_expand),
                    enable_recording=True,
                )
            except Exception as e:  # noqa: BLE001
                return f"Failed: create_report error — {type(e).__name__}: {e}"

            # 丟 Celery 跑(同 run_execution 後半段)
            try:
                from app.tasks.execution_tasks import run_testcases_task
                run_testcases_task.delay(
                    task_id=task_id, report_id=report.id,
                    setup_ids=setup_ids, main_ids=main_ids,
                    project_id=project_id, ddt_expand=bool(ddt_expand),
                    enable_recording=True, by_username=user.username,
                )
            except Exception as e:  # noqa: BLE001
                return (
                    f"已建立 report(id=`{report.id}`)但 Celery 派發失敗:"
                    f"{type(e).__name__}: {e}。手動觸發或重啟 celery container 試試。"
                )

            await db.commit()
            return (
                f"✓ 已觸發執行 — task_id=`{task_id}`,report_id=`{report.id}`,"
                f"共 {total} 筆 testcase 待跑。可叫 `get_execution_status(\"{task_id}\")` 查進度。"
            )

    # ── tool: get_execution_status ─────────────────────────────────────
    @server.tool()
    async def get_execution_status(task_id: str) -> str:
        """查 execution 進度。回 status / 已 pass / 失敗 / 總數。"""
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing."
        tid = (task_id or "").strip()
        if not tid:
            return "Failed: task_id is required."

        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."
            stmt = select(ExecutionReport).where(ExecutionReport.task_id == tid)
            stmt = _scope_for_user(stmt, ExecutionReport, user)
            report = (await db.execute(stmt)).scalar_one_or_none()
            if report is None:
                return f"Failed: task_id={tid} not found."

        st = getattr(report.status, "value", report.status) or "?"
        return (
            f"task=`{tid[:8]}…` report=`{report.id[:8]}…` status={st}\n"
            f"進度:passed {report.passed_cases} / failed {report.failed_cases} / "
            f"total {report.total_cases}"
            f"{', duration ' + str(report.duration_ms) + 'ms' if report.duration_ms else ''}"
        )

    # ── tool: list_executions ──────────────────────────────────────────
    @server.tool()
    async def list_executions(project_id: str = "", limit: int = 20) -> str:
        """列出近期 execution 報告。可選 project_id 過濾。"""
        username = _current_platform_user.get()
        if not username:
            return "Platform context missing."
        try:
            limit = max(1, min(50, int(limit)))
        except (TypeError, ValueError):
            limit = 20

        async with AsyncSessionLocal() as db:
            user = await _resolve_user(db, username)
            if user is None:
                return f"Failed: user '{username}' not found."
            stmt = select(ExecutionReport).order_by(desc(ExecutionReport.created_at))
            pid = (project_id or "").strip()
            if pid:
                stmt = stmt.where(ExecutionReport.project_id == pid)
            stmt = _scope_for_user(stmt, ExecutionReport, user).limit(limit)
            rows = (await db.execute(stmt)).scalars().all()

        if not rows:
            return "(沒有 execution 紀錄)"
        lines = [f"找到 {len(rows)} 筆 execution:"]
        for r in rows:
            st = getattr(r.status, "value", r.status) or "?"
            lines.append(
                f"- task=`{(r.task_id or '')[:8]}…` report=`{r.id[:8]}…` "
                f"status={st} pass={r.passed_cases}/{r.total_cases} "
                f"({r.created_at.strftime('%Y-%m-%d %H:%M') if r.created_at else 'N/A'})"
            )
        return "\n".join(lines)

    # ── tool: platform_help ────────────────────────────────────────────
    # 用於 LLM 自我探索:把整個 RL 平台的功能 + 對應 tool 列出來。
    # 等於把「平台知識」存進 LLM 可隨時 query 的位置(對應使用者要求 #2)。
    @server.tool()
    async def platform_help(topic: str = "") -> str:
        """列出 RL 平台所有功能與對應 tool。topic 留空 = 全列;指定 topic 會
        只回該主題的詳述(可選 topic:projects / testcases / defects /
        documents / requirements / milestones / versions / plans / todos /
        recordings)。
        """
        topic = (topic or "").strip().lower()
        if not topic:
            return _PLATFORM_CATALOG_FULL
        return _PLATFORM_CATALOG_TOPICS.get(topic, _PLATFORM_CATALOG_FULL)

    return server.streamable_http_app()


# ── 平台功能知識庫(供 platform_help tool 用)─────────────────────────────
# 設計取捨:不寫進 mem0(全 user 共用、永久不變、又夾在 user 個人記憶裡會污染),
# 改放成 module-level 常數,LLM 透過 `platform_help(topic?)` tool 查。
_PLATFORM_CATALOG_TOPICS = {
    "projects": (
        "## 專案 (Projects)\n"
        "- **建立**:`create_project(name, description?)` — 直接建,別問技術棧。\n"
        "- **列出**:`list_projects(limit?)` — 列當前 org 內可看到的專案。\n"
        "- 專案是所有其他實體(testcase / defect / document / ...)的根容器。"
    ),
    "testcases": (
        "## 測試案例 (Test Cases)\n"
        "- 平台用 5 段階層:FEATURE / PLATFORM / PAGE / SCENARIO / TESTCASE。\n"
        "- **建立(快速)**:`create_simple_testcase(project_id, scenario_path, name)` — "
        "scenario_path 用「/」分 4 段,中間階層自動補。\n"
        "- **列出**:`list_testcases(project_id, limit?)`。\n"
        "- 進階編輯(BDD 步驟、ACs、DDT)目前需在前端「測試案例」分頁。"
    ),
    "defects": (
        "## 缺陷 (Defects)\n"
        "- **建立**:`create_defect(project_id, title, description?, severity?, priority?)` — "
        "severity ∈ Critical/Major/Minor/Trivial,priority ∈ P0/P1/P2/P3。\n"
        "- **列出**:`list_defects(project_id?, limit?)`。\n"
        "- **改狀態**:`update_defect_status(defect_id, new_status)` — 7 值 status。"
    ),
    "documents": (
        "## 測試文件 (Documents)\n"
        "- **建立**:`create_document(project_id, title, content_md?, category?)` — "
        "category ∈ Strategy/Guide/Runbook/Checklist/Note/Other。\n"
        "- **列出**:`list_documents(project_id?, limit?)`。\n"
        "- **搜尋**:`search_documents(query, limit?)` — 比對 title/summary/content_md。"
    ),
    "requirements": (
        "## 需求 / RTM (Requirements)\n"
        "- **建立**:`create_requirement(project_id, title, description?, priority?, source?)` — "
        "priority ∈ Must/Should/Could/Wont,source ∈ PRD/Customer/Regulatory/Security/Internal。\n"
        "- **列出**:`list_requirements(project_id?, limit?)`。\n"
        "- 需求 ↔ 測試案例的反向 RTM 鏈目前僅前端可視。"
    ),
    "milestones": (
        "## 測試時程 (Milestones)\n"
        "- **建立**:`create_milestone(project_id, name, start_date, end_date, description?)` — "
        "日期格式 YYYY-MM-DD。\n"
        "- **列出**:`list_milestones(project_id?, limit?)`。"
    ),
    "versions": (
        "## 測試版號 (Test Versions)\n"
        "- **建立**:`create_test_version(project_id, version_label, platform?, description?)` — "
        "platform ∈ WEB/API/APP。\n"
        "- **列出**:`list_test_versions(project_id?, limit?)`。\n"
        "- 缺陷 / 報告 / 測試輪可以反向掛到版號做版本追蹤。"
    ),
    "plans": (
        "## 測試計畫 (Test Plans)\n"
        "- **建立**:`create_test_plan(project_id, title, test_strategy_text?, scope_in_text?)`。\n"
        "- **列出**:`list_test_plans(project_id?, limit?)`。\n"
        "- 進階欄位(風險、entry/exit criteria、approvals)目前需前端編輯。"
    ),
    "todos": (
        "## 待辦事項 (Todos)\n"
        "- **建立**:`create_todo(title, description?, item_type?, priority?, project_id?)` — "
        "item_type ∈ Feature/Task/Bug/Spike,priority ∈ P0/P1/P2/P3。\n"
        "- **列出**:`list_todos(limit?, status_filter?)`。\n"
        "- project_id 留空 = 個人 todo,跨專案。"
    ),
    "recordings": (
        "## 錄製 (Recordings)\n"
        "- **新建 session**:`start_recording_session(project_id, target_url)` — 建 DB row "
        "並回 session_id。**實際的「開瀏覽器錄」需要使用者在前端「錄製」分頁點啟動**,"
        "MCP 沒辦法替使用者操作 noVNC iframe。\n"
        "- **列出**:`list_recordings(project_id?, limit?)`。\n"
        "- **轉 step**:`convert_recording_to_steps(recording_id)` — 把錄好的 Playwright "
        "腳本 / HAR 解析成 step 陣列(便於下一步寫進 testcase)。"
    ),
    "browser": (
        "## 瀏覽器自動化 (Browser via Playwright MCP)\n"
        "你會看到一組 `browser_*` tool — 那是來自 per-user Playwright MCP container,"
        "**真的能操作瀏覽器**(不是模擬)。常用:\n"
        "- `browser_navigate(url)` — 開頁\n"
        "- `browser_snapshot()` — 抓當前 DOM accessibility tree(回 LLM 可讀的結構,"
        "比 screenshot 省 token)\n"
        "- `browser_click(ref)` / `browser_type(ref, text)` — 操作元件\n"
        "- `browser_get_images()` — 真的要 screenshot 才呼叫\n"
        "用法情境:\n"
        "1) 探索目標網站結構並產生測試案例:`browser_navigate` → `browser_snapshot` → "
        "看 DOM 後提案 → 用 `create_simple_testcase` 建好 testcase。\n"
        "2) 驗證測試案例:跟讀 testcase steps 後逐步在瀏覽器重現。\n"
        "**重要**:不要把 browser_* 當成「無限上網工具」— 你只該打開使用者明確指名的"
        "目標 URL 或來自平台 RecordingSession 的 target_url。"
    ),
    "execution": (
        "## 執行 (Execution)\n"
        "- **觸發**:`execute_testcase(testcase_id, ddt_expand?)` — 派 Celery 跑 docker"
        "模式測試,回 task_id。\n"
        "- **查進度**:`get_execution_status(task_id)`。\n"
        "- **歷史**:`list_executions(project_id?, limit?)` — 列近期 report。"
    ),
}
_PLATFORM_CATALOG_FULL = (
    "# RL 自動化測試平台 — 助理可呼叫的功能總覽\n\n"
    + "\n\n".join(_PLATFORM_CATALOG_TOPICS[k] for k in (
        "projects", "testcases", "defects", "documents",
        "requirements", "milestones", "versions", "plans",
        "todos", "recordings", "browser", "execution",
    ))
    + "\n\n"
    "## 用法提示\n"
    "- 不確定該叫哪個 tool 時,先 `platform_help(topic)` 查;再不確定就 "
    "`platform_help()` 看全表。\n"
    "- 缺 project_id 時先 `list_projects()` 找對的 id。\n"
    "- 不要反問使用者要 UUID — 他們不會記。\n"
    "- 整合工作流範例:\n"
    "  * 「替我為登入頁產一個測試案例並執行」→ `browser_navigate` → `browser_snapshot` "
    "→ `create_simple_testcase` → `execute_testcase` → `get_execution_status`。\n"
    "  * 「跑剛才那段錄製」→ `list_recordings` → `convert_recording_to_steps` → "
    "`create_simple_testcase`(把 step 寫進去後)→ `execute_testcase`。"
)


async def _resolve_user(db: AsyncSession, username: str) -> Optional[User]:
    return (
        await db.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()


def _enum_or_default(enum_cls, value: str, default):
    """字串轉 enum,認不出來回 default。LLM 可能不嚴格遵守 enum,我們做容錯。"""
    if not value:
        return default
    v = str(value).strip()
    # 直接 match value(case-sensitive,因為 DefectStatus 是 PascalCase 的)
    for m in enum_cls:
        if m.value == v or m.name == v.upper() or m.value.lower() == v.lower():
            return m
    return default


def _scope_for_user(stmt, model, user: User):
    """跨 org/project 的 scope filter — 對應 auth.scope.scope_by_project 的精簡版。

    superuser 直通;其他人限定 user.organization_id 內的 project + 必須是 active member。
    寫在這裡而非沿用 scope_by_project 是因為這個 module 在 /platform-mcp/* 路徑跑,
    不走主 app 的 AuthMiddleware,沿用 require_permission 等 dependency 不適用 — 直接
    用 SQL JOIN 強制就好,同樣的安全保證。
    """
    from sqlalchemy import and_

    if user.is_superuser:
        return stmt
    project_fk = getattr(model, "project_id")
    return (
        stmt.join(Project, project_fk == Project.id)
        .join(
            ProjectMember,
            and_(
                ProjectMember.project_id == Project.id,
                ProjectMember.username == user.username,
                ProjectMember.status == "active",
            ),
        )
        .where(Project.organization_id == user.organization_id)
    )


async def _project_in_scope(db: AsyncSession, project_id: str, user: User) -> Optional[Project]:
    """確認 project_id 存在且 user 看得到 — 找不到回 None,呼叫端決定要怎麼回 LLM。"""
    project = await db.get(Project, project_id)
    if project is None:
        return None
    if user.is_superuser:
        return project
    if project.organization_id != user.organization_id:
        return None
    # ProjectMember 檢查(對齊 scope_by_project 的 active member 限制)
    pm = (await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.username == user.username,
            ProjectMember.status == "active",
        )
    )).scalar_one_or_none()
    if pm is None:
        return None
    return project


# ── 對外:讓 main.py mount + middleware 配置 ───────────────────────────────
def mount_platform_mcp(app) -> None:
    """把 platform-mcp 掛到主 FastAPI app 上(/platform-mcp/mcp)。"""
    try:
        sub_app = _build_mcp_app()
    except Exception as e:  # noqa: BLE001
        LOG.exception("Platform MCP server failed to initialize — feature disabled: %s", e)
        return
    app.mount("/platform-mcp", sub_app)
    app.middleware("http")(_platform_mcp_auth)
    LOG.info("Platform MCP mounted at /platform-mcp/mcp")
