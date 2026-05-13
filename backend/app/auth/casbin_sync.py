"""Casbin policy sync layer。

從 DB 的 ``Role`` / ``OrgMembership`` / ``ProjectMember`` / ``Project`` 拉出
3 階層角色解析(``ProjectMember > OrgMembership > User.role_id``),flatten
成 Casbin policy:

* ``p, <RoleName>, <dom>, <obj>, <act>``  ← 由 ``Role.permissions_json`` 翻
* ``g, <username>, <RoleName>, <dom>``    ← 由 OrgMembership / ProjectMember 翻

為什麼要 flatten 而不是仰賴 Casbin 內建的 RBAC inheritance:Casbin 的 domain
RBAC 不能表達「project 角色 override org 角色」這種有方向性的 fallback —
若要兩層繼承 Casbin 需要另外的 group definition 配合 keyMatch matcher,可讀性
+ 維護成本都高於這裡的 Python 計算。把計算結果(已決勝)寫進 casbin_rule 表,
enforce 仍然是純 in-memory O(policy) lookup,速度跟單層 RBAC 一樣。

兩個 entrypoint:

* :func:`rebuild_user_grants` — 單一使用者,寫成本低,當 OrgMembership /
  ProjectMember mutate 時呼叫(Phase 4.1 refactor 那 44 個 router 時順手補)。
* :func:`rebuild_all_policies` — 整表 truncate-and-rewrite,給 Phase 3.2
  initial seed + Phase 6 periodic reconcile 用。
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import casbin as _casbin
from app.auth.permissions_catalog import permission_to_casbin
from app.models.org_membership import OrgMembership
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.role import Role
from app.models.user import User

logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────


def _role_to_policies(role: Role, dom: str) -> list[list[str]]:
    """Role.permissions_json → 一組 [sub, dom, obj, act] policy lines。"""
    out: list[list[str]] = []
    for perm in role.permissions_json or []:
        try:
            obj, act = permission_to_casbin(perm)
        except KeyError:
            logger.warning(
                "casbin_sync: role '%s' has unknown permission '%s' — skipping",
                role.name, perm,
            )
            continue
        out.append([role.name, dom, obj, act])
    return out


async def _load_role_map(db: AsyncSession) -> dict[str, Role]:
    rows = (await db.execute(select(Role))).scalars().all()
    return {r.id: r for r in rows}


# ── Public API ─────────────────────────────────────────────────────────


async def rebuild_user_grants(db: AsyncSession, username: str) -> None:
    """重寫單一使用者的所有 grouping(``g``) policies。

    步驟:

    1. 刪掉該 user 名下所有 ``g, <username>, *, *`` 的 row。
    2. 從 OrgMembership 拉「user 在哪些 org / 用哪個 role」→ 寫 ``g`` rules 到
       ``org:<oid>`` domain。
    3. 從 ProjectMember 拉「user 在哪些 project / override 的 role」→ 寫
       ``g`` rules 到 ``project:<pid>`` domain。
       - ProjectMember.role_id 為 NULL 表示「沿用 OrgMembership 的 role」,
         此時依規格要把 OrgMembership 對應 role 也寫一份到 project domain
         (否則該 project 完全沒 g rule,Casbin 會 deny)。
       - ProjectMember.role_id 非 NULL → 用該 project role,**覆蓋**任何
         同 user 在該 project domain 的舊規則(已在步驟 1 清光,不會重複)。
    4. 不處理 ``User.role_id``(global default role):這個目前等同 OrgMembership
       的 role,在 ``_seed_default_roles`` 流程裡會被同步進 OrgMembership。
       若以後要拆出來,在這加一條 global domain 的 g rule 即可。
    5. ``enforcer.save_policy()`` 寫回 DB。

    Casbin 沒啟用 → no-op。Caller 都會在 ``CASBIN_ENABLED`` 切 True 之後才
    間接觸發到這條(透過 require_casbin 失敗 → admin 跑修復 / 透過 mutate
    endpoint 顯式呼叫),所以 fail-open 沒副作用。
    """
    enf = _casbin.get_enforcer()
    if enf is None:
        return

    role_map = await _load_role_map(db)

    # 1. 清掉舊的 g rules
    enf.remove_filtered_grouping_policy(0, username)

    # 2. OrgMembership → g 在 org:<oid> domain
    orgmems = (
        await db.execute(
            select(OrgMembership)
            .where(OrgMembership.username == username)
            .where(OrgMembership.status == "active")
        )
    ).scalars().all()
    org_role_id_by_org: dict[str, Optional[str]] = {}
    for om in orgmems:
        org_role_id_by_org[om.organization_id] = om.role_id
        if om.role_id and om.role_id in role_map:
            enf.add_grouping_policy(
                username, role_map[om.role_id].name, _casbin.org_domain(om.organization_id),
            )

    # 3. ProjectMember → g 在 project:<pid> domain
    # Project.organization_id 是用來退回 OrgMembership.role_id 的依據。
    projmems = (
        await db.execute(
            select(ProjectMember, Project)
            .join(Project, Project.id == ProjectMember.project_id)
            .where(ProjectMember.username == username)
            .where(ProjectMember.status == "active")
        )
    ).all()
    for pm, project in projmems:
        effective_role_id = pm.role_id or org_role_id_by_org.get(project.organization_id)
        if effective_role_id and effective_role_id in role_map:
            enf.add_grouping_policy(
                username, role_map[effective_role_id].name, _casbin.project_domain(project.id),
            )

    enf.save_policy()


async def rebuild_all_policies(db: AsyncSession) -> dict[str, int]:
    """全表 truncate-and-rewrite。給 Phase 3.2 initial seed +
    Phase 6 periodic reconcile job 用。

    回傳 counter dict 方便 admin 看 diff 大小:``{"p": N, "g": M}``。
    """
    enf = _casbin.get_enforcer()
    if enf is None:
        return {"p": 0, "g": 0}

    # 一刀清空 — Casbin 沒提供 wipe API,逐 row 刪;表本身在大型部署也才
    # ``#users × #projects``,清光 + 重寫總成本仍是秒級。
    enf.clear_policy()

    # ── p rules ────────────────────────────────────────────────────────
    role_map = await _load_role_map(db)
    p_lines: list[list[str]] = []

    # org-scoped role:寫一份 wildcard domain ``org:*``,跟 keyMatch2 配合
    # 後等於「在任何 org 內都生效」;個別 org 不需要再展開。
    # project-scoped role:寫 ``project:*``,跟前面同邏輯。
    for role in role_map.values():
        if role.scope == "org":
            dom = "org:*"
        elif role.scope == "project":
            dom = "project:*"
        else:
            dom = "global"
        p_lines.extend(_role_to_policies(role, dom))

    if p_lines:
        enf.add_policies(p_lines)

    # ── g rules ────────────────────────────────────────────────────────
    # 全部 user 一次寫;rebuild_user_grants 內部會 save_policy,我們改成
    # 直接組好 list 一次 add_grouping_policies 比較省 round-trip。
    g_lines: list[list[str]] = []
    orgmems = (
        await db.execute(
            select(OrgMembership).where(OrgMembership.status == "active")
        )
    ).scalars().all()

    # username → org_id → role_id(給 ProjectMember role_id=NULL 用)
    user_org_role: dict[tuple[str, str], Optional[str]] = {}
    for om in orgmems:
        user_org_role[(om.username, om.organization_id)] = om.role_id
        if om.role_id and om.role_id in role_map:
            g_lines.append([
                om.username, role_map[om.role_id].name, _casbin.org_domain(om.organization_id),
            ])

    projmems = (
        await db.execute(
            select(ProjectMember, Project)
            .join(Project, Project.id == ProjectMember.project_id)
            .where(ProjectMember.status == "active")
        )
    ).all()
    for pm, project in projmems:
        effective_role_id = pm.role_id or user_org_role.get((pm.username, project.organization_id))
        if effective_role_id and effective_role_id in role_map:
            g_lines.append([
                pm.username, role_map[effective_role_id].name, _casbin.project_domain(project.id),
            ])

    if g_lines:
        enf.add_grouping_policies(g_lines)

    enf.save_policy()
    logger.info(
        "casbin_sync.rebuild_all_policies: %d p / %d g rules written",
        len(p_lines), len(g_lines),
    )
    return {"p": len(p_lines), "g": len(g_lines)}


# ── Mutation hooks ─────────────────────────────────────────────────────
#
# 提供給 router caller 用的 fire-and-forget helper:Phase 4.1 refactor 那 44
# 個 site 時,每個會動到 OrgMembership / ProjectMember / Role 的 write path
# 都在最後呼叫 ``schedule_user_resync(username)``,讓 enforcer 自動跟上。
#
# 為什麼是 schedule(背景跑)而不是同步:
#   * rebuild_user_grants 內部會打 DB(同個 session 已 flush),用 to_thread
#     跑 Casbin 的 save_policy 不會跟主 request 競爭。
#   * 失敗只 log,不擋住主流程 — Casbin 暫時不一致 < 5 min reconcile 拉回。

def schedule_user_resync(username: str) -> None:
    """fire-and-forget 觸發單一使用者的 grant 重建。

    用法:``schedule_user_resync(user.username)`` — 不需要 await。
    """
    import asyncio
    from app.database import AsyncSessionLocal

    async def _runner():
        try:
            async with AsyncSessionLocal() as session:
                await rebuild_user_grants(session, username)
        except Exception:
            logger.exception("schedule_user_resync(%s) failed", username)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_runner())
    except RuntimeError:
        # 沒有 running loop(例如 CLI / migration script 環境)→ 同步阻塞跑一次
        asyncio.run(_runner())
