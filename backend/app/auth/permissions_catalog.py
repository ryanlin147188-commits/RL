"""Catalogue of permission keys recognised across the API.

These constants must stay byte-identical to the strings stored in
``Role.permissions_json`` so a typo at the call site shows up as ``mypy``
``Final`` mismatch -- not as a silent always-deny.

The seed data in ``app.main._seed_default_roles`` references these strings
directly; if you add a new permission below, also extend the appropriate
default role's list there (or supersede via an Alembic data migration).
"""
from __future__ import annotations

from typing import Final


class P:
    """Permission keys -- ``<resource>.<action>``.

    Convention: ``read`` for GET; ``write`` for POST/PUT/PATCH; ``delete``
    for DELETE; resource-specific verbs (``execute``, ``approve``, ``manage``)
    for non-CRUD actions.
    """

    # ── Project ─────────────────────────────────────────────────────────
    PROJECT_READ:    Final[str] = "project.read"
    PROJECT_WRITE:   Final[str] = "project.write"
    PROJECT_DELETE:  Final[str] = "project.delete"

    # ── Test case ───────────────────────────────────────────────────────
    TESTCASE_READ:    Final[str] = "testcase.read"
    TESTCASE_WRITE:   Final[str] = "testcase.write"
    TESTCASE_DELETE:  Final[str] = "testcase.delete"
    TESTCASE_EXECUTE: Final[str] = "testcase.execute"

    # ── Defect ──────────────────────────────────────────────────────────
    DEFECT_READ:    Final[str] = "defect.read"
    DEFECT_WRITE:   Final[str] = "defect.write"
    DEFECT_DELETE:  Final[str] = "defect.delete"

    # ── Requirement ─────────────────────────────────────────────────────
    REQUIREMENT_READ:    Final[str] = "requirement.read"
    REQUIREMENT_WRITE:   Final[str] = "requirement.write"
    REQUIREMENT_DELETE:  Final[str] = "requirement.delete"

    # ── Test plan ───────────────────────────────────────────────────────
    PLAN_READ:    Final[str] = "plan.read"
    PLAN_WRITE:   Final[str] = "plan.write"
    PLAN_APPROVE: Final[str] = "plan.approve"

    # ── WBS / Document ──────────────────────────────────────────────────
    WBS_READ:       Final[str] = "wbs.read"
    WBS_WRITE:      Final[str] = "wbs.write"
    DOCUMENT_READ:  Final[str] = "document.read"
    DOCUMENT_WRITE: Final[str] = "document.write"

    # ── Reports ─────────────────────────────────────────────────────────
    REPORT_READ:  Final[str] = "report.read"

    # ── Settings + admin ────────────────────────────────────────────────
    SETTINGS_READ:  Final[str] = "settings.read"
    SETTINGS_WRITE: Final[str] = "settings.write"
    USER_MANAGE:    Final[str] = "user.manage"
    ROLE_MANAGE:    Final[str] = "role.manage"

    # ── Review / approval workflow ──────────────────────────────────────
    REVIEW_READ:    Final[str] = "review.read"
    REVIEW_SUBMIT:  Final[str] = "review.submit"
    REVIEW_MANAGE:  Final[str] = "review.manage"   # approve / reject / revert


ALL_PERMISSIONS: Final[frozenset[str]] = frozenset(
    v for k, v in vars(P).items() if not k.startswith("_") and isinstance(v, str)
)


# ── Casbin obj / act mapping ───────────────────────────────────────────
#
# 每個 ``<resource>.<action>`` 權限對到一個 ``(obj, act)`` tuple,給 Casbin
# enforcer 用。obj 形如 ``project:*``(萬用)/ ``project:proj-123``(特定 id),
# act 對到 RBAC 約定的動詞(``read`` / ``write`` / ``delete`` / ``execute`` /
# ``approve`` / ``manage``)。
#
# Phase 2.4 的 sync 層會把 ``Role.permissions_json`` 轉成 Casbin policy:
# 例如 Role "QA" 有 ``testcase.write`` → 寫出 ``p, QA, <dom>, testcase:*, write``。
#
# Phase 4.1 refactor 時,每個 ``require_permission(P.X)`` 都用這張表查出對應的
# (obj_pattern, act) 換成 ``require_casbin(obj_pattern, act)``;對 per-resource
# id 的 route(如 ``/projects/{pid}``)會把 ``*`` 換成實際 pid。

# Action verbs — 強制限縮在這幾個,讓 keyMatch2 與 policy 寫法統一。
ACT_READ:    Final[str] = "read"
ACT_WRITE:   Final[str] = "write"
ACT_DELETE:  Final[str] = "delete"
ACT_EXECUTE: Final[str] = "execute"
ACT_APPROVE: Final[str] = "approve"
ACT_MANAGE:  Final[str] = "manage"

# 23 條完整對應 — 不省略,讓查詢直觀,加新權限時這裡漏改會在 sync layer
# 跑出來時馬上拋 KeyError。
PERMISSION_TO_CASBIN: Final[dict[str, tuple[str, str]]] = {
    P.PROJECT_READ:       ("project:*",     ACT_READ),
    P.PROJECT_WRITE:      ("project:*",     ACT_WRITE),
    P.PROJECT_DELETE:     ("project:*",     ACT_DELETE),

    P.TESTCASE_READ:      ("testcase:*",    ACT_READ),
    P.TESTCASE_WRITE:     ("testcase:*",    ACT_WRITE),
    P.TESTCASE_DELETE:    ("testcase:*",    ACT_DELETE),
    P.TESTCASE_EXECUTE:   ("testcase:*",    ACT_EXECUTE),

    P.DEFECT_READ:        ("defect:*",      ACT_READ),
    P.DEFECT_WRITE:       ("defect:*",      ACT_WRITE),
    P.DEFECT_DELETE:      ("defect:*",      ACT_DELETE),

    P.REQUIREMENT_READ:   ("requirement:*", ACT_READ),
    P.REQUIREMENT_WRITE:  ("requirement:*", ACT_WRITE),
    P.REQUIREMENT_DELETE: ("requirement:*", ACT_DELETE),

    P.PLAN_READ:          ("plan:*",        ACT_READ),
    P.PLAN_WRITE:         ("plan:*",        ACT_WRITE),
    P.PLAN_APPROVE:       ("plan:*",        ACT_APPROVE),

    P.WBS_READ:           ("wbs:*",         ACT_READ),
    P.WBS_WRITE:          ("wbs:*",         ACT_WRITE),
    P.DOCUMENT_READ:      ("document:*",    ACT_READ),
    P.DOCUMENT_WRITE:     ("document:*",    ACT_WRITE),

    P.REPORT_READ:        ("report:*",      ACT_READ),

    P.SETTINGS_READ:      ("settings:*",    ACT_READ),
    P.SETTINGS_WRITE:     ("settings:*",    ACT_WRITE),
    P.USER_MANAGE:        ("user:*",        ACT_MANAGE),
    P.ROLE_MANAGE:        ("role:*",        ACT_MANAGE),

    P.REVIEW_READ:        ("review:*",      ACT_READ),
    P.REVIEW_SUBMIT:      ("review:*",      ACT_WRITE),   # 提交 review = 寫
    P.REVIEW_MANAGE:      ("review:*",      ACT_MANAGE),  # approve / revert
}


def permission_to_casbin(perm: str) -> tuple[str, str]:
    """``"testcase.write"`` → ``("testcase:*", "write")``。

    缺對應時拋 ``KeyError`` — 由 caller 自己處理(shadow-mode log /
    fail-closed deny)。
    """
    return PERMISSION_TO_CASBIN[perm]
