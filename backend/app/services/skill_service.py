"""Skill CRUD + markdown frontmatter import — Phase 2a。

僅做 DB / 純函式;set-active 由 agent_service 處理(需 touch session)。

markdown import 採「Claude Code SKILL.md」格式:YAML frontmatter + body。
frontmatter 內欄位映射:
    name        → skills.name
    description → skills.description
    triggers    → skills.trigger_keywords(也支援 "trigger_keywords")
    allowed_tools / tools → skills.allowed_tools
    mode_scope  → skills.mode_scope
body 為 system_prompt_addition。
"""
from __future__ import annotations

import re
import uuid
from typing import Any, Optional

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.skill import Skill


class SkillError(Exception):
    """Router 轉 HTTPException 用。"""


class SkillNotFound(SkillError):
    pass


class SkillNameConflict(SkillError):
    pass


class SkillMarkdownInvalid(SkillError):
    pass


# ── CRUD ──────────────────────────────────────────────────────────────


async def list_skills(
    db: AsyncSession,
    *,
    organization_id: str,
    mode: Optional[str] = None,
    enabled_only: bool = True,
) -> list[Skill]:
    """列 org 的 skills。``mode`` 給 chat picker 過濾用 — 命中 mode_scope
    或 mode_scope 為空(= 不限)的都回。"""
    stmt = select(Skill).where(Skill.organization_id == organization_id)
    if enabled_only:
        stmt = stmt.where(Skill.enabled == True)  # noqa: E712
    stmt = stmt.order_by(Skill.name)
    rows = (await db.execute(stmt)).scalars().all()
    if mode is None:
        return list(rows)
    out: list[Skill] = []
    for s in rows:
        scope = s.mode_scope or []
        if not scope or mode in scope:
            out.append(s)
    return out


async def get_skill(
    db: AsyncSession,
    *,
    skill_id: str,
    organization_id: str,
) -> Skill:
    """取一個 skill;非該 org 視為 not found(避免 enum)。"""
    stmt = select(Skill).where(
        Skill.id == skill_id, Skill.organization_id == organization_id
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise SkillNotFound(f"skill {skill_id} not found")
    return row


async def get_skill_by_name(
    db: AsyncSession,
    *,
    name: str,
    organization_id: str,
) -> Optional[Skill]:
    """供 /skill-name 前綴 parsing 用。"""
    stmt = select(Skill).where(
        Skill.organization_id == organization_id, Skill.name == name
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def create_skill(
    db: AsyncSession,
    *,
    organization_id: str,
    created_by: Optional[str],
    name: str,
    description: str = "",
    system_prompt_addition: str = "",
    trigger_keywords: Optional[list[str]] = None,
    allowed_tools: Optional[list[str]] = None,
    mode_scope: Optional[list[str]] = None,
    enabled: bool = True,
) -> Skill:
    name = name.strip()
    if not name:
        raise SkillError("name 不可為空")
    if len(name) > 64:
        raise SkillError("name 長度超過 64 字元")
    # per-org unique check(DB 也有 UNIQUE constraint;這層先做給更好的錯誤訊息)
    dup = await get_skill_by_name(db, name=name, organization_id=organization_id)
    if dup is not None:
        raise SkillNameConflict(f"skill name '{name}' 已存在")
    row = Skill(
        id=str(uuid.uuid4()),
        organization_id=organization_id,
        created_by=created_by,
        name=name,
        description=description or "",
        trigger_keywords=list(trigger_keywords or []),
        system_prompt_addition=system_prompt_addition or "",
        allowed_tools=list(allowed_tools) if allowed_tools else None,
        mode_scope=list(mode_scope or []),
        enabled=enabled,
        version=1,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def update_skill(
    db: AsyncSession,
    *,
    skill_id: str,
    organization_id: str,
    payload: dict[str, Any],
) -> Skill:
    row = await get_skill(
        db, skill_id=skill_id, organization_id=organization_id
    )
    bumped = False
    if "name" in payload:
        new_name = (payload["name"] or "").strip()
        if not new_name:
            raise SkillError("name 不可為空")
        if new_name != row.name:
            dup = await get_skill_by_name(
                db, name=new_name, organization_id=organization_id
            )
            if dup is not None:
                raise SkillNameConflict(f"skill name '{new_name}' 已存在")
            row.name = new_name
            bumped = True
    for field in (
        "description",
        "system_prompt_addition",
    ):
        if field in payload:
            setattr(row, field, payload[field] or "")
            bumped = True
    for field in ("trigger_keywords", "mode_scope"):
        if field in payload:
            setattr(row, field, list(payload[field] or []))
            bumped = True
    if "allowed_tools" in payload:
        value = payload["allowed_tools"]
        row.allowed_tools = list(value) if value else None
        bumped = True
    if "enabled" in payload:
        row.enabled = bool(payload["enabled"])
        bumped = True
    if bumped:
        row.version += 1
    await db.flush()
    await db.refresh(row)
    return row


async def delete_skill(
    db: AsyncSession,
    *,
    skill_id: str,
    organization_id: str,
) -> None:
    row = await get_skill(
        db, skill_id=skill_id, organization_id=organization_id
    )
    await db.delete(row)
    await db.flush()


# ── Markdown frontmatter import ──────────────────────────────────────

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<fm>.*?)\n---\s*\n?(?P<body>.*)\Z", re.DOTALL
)


def parse_skill_markdown(content: str) -> dict[str, Any]:
    """Parse ``.claude/skills/*.md`` 風格的 frontmatter + body。

    回傳 dict 可直接餵給 ``create_skill`` / ``update_skill``。
    不寫 DB,純函式。
    """
    if not content or not content.strip():
        raise SkillMarkdownInvalid("檔案內容為空")
    m = _FRONTMATTER_RE.match(content.lstrip("﻿"))
    if m is None:
        raise SkillMarkdownInvalid(
            "找不到 YAML frontmatter(需以 --- 開頭與結尾)"
        )
    try:
        fm = yaml.safe_load(m.group("fm")) or {}
    except yaml.YAMLError as exc:
        raise SkillMarkdownInvalid(f"YAML frontmatter 解析失敗: {exc}") from exc
    if not isinstance(fm, dict):
        raise SkillMarkdownInvalid("frontmatter 不是 mapping 結構")
    name = (fm.get("name") or "").strip()
    if not name:
        raise SkillMarkdownInvalid("frontmatter 缺 name")
    # 容忍兩種命名(Claude Code 的 SKILL.md 多用 description,我們也支援)
    triggers = fm.get("triggers") or fm.get("trigger_keywords") or []
    allowed = fm.get("allowed_tools") or fm.get("tools") or None
    mode_scope = fm.get("mode_scope") or []
    body = (m.group("body") or "").strip()
    return {
        "name": name,
        "description": (fm.get("description") or "").strip(),
        "trigger_keywords": list(triggers) if triggers else [],
        "allowed_tools": list(allowed) if allowed else None,
        "mode_scope": list(mode_scope) if mode_scope else [],
        "system_prompt_addition": body,
    }


async def import_from_markdown(
    db: AsyncSession,
    *,
    organization_id: str,
    created_by: Optional[str],
    content: str,
    overwrite: bool = False,
) -> Skill:
    """Parse markdown frontmatter 後 create / update(由 overwrite 控制)。"""
    payload = parse_skill_markdown(content)
    existing = await get_skill_by_name(
        db, name=payload["name"], organization_id=organization_id
    )
    if existing is not None:
        if not overwrite:
            raise SkillNameConflict(
                f"skill '{payload['name']}' 已存在(overwrite=true 可覆寫)"
            )
        return await update_skill(
            db,
            skill_id=existing.id,
            organization_id=organization_id,
            payload=payload,
        )
    return await create_skill(
        db,
        organization_id=organization_id,
        created_by=created_by,
        **payload,
    )


# ── Active skill helpers(給 agent_service 用) ───────────────────────


def tool_name_matches_allowed(
    tool_name: str, allowed_globs: Optional[list[str]]
) -> bool:
    """檢查 tool name 是否在 skill.allowed_tools 白名單內。

    None / 空 list = 不限縮回 True。glob 支援 ``*`` 通配(fnmatch 風格)。
    """
    if not allowed_globs:
        return True
    import fnmatch

    for pat in allowed_globs:
        if fnmatch.fnmatchcase(tool_name, pat):
            return True
    return False


def render_skill_prompt_section(skill: Skill) -> str:
    """組成 append 到 base prompt 後面的 skill section。

    用清晰的 markdown header 邊界,避免 LLM 跟 base prompt 內容混淆。
    """
    body = (skill.system_prompt_addition or "").strip()
    if not body:
        return ""
    return f"\n\n## Active Skill: {skill.name}\n\n{body}\n"
