"""Database access layer.

Two distinct paths live here, intentionally separate:

* **Async** (FastAPI request handlers) -- ``app.database`` exposes
  ``engine``, ``AsyncSessionLocal``, ``get_db``. Routers must use these.
* **Sync** (Celery tasks) -- :mod:`app.db.sync_session` exposes
  ``SessionLocal`` and the :func:`task_context` context manager. Workers
  must use these.

Mixing the two (e.g. an async session inside a Celery task) leaks
connections because asyncpg ties each connection to the event loop that
opened it; Celery's prefork model creates a new loop per task. The
import-lint rule under ``[tool.ruff.lint.per-file-ignores]`` enforces
the boundary.
"""
