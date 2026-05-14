"""RBAC ``require_permission`` FastAPI dependency.

Checks the calling user's :class:`Role` for every permission key listed at
the call site, returning 403 if any are missing. Superusers bypass.

Usage::

    from fastapi import Depends
    from app.auth.permissions import require_permission
    from app.auth.permissions_catalog import P

    @router.post(
        "/defects",
        dependencies=[Depends(require_permission(P.DEFECT_WRITE))],
    )
    async def create_defect(...):
        ...

    @router.delete(
        "/projects/{pid}",
        dependencies=[Depends(require_permission(P.PROJECT_WRITE, P.PROJECT_DELETE))],
    )
    async def delete_project(...):
        ...

The dependency does not return the user (uses ``dependencies=[]``); use
``get_current_user`` separately when the handler needs the user object.
"""
from __future__ import annotations

import logging
from typing import Callable

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import casbin as _casbin
from app.auth.context import current_org_id
from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.role import Role
from app.models.user import User

# Shadow-mode 專屬 logger — 預設與 root 同 level,但操作者可在 logging
# config 把 ``app.auth.permissions.shadow`` 設成 DEBUG / 抑制 INFO,單獨控制
# divergence log 的量級。
_shadow_logger = logging.getLogger("app.auth.permissions.shadow")


def require_permission(*needed: str) -> Callable:
    """Build a FastAPI dependency that asserts the current user holds every
    permission in ``needed``.

    Raises:
        HTTPException 403: missing one or more permissions; the response body
            ``detail`` lists the missing keys so the frontend can render a
            specific "you need X to do this" message rather than a vague 403.
    """
    if not needed:
        raise ValueError("require_permission() requires at least one permission key")

    async def _check(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> User:
        if user.is_superuser:
            return user

        granted: set[str] = set()
        if user.role_id is not None:
            role = await db.get(Role, user.role_id)
            if role is not None and role.permissions_json:
                granted = set(role.permissions_json)

        missing = [p for p in needed if p not in granted]
        legacy_allow = not missing

        # ── Phase 3.1 shadow-mode 比對 ──────────────────────────────────
        # 只有 CASBIN_SHADOW_ENABLED=True 且 enforcer 已 init 才會跑;這支
        # 完全不影響本檢查的結果(allow/deny 仍由上方 list[str] 邏輯決定),
        # 只把 Casbin 算出的決定 log 出來。Phase 4 cutover 才把實際的 enforce
        # 切到 require_casbin。
        if _casbin.is_shadow_enabled() and _casbin.get_enforcer() is not None:
            try:
                _shadow_compare(user, needed, legacy_allow)
            except Exception:
                # shadow 路徑出錯絕不擋住正常請求 — 它只是 log。
                _shadow_logger.exception("shadow compare crashed")

        if missing:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "permission_denied",
                    "missing_permissions": missing,
                },
            )
        return user

    return _check


# ── Phase 3.1 helpers ─────────────────────────────────────────────────


def _shadow_compare(user: User, needed: tuple[str, ...] | list[str], legacy_allow: bool) -> None:
    """計算 Casbin 對同一組 (user, needed) 的決定,跟 legacy 結果 log 出差異。

    diff 的形式:

    * legacy=allow, casbin=allow → 不 log(noise)
    * legacy=deny,  casbin=deny  → 不 log
    * legacy=allow, casbin=deny  → 警告:Casbin 缺對應 policy / g rule
    * legacy=deny,  casbin=allow → 警告:Casbin 過度開放

    log 出來的 key:``CASBIN_SHADOW`` + match | divergence_a | divergence_b,
    Phase 3 驗證階段 admin 直接 grep 即可。
    """
    from app.auth.permissions_catalog import permission_to_casbin

    dom = _casbin.org_domain(current_org_id.get())
    failed_pairs: list[tuple[str, str, str]] = []  # (perm, obj, act)
    for perm in needed:
        try:
            obj, act = permission_to_casbin(perm)
        except KeyError:
            failed_pairs.append((perm, "<no-mapping>", "<no-mapping>"))
            continue
        if not _casbin.enforce(user.username, dom, obj, act):
            failed_pairs.append((perm, obj, act))
    casbin_allow = not failed_pairs

    if legacy_allow == casbin_allow:
        # 兩邊都同意,單行 INFO 留個成功 footprint(預設 root WARN 不會印,
        # 想看細節時把 shadow logger 拉到 INFO 就好)。
        _shadow_logger.info(
            "CASBIN_SHADOW match user=%s dom=%s perms=%s verdict=%s",
            user.username, dom, ",".join(needed), "allow" if legacy_allow else "deny",
        )
        return

    label = "divergence_a" if legacy_allow else "divergence_b"
    _shadow_logger.warning(
        "CASBIN_SHADOW %s user=%s dom=%s legacy=%s casbin=%s perms=%s casbin_failed=%s",
        label,
        user.username,
        dom,
        "allow" if legacy_allow else "deny",
        "allow" if casbin_allow else "deny",
        ",".join(needed),
        ",".join(f"{o}:{a}" for _p, o, a in failed_pairs) or "(none)",
    )


# ── Casbin-backed dependency(Phase 2.2)─────────────────────────────────
#
# 跟 :func:`require_permission` 一樣的介面,但走 Casbin enforcer。Phase 3
# 跑 shadow-mode 時兩者並存(``require_permission`` 仍是 source of truth),
# Phase 4 cutover 才把 router 端 ``Depends(require_permission(P.X))``
# 改成 ``Depends(require_casbin(P.X))``。
#
# 行為:
#
# * superuser 直接放行(跟 require_permission 一致;Casbin 也可以寫 ``p, *, ...``
#   policy 表達,但在 dependency 層先 short-circuit 更省一次 enforce)。
# * Active org domain 從 ``current_org_id`` ContextVar 取(由 middleware 解出)。
# * 多權限呼叫(``require_casbin(P.A, P.B)``)→ 全部要通過才放行;任一失敗回
#   403 並列出失敗的 (obj, act) tuple,方便前端錯誤訊息 surface。
# * Casbin 沒啟用時 fail-open:回呼 ``require_permission`` 同樣的檢查。這讓
#   Phase 4.1 機械式換掉 dependency 不需要等 Phase 3 enforcer 上線。

def require_casbin(*needed: str) -> Callable:
    """Build a FastAPI dependency backed by Casbin。

    參數是 ``permissions_catalog.P`` 內的字串(``"<resource>.<action>"``),
    跟 ``require_permission`` 完全相同 — 改名只是為了讓 Phase 4 refactor 容易
    grep,實際解析會透過 :func:`permissions_catalog.permission_to_casbin`
    轉成 ``(obj, act)``。

    Casbin 沒啟用 → 退回 ``require_permission`` 的 list[str] 檢查,避免在
    Phase 2/3 卡 Phase 4 的 refactor。
    """
    if not needed:
        raise ValueError("require_casbin() requires at least one permission key")

    from app.auth.permissions_catalog import permission_to_casbin  # 區域 import 避免循環

    pairs: list[tuple[str, str, str]] = []
    for perm in needed:
        try:
            obj, act = permission_to_casbin(perm)
        except KeyError as e:
            raise ValueError(
                f"require_casbin: permission '{perm}' 沒有對應的 Casbin (obj, act) — "
                f"請補進 permissions_catalog.PERMISSION_TO_CASBIN"
            ) from e
        pairs.append((perm, obj, act))

    async def _check(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> User:
        if user.is_superuser:
            return user

        if not _casbin.is_enabled() or _casbin.get_enforcer() is None:
            # Fall back 到 list[str] 檢查 — 跟 require_permission 完全一致。
            granted: set[str] = set()
            if user.role_id is not None:
                role = await db.get(Role, user.role_id)
                if role is not None and role.permissions_json:
                    granted = set(role.permissions_json)
            missing = [p for p, _, _ in pairs if p not in granted]
            if missing:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "error": "permission_denied",
                        "missing_permissions": missing,
                    },
                )
            return user

        dom = _casbin.org_domain(current_org_id.get())
        missing_pairs: list[tuple[str, str]] = []
        for _perm, obj, act in pairs:
            if not _casbin.enforce(user.username, dom, obj, act):
                missing_pairs.append((obj, act))
        if missing_pairs:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "permission_denied",
                    "missing_permissions": [p for p, _, _ in pairs if (
                        permission_to_casbin(p) in missing_pairs
                    )],
                    "casbin_failed": [f"{o}:{a}" for o, a in missing_pairs],
                },
            )
        return user

    return _check
