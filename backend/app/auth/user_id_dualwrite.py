"""v1.1.7 Phase 5: dual-write the new ``user_id`` shadow column.

Phase 3 加好 ``user_id`` shadow column,但 application 9 個 instantiation
site 還在塞 ``username``。如果 Phase 7 要把 PK 換成 ``users.id``,過渡期內
**新建**的 row 也必須有 user_id 值,不然 cutover 時 NOT NULL 加不起來。

選擇用 SQLAlchemy ``before_insert`` event listener 而非手改 9 個 caller:

- caller 不必知道 user_id 存在(forward compat,Phase 7 後它們又會被切去
  傳 user_id 而非 username)
- 單點驗證:listener 裡可以 raise 把「找不到對應 users.id」立刻變 500,
  避免靜默 NULL
- Listener 是 lifespan 階段註冊,測試環境也吃這條同樣邏輯

Phase 7 PK cutover 後,application code 改成傳 user_id;這時 listener
反向 backfill username(或拔掉 listener)即可。
"""
from __future__ import annotations

import logging

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.models.group import GroupMembership
from app.models.org_membership import OrgMembership
from app.models.password_reset_token import PasswordResetToken
from app.models.project_member import ProjectMember
from app.models.user import User

_logger = logging.getLogger(__name__)


def _resolve_user_id_by_username(session: Session, username: str | None) -> str | None:
    """username → users.id。找不到 → None;呼叫端決定要不要 raise。"""
    if not username:
        return None
    return session.execute(
        select(User.id).where(User.username == username)
    ).scalar_one_or_none()


def _listener_org_membership(_mapper, _connection, target: OrgMembership) -> None:
    if not getattr(target, "user_id", None) and target.username:
        # event listener 跑在 flush 階段,target 上 mapper 已經連到 session
        session = Session.object_session(target)
        if session is not None:
            uid = _resolve_user_id_by_username(session, target.username)
            if uid is None:
                raise RuntimeError(
                    f"OrgMembership.username='{target.username}' 找不到對應 users.id"
                )
            target.user_id = uid
    # invited_by_user_id 同理(只在 invited_by 有值時 backfill)
    if not getattr(target, "invited_by_user_id", None) and getattr(target, "invited_by", None):
        session = Session.object_session(target)
        if session is not None:
            target.invited_by_user_id = _resolve_user_id_by_username(
                session, target.invited_by
            )


def _listener_project_member(_mapper, _connection, target: ProjectMember) -> None:
    if not getattr(target, "user_id", None) and target.username:
        session = Session.object_session(target)
        if session is not None:
            uid = _resolve_user_id_by_username(session, target.username)
            if uid is None:
                raise RuntimeError(
                    f"ProjectMember.username='{target.username}' 找不到對應 users.id"
                )
            target.user_id = uid
    if not getattr(target, "invited_by_user_id", None) and getattr(target, "invited_by", None):
        session = Session.object_session(target)
        if session is not None:
            target.invited_by_user_id = _resolve_user_id_by_username(
                session, target.invited_by
            )


def _listener_group_membership(_mapper, _connection, target: GroupMembership) -> None:
    if not getattr(target, "user_id", None) and target.username:
        session = Session.object_session(target)
        if session is not None:
            uid = _resolve_user_id_by_username(session, target.username)
            if uid is None:
                raise RuntimeError(
                    f"GroupMembership.username='{target.username}' 找不到對應 users.id"
                )
            target.user_id = uid


def _listener_password_reset_token(_mapper, _connection, target: PasswordResetToken) -> None:
    if not getattr(target, "user_id", None) and target.username:
        session = Session.object_session(target)
        if session is not None:
            uid = _resolve_user_id_by_username(session, target.username)
            if uid is None:
                raise RuntimeError(
                    f"PasswordResetToken.username='{target.username}' 找不到對應 users.id"
                )
            target.user_id = uid


_registered = False


def register_user_id_dualwrite_listeners() -> None:
    """lifespan 階段呼叫一次。重複呼叫無害(event.listen 內部 dedupe)。"""
    global _registered
    if _registered:
        return
    event.listen(OrgMembership, "before_insert", _listener_org_membership)
    event.listen(ProjectMember, "before_insert", _listener_project_member)
    event.listen(GroupMembership, "before_insert", _listener_group_membership)
    event.listen(PasswordResetToken, "before_insert", _listener_password_reset_token)
    _registered = True
    _logger.info("user_id dual-write listeners registered (v1.1.7 Phase 5)")
