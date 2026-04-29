# Backend domain layout (RFC-10) — DEFERRED

This directory is reserved for the per-domain restructure outlined in
RFC-10. **It is intentionally empty** in this batch.

## Why deferred

RFC-10 moves ~37 model files + 20 router files + the services / schemas
trees into per-domain folders (`domains/projects/`, `domains/testcases/`,
…). That is a mechanical 8-12 day refactor that:

1. Conflicts with the in-flight RFC-1 frontend split (large diffs make
   reviews much harder when both happen at once).
2. Breaks every existing import path. Every other RFC has been crafted to
   land without forcing simultaneous churn — RFC-10 cannot.
3. Has lower payoff per day-of-work than the other open RFCs. The current
   flat layout is navigable; the win from DDD is incremental clarity, not
   a missing capability.

## When to revisit

Trigger any of:

* Backend `routers/` exceeds ~30 files.
* Two contributors hit cross-router merge conflicts in the same week.
* A new contributor takes >2 days to onboard to "where does X live."
* RFC-1 frontend split is fully landed (so we don't churn both at once).

## How it would land

Per the RFC, the migration is per-domain, not big-bang:

1. Land an empty `domains/<x>/` for one domain at a time.
2. Move `models/<x>.py`, `routers/<x>.py`, `services/<x>_service.py`,
   `schemas/<x>.py` into `domains/<x>/{models,router,service,schemas}.py`.
3. Add a re-export shim at the old paths so unrelated imports keep
   working (`# from app.models.x import X  # legacy; use app.domains.x`).
4. Run the full test suite + ruff boundary lint — both must stay green.
5. Repeat for the next domain.

A scratch shim for the import-redirection pattern is in
`scripts/audit_endpoints.py` (RFC-5 work) — re-use that AST walker to
generate the legacy re-export modules automatically.

## Owner

Unassigned. File this as a tracked task in the issue tracker referenced
as `RFC-10 deferred — see backend/app/domains/README.md`.
