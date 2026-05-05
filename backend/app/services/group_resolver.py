"""Recursive group member resolution(D-2).

Why:
    群組可以巢狀(parent_id self-FK)。當有任何系統把群組當作通知收件人 / 指派
    對象時,需要把這個群組「展開」成扁平的 username 集合 — 含巢狀子群組,
    去重。

    這份邏輯原本只活在 routers/todos.py 裡,只有 TodoItem assignment 用得到。
    Tier D 把 generic /api/assignments 也支援群組 fan-out(原本 routers/
    assignments.py:128 標註「v1.2 deferred」),所以兩個 caller 共用同一份。

API:
    resolve_group_members(db, group_id) -> set[str]
        BFS 遍歷;cycle-safe(visited set);永遠回 set,不會回 None。
        group_id 不存在時回空 set(由 caller 自己決定要不要 404)。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.group import Group, GroupMembership


async def resolve_group_members(db: AsyncSession, group_id: str) -> set[str]:
    """遞迴展開 group_id 下所有(含巢狀子群組)成員的 username 集合。"""
    visited: set[str] = set()
    result: set[str] = set()
    queue = [group_id]
    while queue:
        gid = queue.pop()
        if gid in visited:
            continue
        visited.add(gid)
        rows = (
            await db.execute(
                select(GroupMembership.username).where(GroupMembership.group_id == gid)
            )
        ).scalars().all()
        result.update(rows)
        children = (
            await db.execute(
                select(Group.id).where(Group.parent_id == gid)
            )
        ).scalars().all()
        queue.extend(children)
    return result
