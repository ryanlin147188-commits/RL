# RL — Enterprise Test Automation Platform

> 🌐 **Languages**: **English** · [繁體中文](README.zh-TW.md)

> **One self-hosted platform covering the entire test lifecycle — and AI agents that can actually run tests for you.**
> Built on industry-standard open-source (Robot Framework + Playwright + Appium), replacing the scattered toolchain of Selenium IDE + Postman + Jira + TestRail + Allure.
> **Apache 2.0, self-host on your own network, AI assistant that drives a browser to generate executable test cases.**

[![License](https://img.shields.io/badge/License-Apache%202.0-green.svg)](LICENSES.md)
[![Robot Framework](https://img.shields.io/badge/Engine-Robot%20Framework%207.x-blue.svg)](https://robotframework.org/)
[![Docker](https://img.shields.io/badge/Deploy-Docker%20Compose-2496ED.svg)](https://docs.docker.com/compose/)
[![Stack](https://img.shields.io/badge/Stack-FastAPI%20%2B%20PostgreSQL%20%2B%20SeaweedFS-0a7e07.svg)](#tech-stack)
[![AI](https://img.shields.io/badge/AI-MCP%20%2B%20Vision%20%2B%2011%20providers-7c3aed.svg)](#ai-native)

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

# 4) Create the first admin user (no default account is shipped).
docker compose exec backend python -m app.cli create-admin
```

**Default URLs after a successful boot**

| Service | URL |
|---|---|
| Web UI | <http://localhost> |
| REST API (Swagger) | exec into backend: `docker compose exec backend curl localhost:8000/docs` (port 8000 is no longer exposed; see [SECURITY.md](SECURITY.md)) |
| Logs (VictoriaLogs vmui) | Internal by default; chain `docker-compose.dev.yml` to expose port 9428 |

### Daily ops

| Want to… | Command |
|---|---|
| See running containers | `docker compose ps` |
| Tail logs | `docker compose logs -f` |
| Stop (preserve data) | `docker compose down` |
| Reset (DESTRUCTIVE — wipes DB + S3) | `docker compose down -v` |
| Enable observability stack (Prometheus + Jaeger) | add `--profile obs` to up/down |
| Expose internal ports for local dev | `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d` |

---

## <a id="ai-native"></a> AI-native: the platform writes and runs tests for itself

RL is **not** a traditional test tool with a ChatGPT box bolted on. v1.0 ships **three AI pipelines** that treat the LLM as a first-class citizen of the platform:

### 1. AI Chat → executable cases in one click
Ask `"Test the cart flow from add-to-cart through checkout"` and the assistant returns:

- A schema-validated `steps_json` array (not free text — the platform can run it immediately).
- Built-in multi-turn tool calling with a unified schema across OpenAI, Anthropic, and Google.
- Apply to the current case, or open a new SCENARIO for a brand-new case.
- Falls back to traditional Markdown mode if the LLM refuses tool use, never hard-fails.

### 2. AI drives a real browser (MCP)
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

### 11 LLM providers + fully local options

| Cloud | Local / self-hosted |
|---|---|
| OpenAI · Anthropic · DeepSeek · Groq · OpenRouter · Together AI · Mistral · xAI · Google Gemini | Ollama · LM Studio · any OpenAI-compatible endpoint |

---

## What's in the box

| Layer | Capability |
|---|---|
| **Test-case management** | Project / Feature / Platform / Page / Scenario / TestCase tree, Markdown editing, version history, RTM (requirements traceability), defect tracking, WBS, sprint planning |
| **Authoring** | Visual recorder (Playwright), API recorder (mitmproxy), AI chat → `steps_json`, manual editor, dynamic expressions, capture steps, IF / ELSE branches |
| **Execution** | Robot Framework 7.x + Playwright headless, isolated runner containers per execution, real-time WebSocket logs, screenshots / video / trace per step, scheduling (cron), tags, retry on flaky |
| **Review / Approval** | Generic approval workflow for testcases / documents / scripts / reports — pending / approved / rejected tabs, audit trail, reason field per decision; **bulk reassign reviewer** for triaging |
| **Cross-entity assignment** ✨ | Unified `assigned_to` schema across 6 entity types (defect / todo / testcase / requirement / document / review). Group-typed assignees auto fan-out notifications to all members (including nested subgroups). Bulk reassign up to 200 entities per call. **"My Work" inbox** aggregates personal workload from all entities. |
| **Multi-entity Kanban** ✨ | Tab-based view (defect / todo / testcase / requirement / document / review / All) with **"My assignments / All" toggle**. Generic 3-column status mapping (TODO / In progress / Done) for the All tab; per-entity fine-grained status columns for single-entity tabs. Cards show assignee, overdue ring, and a one-click reassign button. |
| **Backlog with cross-entity links** ✨ | TodoLink supports linking a backlog item to any of **10 target types** including TestVersion. Link kinds (`verifies` / `blocks` / `duplicates` / `relates_to`) carry RTM semantics. Reverse-view (linked todos) is rendered inside every entity's detail modal. **Bulk-from-targets** creates one tracking todo per selected target in a single call. |
| **Settings — RBAC, members, groups, invites** ✨ | All 6 management panes (permissions / roles / groups / member-binding / org members / project members) ship search, sort, pagination params, and bulk operations. Role clone (with permission diff), permission reverse-lookup ("who has this perm"), group→project bridge (add a whole group as project members in one call), and full invite lifecycle (send / resend / extend / revoke / bulk). |
| **Observability** | Live console, Allure-style reports, defect linking, history charts (Chart.js), audit log middleware (SOC 2 baseline), Fluent Bit + VictoriaLogs (per-container streams), opt-in Prometheus + Jaeger via `--profile obs` |
| **API gateway** | nginx → APISIX → backend (single internal entry); request-id, CORS, per-IP rate-limit, circuit breaker; backend port never exposed to host |
| **Storage** | All uploads land in SeaweedFS via S3-compatible API (`STORAGE_BACKEND=s3` enforced at startup; container-local fallback removed) |
| **Integration** | REST API + Swagger, OIDC SSO, slowapi rate limiting, Fernet field encryption (SMTP / AI keys), webhook on execution events |
| **Multi-tenant** | Organization model, JWT carries `org_id` + proactive refresh, RBAC scaffold (22 permission keys), email-domain auto-binding with **preview** before adopt, single-tenant stop-gap guards in v1.0 (see [SECURITY.md](SECURITY.md)) |

See [操作說明.md](操作說明.md) (Chinese) for an end-to-end user guide. English walkthroughs are tracked in [issue tracker](../../issues) — contributions welcome.

---

## 🆕 What changed since v1.0 (7 consecutive UX rounds, A → G)

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
```

**Always-on**: 12 services in `docker-compose.yml` (postgres / valkey / seaweedfs / docker-proxy / backend / celery / frontend / apisix / fluent-bit / victoria-logs / seaweedfs-init one-shot).
**Profile-gated**: 2 obs services (Prometheus + Jaeger), 4 spawn-time images (`robot-runner` / `recorder` / `recorder-api` / `mcp` — built once, run per session by backend), 1 bootstrap (one-shot `.env` generator).
**Bundle**: image distribution via `docker-compose.bundle.yml` for air-gapped deployments.

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
v1.0 ships single-tenant stop-gap guards. Multi-tenant isolation, MFA, API tokens, and Helm charts are tracked on the roadmap (see Layer 3 of the [improvement plan](#)). For commercial deployments, please open an issue.

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
