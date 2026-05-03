"""Prevent new bare ``select(TenantScopedModel)`` calls in routers.

``app.auth.tenant.TenantQuery`` is the canonical query factory for models that
carry ``organization_id``. Routers that start from ``select(Model)`` are easy to
forget to scope, which can become an IDOR. The current codebase still has a
known baseline of legacy bare selects; this lint acts as a ratchet so new ones
do not slip in while those are migrated gradually.

Run::

    python backend/scripts/lint_tenant_scoped_selects.py

If this fails for a touched line, prefer replacing ``select(Model)`` with
``TenantQuery.for_(Model)``. Update the baseline only when a reviewer has
explicitly accepted the temporary exception.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = BACKEND_ROOT / "app" / "models"
ROUTERS_DIR = BACKEND_ROOT / "app" / "routers"


# Existing debt captured on 2026-05-03. Keep this list shrinking.
KNOWN_BARE_SELECTS: set[tuple[str, int, str]] = {
    ("app/routers/defects.py", 69, "Defect"),
    ("app/routers/executions.py", 109, "ExecutionReport"),
    ("app/routers/executions.py", 145, "ExecutionReport"),
    ("app/routers/import_export.py", 36, "TestcaseContent"),
    ("app/routers/project_settings.py", 45, "ProjectEnvVar"),
    ("app/routers/project_settings.py", 80, "ProjectEnvVar"),
    ("app/routers/project_settings.py", 100, "ProjectDevice"),
    ("app/routers/project_settings.py", 137, "ProjectDevice"),
    ("app/routers/projects.py", 95, "TreeNode"),
    ("app/routers/reports.py", 123, "ExecutionReport"),
    ("app/routers/reports.py", 194, "ExecutionReport"),
    ("app/routers/reports.py", 324, "ExecutionStepLog"),
    ("app/routers/reports.py", 378, "ExecutionReport"),
    ("app/routers/requirements.py", 91, "Requirement"),
    ("app/routers/requirements.py", 194, "RequirementTestcaseLink"),
    ("app/routers/requirements.py", 219, "Requirement"),
    ("app/routers/requirements.py", 225, "TreeNode"),
    ("app/routers/requirements.py", 236, "RequirementTestcaseLink"),
    ("app/routers/requirements.py", 297, "Requirement"),
    ("app/routers/requirements.py", 308, "RequirementTestcaseLink"),
    ("app/routers/requirements.py", 318, "TreeNode"),
    ("app/routers/requirements.py", 336, "Defect"),
    ("app/routers/reviews.py", 245, "ReviewHistory"),
    ("app/routers/schedules.py", 98, "TreeNode"),
    ("app/routers/schedules.py", 137, "Schedule"),
    ("app/routers/schedules.py", 176, "TreeNode"),
    ("app/routers/schedules.py", 194, "TreeNode"),
    ("app/routers/schedules.py", 238, "TreeNode"),
    ("app/routers/schedules.py", 286, "TreeNode"),
    ("app/routers/test_data_sets.py", 54, "TestDataSet"),
    ("app/routers/test_documents.py", 60, "TestDocument"),
    ("app/routers/test_milestones.py", 43, "TestMilestone"),
    ("app/routers/test_plans.py", 51, "TestPlan"),
    ("app/routers/test_rounds.py", 60, "TreeNode"),
    ("app/routers/test_rounds.py", 84, "TestRound"),
    ("app/routers/testcases.py", 49, "TestcaseContent"),
    ("app/routers/testcases.py", 86, "TestcaseContent"),
    ("app/routers/testcases.py", 132, "TestcaseContent"),
    ("app/routers/wbs_items.py", 51, "WbsItem"),
    ("app/routers/wbs_items.py", 71, "WbsItem"),
}


def _base_name(base: ast.expr) -> str | None:
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    if isinstance(base, ast.Subscript):
        return _base_name(base.value)
    return None


def _tenant_scoped_models() -> set[str]:
    models: set[str] = set()
    for path in MODELS_DIR.glob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            print(f"cannot parse {path.relative_to(BACKEND_ROOT)}: {exc}", file=sys.stderr)
            return set()
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                if any(_base_name(base) == "TenantScoped" for base in node.bases):
                    models.add(node.name)
    return models


def _selected_model_name(arg: ast.expr) -> str | None:
    if isinstance(arg, ast.Name):
        return arg.id
    if isinstance(arg, ast.Attribute):
        return arg.attr
    return None


def _find_bare_selects(models: set[str]) -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    for path in sorted(ROUTERS_DIR.glob("*.py")):
        rel = str(path.relative_to(BACKEND_ROOT))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            print(f"cannot parse {rel}: {exc}", file=sys.stderr)
            return [(rel, 0, "SyntaxError")]
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "select"
            ):
                continue
            for arg in node.args:
                model_name = _selected_model_name(arg)
                if model_name in models:
                    findings.append((rel, node.lineno, model_name))
    return findings


def main() -> int:
    models = _tenant_scoped_models()
    if not models:
        print("tenant select lint: no TenantScoped models discovered", file=sys.stderr)
        return 1

    findings = _find_bare_selects(models)
    new_findings = [f for f in findings if f not in KNOWN_BARE_SELECTS]
    stale_baseline = sorted(KNOWN_BARE_SELECTS - set(findings))

    if new_findings or stale_baseline:
        if new_findings:
            print("New bare select(TenantScopedModel) calls found:", file=sys.stderr)
            for rel, line, model in new_findings:
                print(
                    f"  {rel}:{line}: select({model}) -- use TenantQuery.for_({model})",
                    file=sys.stderr,
                )
        if stale_baseline:
            print("Tenant select baseline entries no longer present:", file=sys.stderr)
            for rel, line, model in stale_baseline:
                print(f"  {rel}:{line}: {model}", file=sys.stderr)
            print("Remove stale entries from KNOWN_BARE_SELECTS.", file=sys.stderr)
        return 1

    print(f"tenant select lint: OK ({len(findings)} baseline entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
