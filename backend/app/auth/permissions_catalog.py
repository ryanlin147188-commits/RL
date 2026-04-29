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
