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


# Existing debt captured on 2026-05-03 (refreshed 2026-05-07 after multiple
# router edits shifted line numbers and after testcases.py grew the
# precondition / env-binding endpoints). Keep this list shrinking.
KNOWN_BARE_SELECTS: set[tuple[str, int, str]] = {
    ("app/routers/defects.py", 73, "Defect"),
    ("app/routers/executions.py", 124, "ExecutionReport"),
    ("app/routers/executions.py", 160, "ExecutionReport"),
    ("app/routers/import_export.py", 36, "TestcaseContent"),
    ("app/routers/project_settings.py", 50, "ProjectEnvVar"),
    ("app/routers/project_settings.py", 89, "ProjectEnvVar"),
    ("app/routers/project_settings.py", 113, "ProjectDevice"),
    ("app/routers/project_settings.py", 154, "ProjectDevice"),
    ("app/routers/projects.py", 140, "TreeNode"),
    ("app/routers/reports.py", 132, "ExecutionReport"),
    ("app/routers/reports.py", 207, "ExecutionReport"),
    ("app/routers/reports.py", 337, "ExecutionStepLog"),
    ("app/routers/reports.py", 391, "ExecutionReport"),
    ("app/routers/requirements.py", 97, "Requirement"),
    ("app/routers/requirements.py", 214, "RequirementTestcaseLink"),
    ("app/routers/requirements.py", 244, "Requirement"),
    ("app/routers/requirements.py", 250, "TreeNode"),
    ("app/routers/requirements.py", 261, "RequirementTestcaseLink"),
    ("app/routers/requirements.py", 326, "Requirement"),
    ("app/routers/requirements.py", 337, "RequirementTestcaseLink"),
    ("app/routers/requirements.py", 347, "TreeNode"),
    ("app/routers/requirements.py", 365, "Defect"),
    ("app/routers/reviews.py", 419, "ReviewHistory"),
    ("app/routers/schedules.py", 99, "TreeNode"),
    ("app/routers/schedules.py", 143, "Schedule"),
    ("app/routers/schedules.py", 182, "TreeNode"),
    ("app/routers/schedules.py", 200, "TreeNode"),
    ("app/routers/schedules.py", 244, "TreeNode"),
    ("app/routers/schedules.py", 292, "TreeNode"),
    ("app/routers/test_data_sets.py", 60, "TestDataSet"),
    ("app/routers/test_documents.py", 62, "TestDocument"),
    ("app/routers/test_milestones.py", 53, "TestMilestone"),
    ("app/routers/test_plans.py", 62, "TestPlan"),
    ("app/routers/test_rounds.py", 61, "TreeNode"),
    ("app/routers/test_rounds.py", 90, "TestRound"),
    ("app/routers/testcases.py", 54, "TestcaseContent"),
    ("app/routers/testcases.py", 91, "TestcaseContent"),
    ("app/routers/testcases.py", 137, "TestcaseContent"),
    ("app/routers/testcases.py", 191, "TestcasePreconditionLink"),
    ("app/routers/testcases.py", 366, "TestcaseEnvBinding"),
    ("app/routers/wbs_items.py", 66, "WbsItem"),
    ("app/routers/wbs_items.py", 91, "WbsItem"),
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
