"""Enforce the FastAPI / Celery DB-session boundary (RFC-9).

Rules:

* Files under ``app/routers/`` -- must NOT import ``app.db.sync_session``.
  Routers are async and must use ``app.database`` (asyncpg).

* Files under ``tasks/`` -- must NOT import the async session
  ``from app.database import AsyncSessionLocal`` or use
  ``app.database.engine`` / ``get_db``. Tasks are sync and must use
  ``app.db.sync_session``.

The rule is a single-purpose AST-style grep (``ast.parse`` + walk) rather
than a regex so we do not accidentally catch the strings inside docstrings
or comments.

Run::

    python backend/scripts/lint_sync_async_boundary.py

Exits non-zero if any violation is found. Wired into CI as a fast pre-test
gate.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

# Resolve the backend root regardless of where the script is run from.
BACKEND_ROOT = Path(__file__).resolve().parents[1]


# (target_dir_relative_to_backend_root, banned_modules, friendly_reason)
RULES: list[tuple[str, set[str], str]] = [
    (
        "app/routers",
        {"app.db.sync_session"},
        "routers are async — use app.database / AsyncSessionLocal instead",
    ),
    (
        "tasks",
        {
            "app.database",  # any of: engine, AsyncSessionLocal, get_db, init_db
        },
        "Celery tasks are sync — use app.db.sync_session.SessionLocal / task_context",
    ),
]


def _collect_imports(path: Path) -> set[str]:
    """Return the dotted module names imported by ``path`` (top-level only)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
    return found


def main() -> int:
    violations: list[str] = []
    for rel_dir, banned, reason in RULES:
        target = BACKEND_ROOT / rel_dir
        if not target.exists():
            continue
        for py in target.rglob("*.py"):
            if py.name == "__init__.py":
                continue
            imports = _collect_imports(py)
            for bad in banned & imports:
                violations.append(
                    f"  {py.relative_to(BACKEND_ROOT)}: imports `{bad}` -- {reason}"
                )
    if violations:
        print("Sync/async boundary violations (RFC-9):", file=sys.stderr)
        for v in violations:
            print(v, file=sys.stderr)
        return 1
    print("sync/async boundary: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
