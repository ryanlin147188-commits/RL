"""Casdoor → local DB sync service。

Phase 6 of the Casdoor + Casbin migration plan。webhook handler 與 5 分鐘
periodic reconcile job 都呼叫這裡的函式,把 Casdoor 端的 user / role 狀態同步
到本地:

* ``users`` 表的 ``casdoor_user_id`` / ``display_name`` / ``email`` /
  ``is_active`` / ``is_superuser`` / ``role_id``
* ``Role.permissions_json``(fallback / shadow 用,Casdoor 不直接管 permissions
  本身,只管「user → role 的 grouping」)
* Casbin grouping(``g``)policies — 透過 :func:`app.auth.casbin_sync.rebuild_user_grants`

不重新發明輪子的部分:Casbin policy 寫入仍走 ``casbin_sync.rebuild_all_policies``
(整表 truncate-and-rewrite),這裡只負責「決定要呼叫誰」+ 紀錄 audit log。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import casdoor as _casdoor
from app.auth.security import hash_password
from app.models.audit_log import AuditLog
from app.models.org_membership import OrgMembership
from app.models.role import Role
from app.models.user import User

logger = logging.getLogger(__name__)


# ── Casdoor REST helpers ───────────────────────────────────────────────


def _client() -> httpx.AsyncClient:
    """以 Casdoor app 的 client_id/secret 走 Basic auth。"""
    if not _casdoor.CASDOOR_CLIENT_ID or not _casdoor.CASDOOR_CLIENT_SECRET:
        raise RuntimeError(
            "CASDOOR_CLIENT_ID / CASDOOR_CLIENT_SECRET 未設;無法同步"
        )
    return httpx.AsyncClient(
        base_url=_casdoor.CASDOOR_ENDPOINT,
        auth=(_casdoor.CASDOOR_CLIENT_ID, _casdoor.CASDOOR_CLIENT_SECRET),
        timeout=15.0,
        headers={"Accept": "application/json"},
    )


async def fetch_users() -> list[dict[str, Any]]:
    """GET ``/api/get-users?owner=<org>`` — 回 list of Casdoor user dicts。"""
    async with _client() as c:
        r = await c.get(f"/api/get-users?owner={_casdoor.CASDOOR_ORG}")
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "ok":
            raise RuntimeError(f"Casdoor get-users 失敗:{body.get('msg')}")
        return body.get("data") or []


async def fetch_roles() -> list[dict[str, Any]]:
    """GET ``/api/get-roles?owner=<org>``。Casdoor role 物件包含 ``users`` 陣列
    (字串格式 ``<org>/<username>``),Phase 6.2 用它組 OrgMembership rows。"""
    async with _client() as c:
        r = await c.get(f"/api/get-roles?owner={_casdoor.CASDOOR_ORG}")
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "ok":
            raise RuntimeError(f"Casdoor get-roles 失敗:{body.get('msg')}")
        return body.get("data") or []


# ── Diff + apply ───────────────────────────────────────────────────────


def _parse_casdoor_user_ref(s: str) -> Optional[str]:
    """``"autotest/alice"`` → ``"alice"``;格式不對 → None。"""
    if not isinstance(s, str) or "/" not in s:
        return None
    org, _, name = s.partition("/")
    if org != _casdoor.CASDOOR_ORG or not name:
        return None
    return name


async def apply_user(db: AsyncSession, cu: dict[str, Any]) -> dict[str, Any]:
    """upsert 單筆 Casdoor user 到本地 ``users`` 表。回傳 diff summary 給
    audit log 用。``isAdmin`` / ``isGlobalAdmin`` 在 Casdoor 是「該 organization
    內的管理者」,我們把它對應到 ``is_superuser``(本地語意 = 平台管理員)。
    """
    name = (cu.get("name") or "").strip()
    if not name:
        return {"skipped": True, "reason": "missing name"}

    sub = cu.get("id") or name  # Casdoor 主鍵
    email = (cu.get("email") or "").strip().lower() or None
    display = (cu.get("displayName") or cu.get("name") or "").strip() or None
    is_admin = bool(cu.get("isAdmin")) or bool(cu.get("isGlobalAdmin"))
    is_active = not bool(cu.get("isForbidden"))

    diff: dict[str, Any] = {"username": name, "casdoor_user_id": sub}

    # 先用 casdoor_user_id 找;對不到才退回 username
    user = (
        await db.execute(select(User).where(User.casdoor_user_id == sub))
    ).scalar_one_or_none()
    if user is None:
        user = (
            await db.execute(select(User).where(User.username == name))
        ).scalar_one_or_none()

    if user is None:
        # 新建:給隨機 password_hash(SSO-only)
        import secrets

        user = User(
            username=name,
            display_name=display or name,
            email=email,
            password_hash=hash_password(secrets.token_urlsafe(32)),
            casdoor_user_id=sub,
            is_active=is_active,
            is_superuser=is_admin,
        )
        db.add(user)
        await db.flush()
        diff["created"] = True
        return diff

    # 既有 user — 比對差異
    if user.casdoor_user_id != sub:
        diff["casdoor_user_id_changed"] = {"from": user.casdoor_user_id, "to": sub}
        user.casdoor_user_id = sub
    if display and user.display_name != display:
        diff["display_name_changed"] = {"from": user.display_name, "to": display}
        user.display_name = display
    if email and user.email != email:
        diff["email_changed"] = {"from": user.email, "to": email}
        user.email = email
    if user.is_active != is_active:
        diff["is_active_changed"] = {"from": user.is_active, "to": is_active}
        user.is_active = is_active
    if user.is_superuser != is_admin:
        diff["is_superuser_changed"] = {"from": user.is_superuser, "to": is_admin}
        user.is_superuser = is_admin
    await db.flush()
    return diff


async def apply_role(db: AsyncSession, cr: dict[str, Any]) -> dict[str, Any]:
    """同步 Casdoor role:
    * Casdoor role.name → 本地 ``roles.name``(同 org 內 unique)
    * Casdoor role.users(``[<org>/<username>, ...]``)→ ``OrgMembership.role_id``
    permissions 不從 Casdoor 拉(Casdoor role 結構沒這欄);沿用本地 seeded
    ``Role.permissions_json`` 作為 source of truth。
    """
    role_name = (cr.get("name") or "").strip()
    if not role_name:
        return {"skipped": True, "reason": "missing role name"}

    diff: dict[str, Any] = {"role_name": role_name}

    role = (
        await db.execute(select(Role).where(Role.name == role_name))
    ).scalar_one_or_none()
    if role is None:
        # 新角色 — 沒對應的 permission seed 就建空 list(operator 再補)
        role = Role(
            name=role_name,
            description=cr.get("displayName") or role_name,
            permissions_json=[],
            is_system=False,
            scope="org",
        )
        db.add(role)
        await db.flush()
        diff["role_created"] = True

    # Casdoor 端目前持有此 role 的 username 集合
    casdoor_usernames: set[str] = set()
    for ref in cr.get("users") or []:
        u = _parse_casdoor_user_ref(ref)
        if u:
            casdoor_usernames.add(u)

    # 本地端目前 OrgMembership.role_id == role.id 的 username 集合
    local_rows = (
        await db.execute(
            select(OrgMembership).where(OrgMembership.role_id == role.id)
        )
    ).scalars().all()
    local_usernames: set[str] = {om.username for om in local_rows}

    added = casdoor_usernames - local_usernames
    removed = local_usernames - casdoor_usernames

    if not added and not removed:
        return diff

    diff["members_added"] = sorted(added)
    diff["members_removed"] = sorted(removed)

    # 新增的:把該 user 在 default org 的 OrgMembership.role_id 設成 role.id;
    # 若沒 OrgMembership 就建一筆。
    if added:
        users = (
            await db.execute(select(User).where(User.username.in_(added)))
        ).scalars().all()
        users_by_name = {u.username: u for u in users}
        for uname in added:
            u = users_by_name.get(uname)
            if not u:
                continue
            om = (
                await db.execute(
                    select(OrgMembership)
                    .where(OrgMembership.username == uname)
                    .where(OrgMembership.is_default.is_(True))
                )
            ).scalar_one_or_none()
            if om is None and u.organization_id:
                om = OrgMembership(
                    username=uname,
                    organization_id=u.organization_id,
                    role_id=role.id,
                    is_default=True,
                    status="active",
                )
                db.add(om)
            elif om is not None:
                om.role_id = role.id
        await db.flush()

    # 移除的:該 user 的 default OrgMembership.role_id 設回 None。
    if removed:
        for uname in removed:
            om = (
                await db.execute(
                    select(OrgMembership)
                    .where(OrgMembership.username == uname)
                    .where(OrgMembership.is_default.is_(True))
                )
            ).scalar_one_or_none()
            if om is not None and om.role_id == role.id:
                om.role_id = None
        await db.flush()

    return diff


# ── Public entry points(webhook + beat) ────────────────────────────────


async def reconcile_all(db: AsyncSession) -> dict[str, Any]:
    """整批同步:fetch users + roles → apply → rebuild casbin policies。

    回傳 summary,給 audit log + Celery task result 用。
    """
    users_payload = await fetch_users()
    roles_payload = await fetch_roles()

    user_diffs: list[dict[str, Any]] = []
    for cu in users_payload:
        try:
            d = await apply_user(db, cu)
            if d.get("created") or any(k.endswith("_changed") for k in d):
                user_diffs.append(d)
        except Exception:
            logger.exception("apply_user failed for %s", cu.get("name"))

    role_diffs: list[dict[str, Any]] = []
    for cr in roles_payload:
        try:
            d = await apply_role(db, cr)
            if d.get("role_created") or d.get("members_added") or d.get("members_removed"):
                role_diffs.append(d)
        except Exception:
            logger.exception("apply_role failed for %s", cr.get("name"))

    await db.commit()

    # 任何 role / user mutation 都重灌 Casbin policy(便宜:in-memory + 一次
    # save_policy 寫 DB)。整表 truncate-and-rewrite 在小資料量下成本可忽略。
    from app.auth.casbin_sync import rebuild_all_policies

    casbin_counts = await rebuild_all_policies(db)

    summary = {
        "fetched_users": len(users_payload),
        "fetched_roles": len(roles_payload),
        "user_diffs": user_diffs,
        "role_diffs": role_diffs,
        "casbin": casbin_counts,
        "at": datetime.utcnow().isoformat(),
    }

    # Phase 6.3 audit:寫一筆 AuditLog 紀錄這次同步動了什麼。
    if user_diffs or role_diffs:
        db.add(AuditLog(
            username="<casdoor-sync>",
            method="SYNC",
            path="/internal/casdoor/reconcile",
            entity_type="casdoor_sync",
            status_code=200,
            duration_ms=0,
            change_summary=summary,
        ))
        await db.commit()

    return summary


async def apply_single_user_event(
    db: AsyncSession, action: str, user_obj: dict[str, Any],
) -> dict[str, Any]:
    """webhook ``add-user`` / ``update-user`` / ``delete-user`` 走這條 fast-path。

    ``delete-user``:把 ``users.is_active`` 設 False(不真刪,保留外鍵)。
    其他:upsert 該筆 user 後重建該 user 的 Casbin grants(便宜,只改一個 user)。
    """
    from app.auth.casbin_sync import rebuild_user_grants

    name = (user_obj.get("name") or "").strip()
    if not name:
        return {"skipped": True}

    if action == "delete-user":
        user = (
            await db.execute(select(User).where(User.username == name))
        ).scalar_one_or_none()
        if user is not None and user.is_active:
            user.is_active = False
            await db.commit()
            await rebuild_user_grants(db, name)
            return {"deactivated": name}
        return {"already_inactive": name}

    diff = await apply_user(db, user_obj)
    await db.commit()
    await rebuild_user_grants(db, name)

    db.add(AuditLog(
        username="<casdoor-webhook>",
        method="WEBHOOK",
        path=f"/internal/casdoor/{action}",
        entity_type="casdoor_user",
        entity_id=name,
        status_code=200,
        duration_ms=0,
        change_summary=diff,
    ))
    await db.commit()
    return diff
