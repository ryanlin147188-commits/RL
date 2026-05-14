# RL — Test Automation Platform

> 🌐 **Languages**: **English** · [繁體中文](README.zh-TW.md)

> **One self-hosted platform covering the entire test lifecycle — and AI agents that can actually run tests for you.**
> Built on industry-standard open-source (Robot Framework + Playwright + Appium), replacing the scattered toolchain of Selenium IDE + Postman + Jira + TestRail + Allure.
> **Apache 2.0, self-host on your own network, AI assistant that drives a browser to generate executable test cases.**

[![License](https://img.shields.io/badge/License-Apache%202.0-green.svg)](LICENSES.md)
[![Robot Framework](https://img.shields.io/badge/Engine-Robot%20Framework%207.x-blue.svg)](https://robotframework.org/)
[![Docker](https://img.shields.io/badge/Deploy-Docker%20Compose-2496ED.svg)](https://docs.docker.com/compose/)
[![Stack](https://img.shields.io/badge/Stack-FastAPI%20%2B%20PostgreSQL%20%2B%20SeaweedFS-0a7e07.svg)](#tech-stack)
[![AI](https://img.shields.io/badge/AI-Hermes%20ACP%20%2B%20mem0%20%2B%20MCP%20%2B%2011%20providers-7c3aed.svg)](#ai-native)

---

## 30-second pitch

| Your problem | RL's answer |
|---|---|
| **"QA writes Selenium, PM lives in Confluence, bugs stay in Jira, reports rot in Allure — five disconnected silos."** | One platform: cases / scheduling / execution / reports / defects / backlog / RTM / todo / groups / versions, all linked. |
| **"Commercial SaaS is $20–$100 per user/month — 50 users = $5K–$60K/year."** | **Zero license fees** (Apache 2.0). Self-host on your network. **No user-based pricing — ever.** |
| **"Compliance: test data, screenshots, video, AI conversations cannot leave our network."** | Fully self-hosted (PostgreSQL / SeaweedFS / Valkey). AI can run on local Ollama / LM Studio. **Data never leaves your infrastructure.** |
| **"Writing cases is slow, AI-generated cases don't actually run, and humans waste hours converting them."** | ✨ **AI assistant emits validated `steps_json`** (executable units, not free text). One click to apply and run. **MCP mode** lets the AI drive a real browser to generate cases. |
| **"Playwright recordings are dead linear scripts — no variables, no branches, no conditions."** | ✨ **Dynamic expressions / Capture step / If-ElseIf-Else / vision-enhanced recording** — converted to Robot Framework 5.0 IF/ELSE/END automatically. |
| **"We want CI/CD integration but the SaaS API is rate-limited and the enterprise tier is a fortune."** | **API-first** + Swagger UI. `/api/executions` is open with no rate limits. |
| **"After we buy SaaS X, the case export is either a proprietary format or an empty shell."** | Cases = Markdown. Environments = `.env`. Robot Framework files use the standard syntax. **100 % portable.** |

---

## 🔥 v1.1.8 — Auth router actually goes through fastapi-users (v1.1.7 was wired but unused)

Straight talk: v1.1.7 imported fastapi-users, wrote the integration module,
and shipped four alembic migrations, but on the request path **only the
PasswordHelper was actually invoked**. UserManager / JWTStrategy /
`current_active_user` were dead code, and all 260 `Depends(get_current_user)`
sites still went through hand-rolled dependencies. v1.1.8 finishes the
cut-over. Grep makes it visible:

```
$ grep -rn "from fastapi_users" backend/app/
backend/app/auth/security.py                    PasswordHelper
backend/app/auth/fastapi_users_integration.py   (core module)
backend/app/auth/dependencies.py                current_active_user et al.
backend/app/routers/auth.py                     UserManager + get_jwt_strategy
```

What's actually cut over:

- **All 260 `Depends(get_current_user)` go through fastapi-users**:
  `dependencies.py::get_current_user` is now a thin alias for
  `fastapi_users_integration.current_active_user`. Every request runs
  `_fa_current_active_user` (the fastapi-users built-in) →
  `UsernameSubJWTStrategy.read_token` → `UserManager.get_by_username` → DB
  lookup. Our `must_change_password` gate is layered on top by the wrapper.
- **Login goes through `UserManager.authenticate_by_username`**, which
  wraps username lookup + bcrypt verify + **constant-time dummy hash for
  non-existent users** (prevents the timing-attack leak that the v1.1.7-era
  handcrafted login lacked) + **bcrypt → argon2 progressive rehash** (a
  successful verify that advises an upgrade persists the new hash, so
  each login migrates one user).
- **Access tokens minted by `JWTStrategy.write_token`**:
  `UsernameSubJWTStrategy` overrides `read_token` / `write_token` to use
  `sub=username` rather than the fastapi-users default UUID id, keeping
  SPA / Casbin / log identifiers consistent. Refresh tokens stay
  hand-rolled (fastapi-users 13 has no refresh-token concept).
- **Admin user CRUD via `UserManager`**: `POST /auth/users` uses
  `password_helper.hash()` + `on_after_register()`; `PUT /auth/users/{u}`
  and `POST /auth/users/{u}/reset-password` use `UserManager._update()`;
  `DELETE /auth/users/{u}` uses `UserManager.delete()`.

Intentionally NOT moved to fastapi-users: JWT decode in `app.middleware`
(it still needs token-revocation cache check + ContextVar setup +
active_org cookie resolution, none of which fastapi-users addresses);
refresh token + `must_change_password` gate + `org_id` cookie
(proprietary); Casbin RBAC (fastapi-users docs explicitly say "we don't do
authorization").

---

## 🔥 v1.1.7 — FastAPI Users primitives wired + schema migration (stack ready, hot path not yet cut)

Eight commits and four alembic migrations on `feat/fastapi-users` move the
auth backend off hand-rolled bcrypt + JWT + OAuth onto the standard
fastapi-users stack. The SPA didn't change. Existing admin and 100+ users
survive the migration intact — full design rationale lives in
`memory/fastapi-users-migration-state.md`.

- **Phase 1 — Foundation**: `backend/app/auth/fastapi_users_integration.py`
  wires `UserManager`, `JWTStrategy`, `PasswordHelper`,
  `SQLAlchemyUserDatabase`, `BearerTransport`. `token_audience` is left
  empty so both auth paths sign mutually-verifiable JWTs and sessions don't
  get evicted during cutover.
- **Phase 2 — Users schema**: alembic `0027` adds `users.id` UUID with
  `gen_random_uuid()::text` default; existing rows are backfilled.
- **Phase 3 — Shadow FK columns**: alembic `0028` adds nullable `user_id`
  UUID shadow columns to the six FK sites (project_members ×2,
  org_memberships ×2, group_memberships, password_reset_tokens) and
  JOIN-backfills them. The migration refuses to leave a NOT-NULL source
  with a NULL backfill — orphan rows surface as RuntimeError.
- **Phase 4 — PasswordHelper cutover**: `security.py` swaps passlib for
  pwdlib via `PasswordHelper`. argon2 for new hashes, bcrypt verify
  fallback for existing `$2b$` rows. 30+ call sites of `hash_password` /
  `verify_password` keep their signature.
- **Phase 5 — Dual-write listener**: `backend/app/auth/user_id_dualwrite.py`
  registers SQLAlchemy `before_insert` hooks on the four FK models so new
  rows auto-populate `user_id` from `username` — no caller has to know the
  shadow column exists. Unresolvable usernames raise instead of silently
  inserting NULL.
- **Phase 6 — OAuth migrated to httpx-oauth**: Zoho client moves from
  authlib `AsyncOAuth2Client` to `httpx-oauth.BaseOAuth2` (the OAuth2
  client family fastapi-users is built around). Behavior is identical;
  future migration to fastapi-users' OAuth router won't need another
  rewrite.
- **Phase 7 — Promote users.id to PK**: alembic `0029` drops the six FKs
  bound to `users_pkey`, swaps the PK from `username` to `id`, then
  recreates the six FKs with explicit `REFERENCES users(username)` so
  they bind to the new `uq_users_username` unique constraint. Application
  code is untouched — JWT `sub` is still username, Casbin policy subjects
  are still username, SPA URL paths are unchanged, `User.username == X`
  lookups still hit a unique index.
- **Phase 8 — Cleanup**: passlib and the bcrypt 4.x pin drop out of
  `requirements.txt` (pulled transitively by fastapi-users 13's pwdlib).
  SPA version bumps from v1.1.1 → v1.1.7.

### Deploy notes

1. `git pull && docker compose build backend && docker compose up -d backend` —
   alembic 0027–0029 run in order; existing users get UUIDs.
2. Backend startup logs should show four "Running upgrade" lines. Confirm
   with `SELECT username, id FROM users;` — every row has a UUID.
3. Existing SSO sessions, admin tokens, SPA logins all survive — no
   forced re-login.
4. To verify the dual-write listener: create a user from the admin modal,
   then `SELECT username, user_id FROM org_memberships WHERE
   username='<new user>'` — `user_id` should equal the user's `users.id`.

---

## 🔥 v1.1.6 — Per-project role permission override + three-field first-login modal

Targeted at the "solo-owner platform + multi-collaborator projects" scenario,
this release adds **per-project role permission overrides** so the same
`Project-Tester` role can have different effective permissions in different
projects without cloning the whole role.

- **Per-project override table** `project_role_permissions(project_id, role_id,
  permissions_json)`. In the SPA, 設定 → 專案協作成員 panel now shows a new
  「本專案角色權限」section listing the 4 project-scope roles with their
  effective permission count + override badge; click 編輯 to flip individual
  permission checkboxes for this project only.
- **Casbin sync writes alias roles** `<role>@<short_pid>` to `casbin_rule`
  for any (project, role) that has an override row. The 42 `require_casbin`
  call-sites stay unchanged — enforce automatically matches the alias's
  p rules in the specific `project:<pid>` domain.
- **First-login profile modal** expanded to three fields (display_name + email
  + new password) via new endpoint `POST /api/auth/profile-setup`. Triggered
  the same way as before (`users.must_change_password=True`).
- **`create_user` defaults `must_change_password=True`** so any user that an
  admin creates locally will be walked through profile setup on first login,
  guaranteeing display_name / email get filled in.
- New API:
  - `GET /api/projects/{pid}/role-permissions` — list effective permissions
    + override badge for all project-scope roles
  - `PUT /api/projects/{pid}/role-permissions/{role_id}` — upsert override
  - `DELETE /api/projects/{pid}/role-permissions/{role_id}` — reset to default
  - `POST /api/auth/profile-setup` — first-login three-field write
- Migration `0026_project_role_permissions` creates the new table with
  `UNIQUE(project_id, role_id)` and `ON DELETE CASCADE` from both FKs.

### Recommended collaboration SOP

| Persona | Global `users.role_id` | `ProjectMember.role_id` | Use case |
|---|---|---|---|
| Platform owner (you) | NULL + `is_superuser=True` | — | Full platform write |
| Customer PM | NULL | `Project-Reviewer` | View plans, approve |
| External QA | NULL | `Project-Tester` (+ override if needed) | Write cases, run tests |
| Read-only stakeholder | NULL | `Project-Viewer` | Dashboards, reports |

External collaborators sign up with `role_id=NULL` and can see nothing;
the platform owner invites them per-project via 「加入現有使用者」 and
optionally fine-tunes role permissions via 「本專案角色權限」. Removing them
from a project deletes only the `ProjectMember` row — no spillover to other
projects.

---

## 🔥 v1.1.5 — Casdoor sidecar dropped, in-process authlib takes over

After spending two minor releases (v1.1.3 / v1.1.4) running Casdoor as a side-
car, hitting subpath SPA whitescreens, session-cookie config trapdoors, and
silent `enable_signin_session=false` defaults, IAM is now back to running
entirely inside the FastAPI process.

* **Casdoor sidecar removed.** Compose service / configs / `casdoor` Postgres DB
  / 14 backend modules all gone. `docker compose ps` no longer shows
  `autotest-casdoor` or `autotest-casdoor-init`. The `casdoor/` config dir is
  deleted.
* **OIDC handled in-process with `authlib`** (`authlib>=1.3,<2`,
  `AsyncOAuth2Client`). New routes `GET /api/auth/{provider}/login` and
  `/callback` walk the OAuth code flow directly with the IdP. Currently
  only `zoho` is wired; adding Google / Microsoft / Okta is one 30-line
  `OIDCProvider` dataclass per provider in
  [backend/app/auth/oidc.py](backend/app/auth/oidc.py).
* **Token format reverted to HS256 in-house JWT** (same as v1.1.2). Backend
  mints its own token after OIDC handshake; `decode_token` no longer
  attempts RS256/JWKS dual-mode (`PyJWT[crypto]` → `PyJWT`, smaller image).
* **Local password endpoints resurrected**: `POST /auth/login` /
  `/auth/forgot-password` / `/auth/reset-password` / `/auth/change-password`
  / `POST,PUT,DELETE /auth/users/...` / `/settings/roles` POST/PUT/DELETE/clone
  back to live code (all were HTTP 410 in v1.1.3–v1.1.4). SPA modals
  (`pmCreateUserModal` / `pmEditUserModal` / `pmResetPwdModal` / `roleModal`)
  re-wired to the original local handlers.
* **Migrations**: `0024_rename_oidc_columns` renames `users.casdoor_user_id`
  → `users.oidc_subject` and adds `users.oidc_provider` with a `(provider,
  subject)` partial unique index. `0025_recreate_password_reset_tokens`
  restores the table that 0023 dropped.
* **Casbin retained, no behavior change.** Enforcer still runs in-process
  with `casbin_rule` table as source of truth. The 5-min reconcile beat is
  removed (no Casdoor to sync from); `schedule_user_resync` mutation hooks
  remain for immediate Casbin grant rebuild after role / membership changes.
* **Default after v1.1.5 deploy**:
  | URL | Account | Password |
  |---|---|---|
  | `http://<host>/` (帳密登入) | `admin` | `admin123` (forced-change on first login) |
  | "使用 Zoho 登入" 按鈕 | (your Zoho account) | — |

### Activating Zoho SSO

```bash
# 1. https://api-console.zoho.com → Add Client → Server-based Applications
#    Authorized Redirect URIs: http://<your-host>/api/auth/zoho/callback
#    (this is YOUR backend now, no longer Casdoor:8001/callback)
# 2. Add to .env:
echo "ZOHO_CLIENT_ID=<client_id>"     >> .env
echo "ZOHO_CLIENT_SECRET=<secret>"     >> .env
echo "ZOHO_REDIRECT_URL=http://<host>/api/auth/zoho/callback" >> .env
# 3. Restart backend:
docker compose up -d --force-recreate backend
# 4. Refresh SPA login page — orange "使用 Zoho 登入" button appears.
```

---

## 🔥 v1.1.4 — Zoho OIDC login via Casdoor (deprecated, replaced in v1.1.5)

- New "**使用 Zoho 登入**" shortcut button on the SPA login overlay (orange,
  below the "使用 Casdoor 登入" button). One click → `/api/auth/casdoor/login?provider=zoho-corp`
  → Casdoor → Zoho Accounts → back to your `/api/auth/callback`, no extra
  click on Casdoor's login page.
- `GET /api/auth/casdoor/login` now accepts `provider=<name>` query — passed
  through to Casdoor's authorize URL so Casdoor can skip its own login form
  and 302 straight to the upstream IdP. Versions of Casdoor that don't honor
  the param degrade gracefully (Casdoor login page just shows the Zoho
  button instead).
- Backend JIT provisioning unchanged — Casdoor unifies the Zoho identity
  into a Casdoor user row, so the JWT we receive always has `sub = <Casdoor uuid>`,
  not Zoho's raw `sub`. `provision_user_from_casdoor_claims` already
  treats `casdoor_user_id` as the stable key for dedup.

### Activating Zoho login (operator runbook, ~15 min)

```bash
# 1. https://api-console.zoho.com → Add Client → Server-based Applications
#    Authorized Redirect URI: http://<your-host>:8001/callback
# 2. Copy the Client ID + Client Secret out (shown once).

# 3. Add as a Casdoor Provider via Casdoor admin UI (http://<host>:8001/providers)
#    Name: zoho-corp · Category: OAuth · Type: Custom · Sub type: OAuth
#    Auth URL:     https://accounts.zoho.com/oauth/v2/auth
#    Token URL:    https://accounts.zoho.com/oauth/v2/token
#    UserInfo URL: https://accounts.zoho.com/oauth/user/info
#    Scopes:       AaaServer.profile.READ email openid
#    User mapping: id=ZUID, displayName=Display_Name, email=Email

# 4. Attach the provider to the application:
docker compose exec postgres psql -U admin -d casdoor -c \
  "UPDATE application SET providers='[{\"name\":\"zoho-corp\",\"canSignUp\":true,\"canSignIn\":true,\"canUnlink\":true,\"prompted\":false,\"rule\":\"None\",\"signupGroup\":\"\"}]'::jsonb WHERE name='app-built-in';"

# 5. Refresh the SPA login page — the orange "使用 Zoho 登入" button is live.
```

> **No email-domain restriction** is enforced by default — any Zoho account
> can JIT in. The local default role is `Project-Viewer` (read-only),
> and with no `project_members` row the user can't see any project. Add
> domain restriction in the Casdoor provider's `emailRegex` if you need
> tighter gating.

---

## 🔥 v1.1.3 — Casdoor + Casbin IAM cutover

### 🔐 SSO / Identity — Casdoor takes over
- New **Casdoor IAM sidecar** at `/casdoor/*` (opt-in via `--profile casdoor`) owns users / organizations / applications / SSO providers — federation to Google / GitHub / SAML / LDAP now configurable from the admin UI
- Login flow: SPA → `GET /api/auth/casdoor/login` → 302 → Casdoor authorize → `/api/auth/callback` → backend sets httpOnly cookies (`access_token` / `refresh_token` / `active_org_id`) → redirects with `#casdoor_login=1` so the SPA hydrates user info via `/api/auth/me`
- Dual-mode JWT verify: backend tries RS256 (JWKS-cached) first then falls back to HS256 — Casdoor tokens and legacy tokens both work during cutover
- `users.casdoor_user_id` (partial unique index) + `users.token_generation` columns added (migration `0021`)

### 🛡 Authorisation — Casbin in-process enforcer
- `pycasbin` 1.36.3 + `casbin-sqlalchemy-adapter` 1.4.0 running in the FastAPI process; policies persisted to a `casbin_rule` table the adapter auto-creates
- RBAC-with-domains model (`app/auth/casbin_model.conf`) using `keyMatch2` for `<resource>:*` wildcards
- New `require_casbin(P.X)` dependency with the same signature as the old `require_permission` — all 44 router call-sites switched (5 routers, mechanical)
- Sync layer **flattens 3-level role resolution** (ProjectMember > OrgMembership > User) into plain `g` rules; reload via `python -m app.cli seed-casbin`
- Opt-in via `CASBIN_ENABLED=True` — when False, `require_casbin` falls back to the legacy `list[str]` check so the cutover is rollback-safe
- Shadow mode (`CASBIN_SHADOW_ENABLED=True`) compares Casbin verdicts against legacy `require_permission` and logs divergence to `app.auth.permissions.shadow` for offline diff review

### 🧹 Legacy auth endpoints decommissioned (HTTP 410 + `moved_to` hint)
- `POST /api/auth/login`, `/auth/forgot-password`, `/auth/reset-password`, `/auth/change-password` — Casdoor's own forms now handle these
- `POST /api/auth/users` + `PUT` + `DELETE` + reset-password — admin user CRUD moved to `/casdoor/users`
- Role CRUD `/api/settings/roles` POST / PUT / DELETE / clone — moved to `/casdoor/roles`
- Old OIDC router (`/auth/oidc/login`, `/auth/oidc/callback`) unmounted; `/auth/oidc/providers` kept as a `200 []` stub so the SPA doesn't 404
- Tables dropped: `oidc_providers` (migration `0022`), `password_reset_tokens` (migration `0023`)
- SPA modals (`roleModal` / `pmCreateUserModal` / `pmEditUserModal` / `pmResetPwdModal`) now `window.open('/casdoor/...')` instead of opening locally

### 🔁 Hardening — webhook + 5-min reconcile
- `POST /api/auth/casdoor-webhook` accepts Casdoor's `add-user` / `update-user` / `delete-user` / `update-role` events; verified by `X-Casdoor-Webhook-Token` shared secret + Valkey `SET NX` idempotency (1h window)
- Celery beat task `tasks.casdoor_reconcile.run` every **5 minutes** as a fallback: pulls `/api/get-users` + `/api/get-roles` from Casdoor, diffs against local `users` + `org_memberships`, calls `rebuild_all_policies()` to refresh `casbin_rule`
- All mutations write an `audit_logs` row (method=`SYNC`/`WEBHOOK`, `change_summary` JSON for diff replay)
- Celery worker entrypoint adds `-B` so beat runs in the same container

### Default credentials after v1.1.3 deploy
| System | URL | Username | Password |
|---|---|---|---|
| App SPA | `http://<host>/` | (via Casdoor SSO) | — |
| **Casdoor admin** | `http://<host>/casdoor/` | `admin` | `admin123` |

### Activating Casdoor on an existing deployment
```bash
# 1. start sidecar (first boot uses Casdoor's built-in org + admin user)
docker compose --profile casdoor up -d casdoor

# 2. grab clientId / clientSecret from the application table
docker compose exec postgres psql -U admin -d casdoor \
  -c "SELECT client_id, client_secret FROM application WHERE name='app-built-in';"

# 3. add redirect URI for your host
docker compose exec postgres psql -U admin -d casdoor -c \
  "UPDATE application SET redirect_uris='[\"http://<host>/api/auth/callback\"]' WHERE name='app-built-in';"

# 4. flip the gates + supply the credentials
cat >> .env <<EOF
CASDOOR_ENABLED=True
CASDOOR_ORG=built-in
CASDOOR_APP=app-built-in
CASDOOR_CLIENT_ID=<step 2>
CASDOOR_CLIENT_SECRET=<step 2>
CASDOOR_REDIRECT_URL=http://<host>/api/auth/callback
CASBIN_ENABLED=True
CASDOOR_RECONCILE_ENABLED=True
EOF
docker compose up -d --force-recreate backend celery frontend

# 5. seed Casbin policies from the existing DB state (idempotent, re-runnable)
docker compose exec backend python -m app.cli seed-casbin
```

---

## 🔥 v1.1.2 — Recent Updates

### 🛡 Self-hosted Trace Viewer + HTTPS
- Frontend image bundles Playwright trace viewer at `/trace-viewer/` (extracted from `playwright-core@1.49.1` at build time)
- nginx serves HTTPS on port 443 with a build-time self-signed cert (10-year validity); cert downloadable via `http://<host>/install-cert/server.crt`. One-shot macOS trust install:
  ```bash
  curl -o /tmp/autotest.crt http://<host>/install-cert/server.crt && \
  sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain /tmp/autotest.crt
  ```
- Don't want to install the cert? The report's **Trace Viewer button now auto-downloads the .zip + opens `trace.playwright.dev`** — drag the file in, no setup needed
- COOP / COEP / CORP / `application/manifest+json` all wired so SharedArrayBuffer + Service Worker run in cross-origin-isolated mode
- APISIX `artifact_routes` CORS opened to `**`

### 🔁 Execution flow hardening
- **Precondition continuity**: the setup chain inlines into the main case's steps and runs in the **same docker container** — cookies / browser context / storage carry over from setup to main
- **Per-testcase step attribution**: step logs split back to source testcase ids with local indices starting at 0 (no more "Step 7 / Empty" when the main case only has 3 steps)
- **Short-polling** container wait (every 2s) replaces long-poll `container.wait()` — sidesteps docker-socket-proxy haproxy 10m timeout
- Two-tier timeout: `RUNNER_CONTAINER_TIMEOUT_SEC` (default 1800s) + `ROBOT_SUBPROCESS_TIMEOUT_SEC` (1680s); SIGTERM with 30s grace before SIGKILL so RF Teardown can finalize video/trace
- **Goto** uses `wait_until=domcontentloaded timeout=30s` — no more hanging forever when SPA XHR never returns
- **Click overlay cleanup**: pre-click JS dismisses modal backdrops, sidebar/drawer overlays, toast containers (Bootstrap / MUI / Ant Design / SweetAlert / CDK / metismenu / offcanvas)
- Wait timeout 60s → 20s — cascade-failure no longer freezes video for an hour
- AppiumLibrary conditional import (only when `Mobile.*` steps exist) avoids `Get Text` clash with Browser Library
- Robot listener: **first-error-wins** — cascade failures don't overwrite root cause with "Variable not found"
- Cancel API also kills orphan runner containers + writes a synthetic step log so cancelled reports aren't blank
- Full-page screenshots (`fullPage=True`) for Pre/Post Action — captures entire scrollable page

### 🧪 Test case editor
- **Copy testcase** button (green) — duplicates `ac_text` / `setup_text` / `steps_json` / `ddt_json` into the same parent, auto-numbered ("副本", "副本 (2)", ...)
- **Bulk step delete** — select-all checkbox + per-row checkbox + red "Delete N selected" button
- **Step reorder**: drag-handle replaced by **▲ / ▼ arrow buttons** (boundary auto-disabled)
- **Precondition link editor** under "Pre-Setup": dropdown picker + enable toggle + remove (wires to `testcase_precondition_links`)
- Auto-create-case failures now pop `alert()` dialogs (no SCENARIO selected / no steps captured / API didn't return id / exception)
- **Goto action** added to dropdown alongside Navigate (backend treats them identically)
- **Test Execution Console** is now an in-flow flex panel with ESC-to-close — no longer overlays test steps

### 🧠 Multi-agent runtime
- New `users.preferred_agent` column (migration `0019`) — switch between Hermes (default) and OpenClaw
- **OpenClaw runtime now accepts regular OpenAI API keys** — sidecar passes the key as `OPENAI_API_KEY` to `openclaw agent --local`. Graceful fallback to Hermes when no token / sidecar unreachable
- AI Token UI removed "Ollama / LM Studio" and "OpenClaw (ChatGPT subscription)" provider options; backend enforces with HTTP 400 on POST/PUT

### 🔒 Auth fixes
- Force-password-change modal: background polls receiving `403 must_change_password` no longer clear tokens while the modal is open
- `GET /api/users/me/preferred-agent` no longer sends `Authorization: Bearer ` (empty) — fixed broken `window.getAccessToken` ternary
- Bulk-selection state on the test-case list now persists across pages with a "已勾選 N 筆(跨頁保留)" badge

---

## Quick Start (Docker, ~5 minutes)

**Prerequisites**: Docker 24+ and Docker Compose v2.23+. Works on Linux, macOS, and Windows (Docker Desktop). All commands below work identically on every platform — no platform-specific deploy scripts needed.

```bash
git clone https://github.com/ryanlin147188-commits/RL_TMP.git
cd RL_TMP

# 1) Generate .env with random secrets (skip if you already have one).
docker compose --profile init run --rm bootstrap

# 2) Pre-build the four spawn-time images (Robot runner / web recorder /
#    API recorder / MCP). These run as per-session containers, not long-
#    lived services, but the images must exist before the backend can
#    `docker run` them. First build takes ~5–10 min.
docker compose --profile spawnable build

# 3) Start the main stack.
docker compose up -d --build

# 4) (Optional) override the seeded default admin password BEFORE the first
#    backend start. If skipped, the seed uses 'admin123' and forces a rotation
#    on first login. See docs/ops/bootstrap.md for details.
# echo "AUTOTEST_DEFAULT_ADMIN_PASSWORD=Op3rator-Init" >> .env
```

After backend starts, log in with `admin` / `admin123` (or your override).
The first login is gated by a forced password change. Self-service registration
is disabled — additional users are created from **設定 → 專案協作成員**.

**Default URLs after a successful boot**

| Service | URL |
|---|---|
| Web UI | <http://localhost> |
| REST API (Swagger) | exec into backend: `docker compose exec backend curl localhost:8000/docs` (port 8000 is no longer exposed; see [SECURITY.md](SECURITY.md)) |
| Logs | `docker compose logs -f`; VictoriaLogs stays internal |

### Daily ops

| Want to… | Command |
|---|---|
| See running containers | `docker compose ps` |
| Tail logs | `docker compose logs -f` |
| Stop (preserve data) | `docker compose down` |
| Reset (DESTRUCTIVE — wipes DB + S3) | `docker compose down -v` |
| Enable observability stack (internal Prometheus + Jaeger) | add `--profile obs` to up/down |
| Enable backend debug mode | `AUTOTEST_DEBUG=True docker compose up -d --force-recreate backend` |

---

## <a id="ai-native"></a> AI-native: the platform writes and runs tests for itself

RL is **not** a traditional test tool with a ChatGPT box bolted on. v1.1 ships **four AI pipelines** that treat the LLM as a first-class citizen of the platform:

### 1. Hermes AI assistant → executable cases + persistent memory
The chat is now backed by [hermes-agent](https://github.com/NousResearch/hermes-agent) running as an isolated ACP subprocess per user. Ask `"Test the cart flow from add-to-cart through checkout"` and the assistant returns:

- A schema-validated `steps_json` array (not free text — the platform can run it immediately).
- **Persistent semantic memory** via [mem0](https://github.com/mem0ai/mem0) — per-user pgvector store, automatic fact extraction post-conversation, and an MCP `search_memory` tool the LLM can invoke mid-conversation to recall past preferences.
- Multi-turn tool calling with a unified schema across OpenAI, Anthropic (Claude), and Google (Gemini).
- Apply to the current case, or open a new SCENARIO for a brand-new case.

### 2. AI drives a real browser (Playwright MCP)
Wired to [Model Context Protocol](https://modelcontextprotocol.io/) and [Playwright MCP](https://github.com/microsoft/playwright-mcp), the AI becomes an executable agent:

- **Per-user MCP containers** — each user gets an isolated Chromium; sessions don't collide.
- **Multi-turn tool-calling loop** — LLM → call browser tool → read screenshot → decide next action → repeat until done.
- **Instant abort** — hitting "Stop" cancels the asyncio task immediately; no zombie containers.
- **Idle sweeper** — background task reaps idle MCP containers so resources don't leak.
- **Use case** — say *"Open our website, click the support button, fill in the form"* → the AI clicks through and emits a runnable case.

### 3. Vision-enhanced recording
A finished trace can be enriched by any vision-capable LLM (GPT-4o / Claude 3.5 Sonnet / Gemini):

- Extracts screenshots from `trace.zip`, ships them with the action sequence to the LLM.
- The LLM infers user intent and adds **multi-condition assertions, Capture variables, and If/ElseIf branches**.
- Results are shown in a **diff view** so you can accept / reject step by step — the original recording is never silently overwritten.
- Every accepted suggestion is written to the audit log.

### 4. Persistent semantic memory (mem0 sidecar)
A dedicated `mem0` sidecar (FastAPI + pgvector) gives every user their own long-term memory:

- **Pre-hook recall** — every `send_message` first runs `mem0.search` and injects top-5 past memories into the prompt as `<recalled_memory>`. The LLM doesn't have to ask twice.
- **Post-hook write** — fact extraction runs fire-and-forget after each turn (LLM extracts atomic facts, mem0 dedups and stores).
- **`search_memory` MCP tool** — the LLM can also actively query memory mid-conversation (e.g., *"do I have a saved staging URL for this client?"*).
- **Per-user isolation** — `org_id:username` partition key; X-Mem0-User-Id header set by backend, not the LLM. No tenant data leakage.
- **Graceful degrade** — circuit breaker, 5s timeouts, friendly error string on cache miss; main chat never blocks on memory.

### 11 LLM providers + fully local options

| Cloud | Local / self-hosted |
|---|---|
| OpenAI · Anthropic · DeepSeek · Groq · OpenRouter · Together AI · Mistral · xAI · Google Gemini | Ollama · LM Studio · any OpenAI-compatible endpoint |

**Memory (mem0) provider matrix** — fact extraction uses the user's chat LLM; vector embedding needs an embedder API:

| Primary chat LLM | Embedder for memory | Notes |
|---|---|---|
| OpenAI | OpenAI `text-embedding-3-small` | Same token, no extra setup |
| Gemini | Gemini `text-embedding-004` | Same token, no extra setup |
| Anthropic (Claude) | OpenAI / Gemini fallback (any token in same org) | Anthropic has no embedder API — backend auto-picks the cheapest embedder-capable token in your org. Add an OpenAI key alongside Claude to unlock memory features. |
| Anthropic alone (no fallback) | — | Memory features auto-disabled; main chat works normally. |

---

## What's in the box

| Layer | Capability |
|---|---|
| **Test-case management** | Project / Feature / Platform / Page / Scenario / TestCase tree, Markdown editing, version history, RTM (requirements traceability), defect tracking, WBS, sprint planning |
| **Authoring** | Visual recorder (Playwright), API recorder (mitmproxy), AI chat → `steps_json`, manual editor, dynamic expressions, capture steps, IF / ELSE branches |
| **Execution** | Robot Framework 7.x + Playwright headless, isolated runner containers per execution, real-time WebSocket logs, screenshots / video / trace per step, scheduling (cron), tags, retry on flaky |
| **Review / Approval** | Generic approval workflow for testcases / documents / scripts / reports — pending / approved / rejected tabs, audit trail, reason field per decision; **bulk reassign reviewer** for triaging |
| **Cross-entity assignment** ✨ | Unified `assigned_to` schema across 6 entity types (defect / todo / testcase / requirement / document / review). Group-typed assignees auto fan-out notifications to all members (including nested subgroups). Bulk reassign up to 200 entities per call. **"My Work" inbox** aggregates personal workload from all entities. |
| **Multi-entity Kanban** ✨ | Tab-based view (defect / todo / testcase / requirement / document / review / All) with **"My assignments / All" toggle**. **Unified 7-column status board** (`New / Assigned / InProgress / InReview / ReworkRequired / Verified / Closed`) shared by all entities — same workflow semantics, same colors. **Drag-and-drop status change** for defect / todo / requirement (PUT-based, optimistic update + rollback on failure). Cards show priority / blocked / module / type badges, assignee, due date, overdue ring, and a one-click reassign button. |
| **Backlog with cross-entity links** ✨ | TodoLink supports linking a backlog item to any of **10 target types** including TestVersion. Link kinds (`verifies` / `blocks` / `duplicates` / `relates_to`) carry RTM semantics. Reverse-view (linked todos) is rendered inside every entity's detail modal. **Bulk-from-targets** creates one tracking todo per selected target in a single call. |
| **Settings — RBAC, members, groups, invites** ✨ | All 6 management panes (permissions / roles / groups / member-binding / org members / project members) ship search, sort, pagination params, and bulk operations. Role clone (with permission diff), permission reverse-lookup ("who has this perm"), group→project bridge (add a whole group as project members in one call), and full invite lifecycle (send / resend / extend / revoke / bulk). |
| **Observability** | Live console, Allure-style reports, defect linking, history charts (Chart.js), audit log middleware (SOC 2 baseline), Fluent Bit + VictoriaLogs (per-container streams), opt-in Prometheus + Jaeger via `--profile obs` |
| **API gateway** | nginx → APISIX → backend (single internal entry); request-id, CORS, per-IP rate-limit, circuit breaker; backend port never exposed to host |
| **Storage** | All uploads land in SeaweedFS via S3-compatible API (`STORAGE_BACKEND=s3` enforced at startup; container-local fallback removed) |
| **Integration** | REST API + Swagger, OIDC SSO, slowapi rate limiting, Fernet field encryption (SMTP / AI keys), webhook on execution events |
| **Multi-tenant** | Organization model, JWT carries `org_id` + proactive refresh, RBAC scaffold (22 permission keys), email-domain auto-binding with **preview** before adopt, single-tenant stop-gap guards in v1.1 (see [SECURITY.md](SECURITY.md)) |

See [操作說明.md](操作說明.md) (Chinese) for an end-to-end user guide. English walkthroughs are tracked in [issue tracker](../../issues) — contributions welcome.

---

## 🆕 v1.1.1 — Assistant × Platform Action Tools × Real Browser

v1.1.0 wired Hermes ACP + mem0 in; v1.1.1 actually **closes the loop** so the
assistant can act on the platform end-to-end:

- **Platform MCP server (new)** — backend self-mounts `/platform-mcp/mcp` (FastMCP
  sub-app) exposing **27 action tools** to the Hermes LLM (projects / testcases /
  defects / documents / requirements / milestones / versions / plans / todos /
  recordings / executions). When a user says "create a Kapito project", the
  assistant calls `create_project` directly instead of asking about tech-stack
  details.
- **Per-user Playwright MCP** — Hermes provisioning auto-spins the
  `autotest-mcp` container; the LLM receives 22 `browser_*` tools (navigate /
  click / type / snapshot / get_images / etc.) and can actually drive a browser
  to explore a site, propose test cases, and verify them.
- **`platform_help(topic?)` knowledge tool** — a module-level catalog (not in
  mem0, to avoid polluting personal memory) the LLM queries to discover what
  the platform can do.
- **Execution chain** — `execute_testcase` / `get_execution_status` /
  `list_executions` let the assistant kick off and track a real docker-mode run
  in one breath.
- **Language follows the UI** — frontend fetch wrapper sends `Accept-Language`
  (zh-TW / en); backend injects a per-turn `<language_directive>` so toggling
  the locale takes effect **instantly**, no Hermes reprovision needed.
- **Assistant UI cleanup** — removed advanced toolbar items (scheduled
  tasks / LLM connection / pause-memory / case-toolbar); renamed "AI 助理" to
  "助理"; Enter no longer auto-sends — explicit send button only.
- **Recording chain** — `start_recording_session` creates the DB row via MCP;
  `convert_recording_to_steps` parses Playwright codegen / HAR into step arrays.
- **Bug-fix sweep** (shipped): `0005` migration ai_conversations index fix
  for fresh DBs; full-API auth 401/403 + must_change_password URL-clear flow;
  Hermes `POST /api/hermes/sessions` provider-mapping fix (OpenAI → custom +
  base_url + api_mode=chat_completions); Playwright MCP Streamable HTTP
  `initialize` handshake; Docker Desktop bind-mount stale-inode mitigation.
- **AI Token model list** — filters out whisper / dall-e / embedding / tts
  (non-chat models leak into OpenAI's `/v1/models`).
- **Two-tier platform-only sandbox** — `acp_lockdown.py` monkey-patch removes
  `web` / `terminal` / `file` / `code_execution` / `delegation` toolsets from
  the LLM's tool surface; the system prompt is a second defence telling the
  LLM to refuse anything that would leave the platform.

Upgrade notes: this release ships 18 alembic migrations (0001 → 0018). For a
**fresh DB**, just run `./deploy.sh` (lifespan calls `alembic upgrade head`).
For an **upgrade from v1.1.0**, stop the stack, `docker compose pull` /
`build`, then `up`; the backend will migrate on next start.

---

## What changed since v1.0 (7 consecutive UX rounds, A → G)

After the v1.0 baseline, seven focused UX-hardening rounds shipped to `main`. Every round is backend-additive (no breaking schema changes — the one column rename in tier D ships behind a reversible Alembic migration):

| Tier | Theme | Highlights |
|---|---|---|
| **A** | Settings panes baseline | search / sort / unified loading-empty-error states / cascade-aware delete confirms across 6 panes |
| **B** | Pagination + bulk operations | server-side pagination params, role usage stats, role clone, bulk role assignment |
| **C** | Cross-pane collaboration | permission reverse-lookup drawer, email-domain preview validator, full invite lifecycle UI, group→project bridge, multi-select add-member modal |
| **D** | Assignment system overhaul | TodoItem schema unified with the rest of the Assignable mixin; group fan-out completed for all 5 generic entity types; bulk reassign + stale assignment endpoints; **"My Work" inbox**; assignee picker with search + group fan-out preview + audit metadata + stale-cleanup CTA; 4 native `prompt()` chains replaced by a generic form-modal helper |
| **E** | Coverage close-out | bulk reassign rolled into testcase / review lists; `/api/assignments/me?entity_type=todo` enum fix |
| **F** | Multi-entity kanban | kanban shifts from defect-only to a 6-entity workspace, plugged into the new assignment system |
| **G** | Todo linking finalised | TodoLink supports TestVersion; `link_kind` semantics surfaced in UI (verifies / blocks / duplicates); reverse-view block in 5 detail modals; `POST /api/todos/bulk-from-targets`; link-creation notifications to entity assignees |
| **H** | **Unified status workflow + draggable kanban** | Eight entity status enums (defect / todo / requirement / review / test_plan / test_milestone / wbs / project) collapsed into the same 7-value canonical workflow: `New → Assigned → InProgress → InReview → (Verified \| ReworkRequired \| Closed)`. Two reversible Alembic migrations (`0011_unify_status` + `0012_unify_status_part2`) auto-convert existing rows; routers normalize legacy values; Python enum aliases keep old `.DRAFT / .APPROVED / .ACTIVE / …` references working. **Kanban supports drag-and-drop status change** for defect / todo / requirement; cards expose Blocked / Rejected close-reason / Priority / Module / Type / Assignee / Due Date badges. Sidebar nav drawer + home quick-nav reorganised into 5 sections (專案管理 / 測試設計 / 測試環境 / 執行中心 / 品質追蹤). |

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
┌──────────────┐ ┌──────────────┐ │  → Playwright MCP         │
│ SeaweedFS    │ │ Fluent Bit + │ │  (each in its own         │
│ (S3, media)  │ │ VictoriaLogs │ │  short-lived container)   │
└──────────────┘ └──────────────┘ └───────────────────────────┘

  ┌──────────────────────────────────────────────────────────┐
  │                AI sidecars (internal-only)               │
  │                                                          │
  │  hermes:7800 — Hermes ACP supervisor                     │
  │    └─ per-user ACP subprocess pool (idle-evict)          │
  │       └─ MCP HTTP client → mem0:7900/mcp/mcp             │
  │                                                          │
  │  mem0:7900  — semantic memory layer                      │
  │    ├─ FastAPI proxy + FastMCP `search_memory` tool       │
  │    └─ pgvector (mem0-postgres) — per-user partition      │
  └──────────────────────────────────────────────────────────┘
```

**Default compose**: 12 long-running services plus `seaweedfs-init` one-shot (postgres / valkey / seaweedfs / docker-proxy / backend / celery / frontend / apisix / fluent-bit / victoria-logs / **hermes** / **mem0** + mem0-postgres / seaweedfs-init).
**Profile-gated**: 2 obs services (Prometheus + Jaeger), 4 spawn-time images (`robot-runner` / `recorder` / `recorder-api` / `mcp` — built once, run per session by backend), 1 bootstrap (one-shot `.env` + Fernet/JWT/sidecar-auth generator).
**Bundle**: the same `docker-compose.yml` also supports preloaded app images via `docker compose up -d --no-build`.

---

## Production hardening

Before exposing RL to the internet:

- Set `ALLOWED_ORIGINS` to your front-end origin (never `*`).
- Override default secrets — `AUTOTEST_JWT_SECRET`, `AUTOTEST_FERNET_KEY`, `DB_PASSWORD`, `S3_ROOT_PASSWORD`. The bootstrap profile (`docker compose --profile init run --rm bootstrap`) generates random values on first run; rotate them on a schedule.
- Run behind HTTPS (e.g., a reverse proxy with Let's Encrypt or your own CA).
- Pin `RECORDER_IMAGE` and `ROBOT_RUNNER_IMAGE` to specific tags or sha256 digests — never `latest`.
- Schedule backups of the PostgreSQL and SeaweedFS volumes.
- Read [SECURITY.md](SECURITY.md) for the vulnerability disclosure policy.

---

## FAQ

**Q: Why not just use TestRail / Zephyr / qTest?**
Pricing scales linearly with seats, your test data lives in someone else's cloud, and the export formats are proprietary or empty shells. RL keeps everything on your infrastructure with standard open formats.

**Q: Why not just Robot Framework + a CI server?**
Authoring, scheduling, defect linking, RTM, vision-enhanced recording, and AI agents are not part of vanilla Robot Framework. RL composes them into one product so QA / PM / SRE share a single source of truth.

**Q: Can I disable AI features?**
Yes. AI features are opt-in via API keys stored encrypted in the database (Fernet). With no key, the chat / MCP / vision tabs are inert; the rest of the platform works exactly as a traditional test platform.

**Q: Is this enterprise-ready?**
v1.1 ships single-tenant stop-gap guards. Multi-tenant isolation, MFA, API tokens, and Helm charts are tracked on the roadmap (see Layer 3 of the [improvement plan](#)). For commercial deployments, please open an issue.

**Q: Apple Silicon (M1 / M2 / M3)?**
Works via Rosetta 2, but recorder containers run x86-64 and are 2–4× slower than on a native amd64 host. Native arm64 images are on the Layer 3 roadmap.

---

## Contributing

- Bug reports and feature requests: [open an issue](../../issues).
- Security vulnerabilities: see [SECURITY.md](SECURITY.md).
- Community guidelines: see [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
- Pull requests welcome — please run `gitleaks`, `pip-audit`, and `bandit` locally before submitting (the same checks CI runs).

---

## License

Apache License 2.0. See [LICENSES.md](LICENSES.md) for the full text and a third-party dependency audit.

---

> Need the original Chinese documentation? See [README.zh-TW.md](README.zh-TW.md) and [操作說明.md](操作說明.md).
