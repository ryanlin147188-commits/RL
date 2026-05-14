# AutoTest — Test Automation Platform

> 🌐 **Languages**: **English** · [繁體中文](README.zh-TW.md)

> **A self-hosted test automation platform with a recorder, BDD case editor, and a Robot Framework + Playwright runner — all behind a single Docker Compose stack.**
> Apache 2.0. Runs entirely on your own network. No license fees, no per-user pricing, no telemetry.

[![License](https://img.shields.io/badge/License-Apache%202.0-green.svg)](LICENSES.md)
[![Robot Framework](https://img.shields.io/badge/Engine-Robot%20Framework%207.x-blue.svg)](https://robotframework.org/)
[![Docker](https://img.shields.io/badge/Deploy-Docker%20Compose-2496ED.svg)](https://docs.docker.com/compose/)
[![Stack](https://img.shields.io/badge/Stack-FastAPI%20%2B%20PostgreSQL%20%2B%20SeaweedFS-0a7e07.svg)](#tech-stack)

---

## What this is

AutoTest is the smallest opinionated self-hosted setup that lets a QA team:

1. **Author** test cases as Markdown + BDD steps, with Capture variables,
   If/ElseIf/Else branches, dynamic expressions, and data-driven rows (DDT).
2. **Record** in three modes — web (Playwright codegen), API (mitmproxy or
   cURL paste), and app (Appium / iOS-Web-Inspector / Android-uiautomator2).
3. **Execute** in isolated per-run Docker containers via Robot Framework
   7.x + Playwright Browser Library + AppiumLibrary, with real-time
   WebSocket logs, per-step screenshots, video, and Playwright trace.
4. **Report** with an Allure-style execution view (history charts, trace
   viewer, defect linking via direct text fields), exportable to PDF.

What was originally a sprawling ALM platform (defects / requirements / RTM
/ WBS / kanban / AI assistant / device pools / test plans / milestones /
schedules / assignments / test documents) has been deliberately slimmed
down — see **v1.1.9** below.

---

## 🔥 v1.1.9 — Slim cutdown

The platform was trimmed back to the test-case authoring → execution →
report loop. 13 features were removed:

- 測試看版 (multi-entity kanban)
- 測試專案 page (the standalone project-management workspace; the
  `projects` entity is still there, accessed via the sidebar switcher)
- DB 資訊 (per-project DB connection registry)
- AI 助理 — full removal of the Hermes ACP / mem0 / OpenClaw stack:
  three sidecar containers, ~14 backend files, the chat panel,
  AI Token settings, AI-enhance-recording, MCP test panel, and
  vision-enhanced recording
- 設備資訊 (project device inventory)
- 測試文件 (test documentation entity)
- 測試版號 UI (the `test_versions` model is retained because Defect /
  TestRound / ExecutionReport still have `test_version_id` FK columns,
  but the management page is gone)
- 測試時程 (`schedules` + `test_milestones` + the cron-style
  `scheduler_loop` background task)
- 測試計畫
- WBS (work breakdown structure + WBS links)
- 需求/RTM UI (model kept for FK integrity, UI gone)
- 缺陷管理 UI (model kept, UI gone)
- 指派 (the cross-entity Assignment / "My Work" inbox / bulk reassign).
  `assigned_to` columns on remaining entities are unchanged — testcases
  and reviews still set assignees through their own PATCH endpoints.

**DB schema is preserved.** All alembic migrations stay, and the
to-be-removed tables remain in PostgreSQL. A future migration can drop
them outright if needed.

**Frontend dead code was swept.** ~2,100 lines of orphan JS that no
longer had a UI entry point (AI chat helpers, Hermes modal helpers,
MCP test panel, AI Token CRUD, My Work inbox, recording AI-enhance,
agent runtime prefs) were deleted along with the CSS selectors targeting
removed elements. `showBacklogView` was rewritten to bypass the deleted
kanban wrapper so the **待辦清單** view stays reachable.

---

## 🔥 v1.1.8.1 — Middleware decode + OIDC JIT also go through fastapi-users

Follow-up to v1.1.8: close the two highest-value remaining gaps where
hand-rolled code lived alongside the fastapi-users primitives.

- **Middleware JWT decode**: `app.middleware.AuthMiddleware` now calls
  `fastapi_users_integration.decode_access_token_payload`, the same
  function `UsernameSubJWTStrategy.read_token` uses. Single source of
  truth for "what does a valid access token look like" — the dependency
  chain and the middleware can never drift apart. The `typ == "access"`
  check is also pulled into that helper, so refresh tokens sent to
  `/api/*` get a 401 from the same code path.
- **OIDC JIT through UserManager**: `routers/oidc_auth.py` no longer
  hand-rolls the "by (provider, sub) → by email → create" flow. The
  logic now lives on `UserManager.get_or_provision_via_oidc()`. SSO-
  created users now get **argon2** password hashes (via PasswordHelper)
  rather than bcrypt. Access token issuance moves from hand-rolled
  `create_access_token` to `JWTStrategy.write_token`.

### 🧹 Ops runbook: container log rotation is required

The Docker daemon ships **without** log rotation by default. Set a
host-level daemon config once:

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF
sudo systemctl restart docker
```

Caps every container's logs at `10MB × 3 files = 30MB`. **One-time
setup — don't repeat per deploy.** Reclaim already-grown logs with
`sudo find /var/lib/docker/containers -name '*-json.log' -exec truncate -s 0 {} +`.

Orphan cleanup: `docker volume prune -f` + `docker image prune -f` +
`docker builder prune -af`. **Do NOT run** `docker image prune -a -f` —
the platform spawns `autotest-robot-runner` / `autotest-recorder` on
demand via `docker.sock`, and those images aren't held by long-running
containers; a blanket `-a` prune deletes them and breaks test execution.

---

## 🔥 v1.1.8 — Auth router actually goes through fastapi-users

v1.1.7 imported fastapi-users and shipped four alembic migrations, but
on the request path only the PasswordHelper was actually invoked. v1.1.8
finishes the cut-over:

- **All 260 `Depends(get_current_user)` go through fastapi-users**:
  `dependencies.py::get_current_user` is a thin alias for
  `fastapi_users_integration.current_active_user`. Every request runs
  `_fa_current_active_user` → `UsernameSubJWTStrategy.read_token` →
  `UserManager.get_by_username` → DB lookup.
- **Login goes through `UserManager.authenticate_by_username`**:
  username lookup + bcrypt verify + **constant-time dummy hash for
  non-existent users** (prevents timing-attack leak) + **bcrypt → argon2
  progressive rehash** on successful verify.
- **Access tokens minted by `JWTStrategy.write_token`**:
  `UsernameSubJWTStrategy` overrides `read_token` / `write_token` to use
  `sub=username` rather than the fastapi-users default UUID id, keeping
  SPA / Casbin / log identifiers consistent.
- **Admin user CRUD via `UserManager`**: `POST /auth/users` uses
  `password_helper.hash()` + `on_after_register()`; `PUT` / reset-password
  use `_update()`; `DELETE` uses `delete()`.

Intentionally NOT moved to fastapi-users: refresh token (fastapi-users
13 has no concept), `must_change_password` gate, `org_id` cookie, Casbin
RBAC.

---

## 🔥 v1.1.7 — FastAPI Users primitives wired + schema migration

Eight commits and four alembic migrations on `feat/fastapi-users` move
the auth backend off hand-rolled bcrypt + JWT onto the standard
fastapi-users stack. SPA didn't change. Existing admin and 100+ users
survive the migration intact.

- Migration `0027` adds `users.id` UUID with `gen_random_uuid()::text`
  default; existing rows backfilled.
- Migration `0028` adds nullable `user_id` UUID shadow columns to the
  six FK sites (project_members ×2, org_memberships ×2,
  group_memberships, password_reset_tokens) and JOIN-backfills them.
- Migration `0029` promotes `users.id` to PK; the six FKs are recreated
  with explicit `REFERENCES users(username)` so they bind to the new
  `uq_users_username` unique constraint. Application code is untouched.

`SQLAlchemyUserDatabase`, `UserManager`, `JWTStrategy`, `PasswordHelper`,
`BearerTransport` are wired in `backend/app/auth/fastapi_users_integration.py`.

Zoho OAuth migrated from authlib `AsyncOAuth2Client` to
`httpx-oauth.BaseOAuth2` (the OAuth2 client family fastapi-users is
built around).

---

## 🔥 v1.1.6 — Per-project role permission override + three-field first-login modal

Same `Project-Tester` role can have different effective permissions in
different projects without cloning the whole role.

- **Per-project override table** `project_role_permissions(project_id,
  role_id, permissions_json)`. In the SPA, **設定 → 專案協作成員** panel
  shows a 「本專案角色權限」 section listing the 4 project-scope roles
  with their effective permission count + override badge; click 編輯 to
  flip individual permission checkboxes for this project only.
- **Casbin sync writes alias roles** `<role>@<short_pid>` to `casbin_rule`
  for any (project, role) that has an override row. Enforce automatically
  matches the alias's p rules in the specific `project:<pid>` domain.
- **First-login profile modal** expanded to three fields (display_name
  + email + new password) via `POST /api/auth/profile-setup`. Triggered
  by `users.must_change_password=True`.

### Recommended collaboration SOP

| Persona | Global `users.role_id` | `ProjectMember.role_id` | Use case |
|---|---|---|---|
| Platform owner (you) | NULL + `is_superuser=True` | — | Full platform write |
| Customer PM | NULL | `Project-Reviewer` | View, approve |
| External QA | NULL | `Project-Tester` (+ override if needed) | Write cases, run tests |
| Read-only stakeholder | NULL | `Project-Viewer` | Dashboards, reports |

---

## 🔥 v1.1.5 — Casdoor sidecar dropped, in-process authlib takes over

After spending two minor releases running Casdoor as a sidecar, IAM is
back inside the FastAPI process.

- **Casdoor sidecar removed.** Compose service / configs / `casdoor`
  Postgres DB / 14 backend modules all gone.
- **OIDC handled in-process with `authlib`**. New routes
  `GET /api/auth/{provider}/login` and `/callback` walk the OAuth code
  flow directly with the IdP. Currently only `zoho` is wired; adding
  Google / Microsoft / Okta is one 30-line `OIDCProvider` dataclass per
  provider in `backend/app/auth/oidc.py`.
- **Token format reverted to HS256 in-house JWT** (same as v1.1.2).
- **Local password endpoints resurrected**: `POST /auth/login` /
  forgot-password / reset-password / change-password / user CRUD /
  role CRUD back to live code.
- **Casbin retained, no behavior change.** Still in-process with
  `casbin_rule` table as source of truth.

### Activating Zoho SSO

```bash
# 1. https://api-console.zoho.com → Add Client → Server-based Applications
#    Authorized Redirect URIs: http://<your-host>/api/auth/zoho/callback
# 2. Add to .env:
echo "ZOHO_CLIENT_ID=<client_id>"     >> .env
echo "ZOHO_CLIENT_SECRET=<secret>"     >> .env
echo "ZOHO_REDIRECT_URL=http://<host>/api/auth/zoho/callback" >> .env
# 3. Restart backend:
docker compose up -d --force-recreate backend
# 4. Refresh SPA login page — orange "使用 Zoho 登入" button appears.
```

---

## Quick Start (Docker, ~5 minutes)

**Prerequisites**: Docker 24+ and Docker Compose v2.23+. Works on Linux,
macOS, and Windows (Docker Desktop).

```bash
git clone https://github.com/ryanlin147188-commits/RL-for-Kapito.git
cd RL-for-Kapito

# 1) Generate .env with random secrets (skip if you already have one).
docker compose --profile init run --rm bootstrap

# 2) Pre-build the spawn-time images (Robot runner / web recorder /
#    API recorder). These run as per-session containers, not long-lived
#    services, but the images must exist before the backend can
#    `docker run` them. First build takes ~5–10 min.
docker compose --profile spawnable build

# 3) Start the main stack.
docker compose up -d --build

# 4) (Optional) override the seeded default admin password BEFORE the
#    first backend start. If skipped, the seed uses 'admin123' and
#    forces a rotation on first login.
# echo "AUTOTEST_DEFAULT_ADMIN_PASSWORD=Op3rator-Init" >> .env
```

After backend starts, log in with `admin` / `admin123` (or your
override). First login is gated by a forced password change. Self-
service registration is disabled — additional users are created from
**設定 → 專案協作成員**.

**Default URLs after a successful boot**

| Service | URL |
|---|---|
| Web UI | <http://localhost> |
| REST API (Swagger) | exec into backend: `docker compose exec backend curl localhost:8000/docs` — port 8000 is no longer exposed; see [SECURITY.md](SECURITY.md) |
| Logs | `docker compose logs -f`; VictoriaLogs stays internal |

### Daily ops

| Want to… | Command |
|---|---|
| See running containers | `docker compose ps` |
| Tail logs | `docker compose logs -f` |
| Stop (preserve data) | `docker compose down` |
| Reset (DESTRUCTIVE — wipes DB + S3) | `docker compose down -v` |
| Enable observability stack | add `--profile obs` to up/down |
| Enable backend debug mode | `AUTOTEST_DEBUG=True docker compose up -d --force-recreate backend` |

---

## What's in the box (after v1.1.9)

| Layer | Capability |
|---|---|
| **Test-case management** | Project / Feature / Platform / Page / Scenario / TestCase tree, Markdown editing, version history (`entity_versions` mirror) |
| **Authoring** | Visual recorder (Playwright codegen), API recorder (mitmproxy / cURL paste), App recorder (Appium / iOS Web Inspector / Android uiautomator2), manual BDD editor, dynamic expressions, Capture steps, If / ElseIf / Else / EndIf branches |
| **Data-driven (DDT)** | Per-case data sets — a single case can iterate over N rows; `{{=row.column}}` references resolve at execution time |
| **Execution** | Robot Framework 7.x + Playwright headless, per-run isolated runner containers, real-time WebSocket logs, screenshots / video / trace per step, tags, retry on flaky |
| **Reports** | Allure-style report per execution, history charts (Chart.js), per-step trace viewer integration, PDF export |
| **Test Rounds** | Grouping of executions (e.g. "Smoke Round 2024-Q1"); shared dashboard per round |
| **Reviews** | Generic approval workflow for testcases / scripts / reports — pending / approved / rejected tabs, audit trail, reason field per decision |
| **Backlog (Todos)** | Feature → Task / Bug / Spike hierarchy, Sprint labels, due-date overdue badges, full CRUD; reachable via `#backlog` route and the linked-todos popover inside testcase / report detail views |
| **Settings — RBAC, members, groups, invites** | Permissions catalog, role CRUD with clone, project-scoped role overrides, group CRUD (nested, can be assignees), org members, project members, invite lifecycle (send / resend / extend / revoke / bulk) |
| **Auth / SSO** | fastapi-users + argon2 PasswordHelper, Zoho OIDC via `httpx-oauth`, JWT in httpOnly cookie, refresh token, must-change-password gate, three-field first-login profile setup |
| **Audit / Observability** | `audit_logs` middleware (SOC 2 baseline), Fluent Bit → VictoriaLogs per-container streams, opt-in Prometheus + Jaeger via `--profile obs` |
| **API gateway** | nginx → APISIX → backend; request-id, CORS, per-IP rate-limit, circuit breaker; backend port never exposed to host |
| **Storage** | All uploads land in SeaweedFS via S3-compatible API (`STORAGE_BACKEND=s3` enforced at startup) |
| **Mock endpoints** | Lightweight mock-API registry per org — define paths + canned responses for test cases that don't have a real backend yet |
| **Local Agent** | Headed-mode runner that lets you watch a test execute on a real desktop browser instead of a headless container |
| **Markdown import / export** | Round-trip a whole project tree to / from `.md` files |
| **REST API** | Full Swagger; `/api/executions` open for CI/CD — kick off cases from Jenkins / GitHub Actions / GitLab CI |

See [操作說明.md](操作說明.md) for the end-to-end user guide (Chinese).

---

## <a id="tech-stack"></a> Tech stack

```
┌─────────────────────────────────────────────────────────────────┐
│            Web UI (Vanilla JS + Tailwind CDN, no build step)    │
│            Lazy-loads Chart.js / Mermaid / html2pdf on demand   │
└──────────────────────────────┬──────────────────────────────────┘
                               │ REST + WebSocket (port 80)
┌──────────────────────────────▼──────────────────────────────────┐
│   nginx (front door, SPA shell, /recorder/<id>/* WS reverse-    │
│          proxy to dynamic spawn containers)                     │
└──────────────────────────────┬──────────────────────────────────┘
                               │ /api/*  /ws/*  /pics/*  /results/*
┌──────────────────────────────▼──────────────────────────────────┐
│   APISIX  (request-id · CORS · rate-limit · api-breaker)        │
└──────────────────────────────┬──────────────────────────────────┘
                               │ proxy to backend:8000 (internal)
┌──────────────────────────────▼──────────────────────────────────┐
│         FastAPI (Python 3.11) · OIDC · slowapi · Fernet         │
└────────┬───────────────┬─────────────────┬──────────────────────┘
         │               │                 │
┌────────▼─────┐ ┌───────▼──────┐ ┌────────▼──────────────────┐
│ PostgreSQL 16│ │  Valkey 8    │ │ Celery worker             │
│ (data)       │ │ (cache+queue)│ │  → Robot Framework runner │
└──────────────┘ └──────────────┘ │  → Playwright recorder    │
                                  │  → mitmproxy API recorder │
┌──────────────┐ ┌──────────────┐ │  (each in its own         │
│ SeaweedFS    │ │ Fluent Bit + │ │  short-lived container)   │
│ (S3, media)  │ │ VictoriaLogs │ └───────────────────────────┘
└──────────────┘ └──────────────┘
```

**Default compose** (after v1.1.9): 11 long-running services plus
`seaweedfs-init` one-shot (postgres / valkey / seaweedfs /
docker-proxy / backend / celery / frontend / apisix / fluent-bit /
victoria-logs / `seaweedfs-init`).
**Profile-gated**: 2 obs services (Prometheus + Jaeger), 4 spawn-time
images (`robot-runner` / `recorder` / `recorder-api` / `mcp` — built
once, run per session by backend), 1 bootstrap one-shot.

The Hermes / mem0 / mem0-postgres / openclaw sidecars from earlier
versions are gone in v1.1.9.

---

## Production hardening

Before exposing AutoTest to the internet:

- Set `ALLOWED_ORIGINS` to your front-end origin (never `*`).
- Override default secrets — `AUTOTEST_JWT_SECRET`, `AUTOTEST_FERNET_KEY`,
  `DB_PASSWORD`, `S3_ROOT_PASSWORD`. The bootstrap profile generates
  random values on first run; rotate them on a schedule.
- Run behind HTTPS (e.g. a reverse proxy with Let's Encrypt or your own CA).
- Pin `RECORDER_IMAGE` and `ROBOT_RUNNER_IMAGE` to specific tags or
  sha256 digests — never `latest`.
- Schedule backups of the PostgreSQL and SeaweedFS volumes.
- Read [SECURITY.md](SECURITY.md) for the vulnerability disclosure
  policy.

---

## FAQ

**Q: Why not just use TestRail / Zephyr / qTest?**
Pricing scales linearly with seats, your test data lives in someone
else's cloud, and the export formats are proprietary or empty shells.
AutoTest keeps everything on your infrastructure with standard open
formats.

**Q: Why not just Robot Framework + a CI server?**
Authoring UI, recording, per-step screenshots / video / trace, history
charts, RBAC, multi-tenant scoping, and the test-case tree aren't part
of vanilla Robot Framework. AutoTest composes them into one product so
a QA team can share a single source of truth.

**Q: I want the removed AI / kanban / RTM / defect features back.**
Check out the commit immediately before the v1.1.9 series — those
features were removed in 13 separate commits between `6283deb` and
`e64fc59`. Each is revertable on its own. The DB schema for the removed
entities is preserved so you can roll back without data loss.

**Q: Is this enterprise-ready?**
v1.1 ships single-tenant stop-gap guards. Multi-tenant isolation, MFA,
API tokens, and Helm charts are tracked on the roadmap. For commercial
deployments, please open an issue.

**Q: Apple Silicon (M1 / M2 / M3)?**
Works via Rosetta 2, but recorder containers run x86-64 and are 2–4×
slower than on a native amd64 host. Native arm64 images are on the
roadmap.

---

## Contributing

- Bug reports and feature requests: [open an issue](../../issues).
- Security vulnerabilities: see [SECURITY.md](SECURITY.md).
- Community guidelines: see [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
- Pull requests welcome — please run `gitleaks`, `pip-audit`, and
  `bandit` locally before submitting (the same checks CI runs).

---

## License

Apache License 2.0. See [LICENSES.md](LICENSES.md) for the full text
and a third-party dependency audit.

---

> Need the Chinese documentation? See [README.zh-TW.md](README.zh-TW.md)
> and [操作說明.md](操作說明.md).
