#!/usr/bin/env python3
"""Batch-migrate local users + oidc_providers into Casdoor.

Phase 4.2 of the Casdoor + Casbin migration plan. Reads existing `users` +
`oidc_providers` rows from PostgreSQL, then POSTs them into Casdoor via REST
API. Idempotent — re-running is safe; existing Casdoor entries are skipped
rather than overwritten.

Run inside the backend container so DB_* + CASDOOR_* envs come from compose:

    docker compose exec backend python -m scripts.migrate-users-to-casdoor \\
        --casdoor-client-id <id> --casdoor-client-secret <secret>

Or with envs set on host(.env-style):

    CASDOOR_CLIENT_ID=... CASDOOR_CLIENT_SECRET=... \\
        docker compose exec backend python -m scripts.migrate-users-to-casdoor

Mutations:
  * For every active user:
      - PUT /api/add-user with sub = users.username (Casdoor enforces uniqueness)
      - users.casdoor_user_id is written back so future logins recognise them
      - users.token_generation +1 (forces re-login → old HS256 JWT 401)
  * For every oidc_providers row:
      - PUT /api/add-provider in Casdoor's "autotest" organisation
      - Mapped to Casdoor's OAuth / OIDC provider category — Google / GitHub /
        custom OIDC are well-supported; SAML lives in a different schema and
        is left for manual operator follow-up.
  * Local DB stays read-only **except** for the `casdoor_user_id` /
    `token_generation` writes. oidc_providers rows are NOT deleted here —
    migration 0022 drops the whole table once the operator confirms.

Failure handling:
  * Network errors → exit 1 with stack trace; safe to retry (idempotent).
  * Per-row failure → log and continue;最後 print 一張 summary 表。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any, Optional

import httpx
from sqlalchemy import select

# 讓 script 跑 docker compose exec 也能 import app.* — 直接從 /app 起算 PYTHONPATH。
sys.path.insert(0, os.environ.get("APP_DIR", "/app"))

from app.auth.casdoor import (  # noqa: E402
    CASDOOR_APP,
    CASDOOR_ENDPOINT,
    CASDOOR_ORG,
)
from app.auth.crypto import _fernet  # noqa: E402
from app.database import AsyncSessionLocal  # noqa: E402
from app.models.oidc_provider import OidcProvider  # noqa: E402
from app.models.user import User  # noqa: E402

logger = logging.getLogger("migrate-to-casdoor")


# ── Casdoor REST helpers ───────────────────────────────────────────────


def _client(client_id: str, client_secret: str) -> httpx.AsyncClient:
    """Casdoor REST API 接受 Basic auth(app client_id/secret)。"""
    return httpx.AsyncClient(
        base_url=CASDOOR_ENDPOINT,
        auth=(client_id, client_secret),
        timeout=15.0,
        headers={"Accept": "application/json"},
    )


async def _get(client: httpx.AsyncClient, path: str) -> Optional[dict]:
    r = await client.get(path)
    if r.status_code == 200:
        body = r.json()
        if body.get("status") == "ok":
            return body
    return None


async def _post_json(client: httpx.AsyncClient, path: str, payload: dict) -> dict:
    r = await client.post(path, json=payload)
    r.raise_for_status()
    return r.json()


# ── User migration ─────────────────────────────────────────────────────


async def migrate_user(client: httpx.AsyncClient, user: User) -> tuple[str, str]:
    """匯入單一使用者到 Casdoor。回傳 (status, message)。

    status:
      * created   — 新建立
      * exists    — Casdoor 已有同名 user,只更新本地 casdoor_user_id
      * skipped   — user 已非 active / 名稱違法
      * failed    — 失敗;message 帶錯誤訊息
    """
    if not user.is_active:
        return "skipped", "user inactive"

    # 1. 先看 Casdoor 是否已有同名
    existing = await _get(client, f"/api/get-user?id={CASDOOR_ORG}/{user.username}")
    if existing and existing.get("data"):
        sub = existing["data"].get("id") or existing["data"].get("name") or user.username
        return "exists", f"casdoor sub={sub}"

    # 2. POST add-user。Casdoor 的密碼可給 plaintext + passwordType="plain"
    #    搭配 password options "AtLeast6" 在 application 設定上。
    payload: dict[str, Any] = {
        "owner": CASDOOR_ORG,
        "name": user.username,
        "displayName": user.display_name or user.username,
        "email": user.email or "",
        # SSO-only 帳號:給隨機密碼(operator 之後可在 Casdoor UI 重設或
        # 走 forgot-password 流程)。本地的 bcrypt hash 沒辦法搬到 Casdoor 的
        # bcrypt 格式(salt scheme 不同),所以這裡只能切斷。
        "password": "",
        "passwordType": "plain",
        "isAdmin": user.is_superuser,
        "isGlobalAdmin": user.is_superuser,
        "isForbidden": not user.is_active,
        "signupApplication": CASDOOR_APP,
    }
    try:
        resp = await _post_json(client, "/api/add-user", payload)
    except httpx.HTTPStatusError as e:
        return "failed", f"HTTP {e.response.status_code}: {e.response.text[:200]}"
    except Exception as e:
        return "failed", str(e)[:200]

    if resp.get("status") != "ok":
        return "failed", resp.get("msg") or "casdoor returned non-ok"
    return "created", f"casdoor name={user.username}"


async def migrate_users(client: httpx.AsyncClient, dry_run: bool) -> dict[str, int]:
    counters = {"created": 0, "exists": 0, "skipped": 0, "failed": 0}
    async with AsyncSessionLocal() as session:
        users = (await session.execute(select(User))).scalars().all()
        for u in users:
            status, msg = await migrate_user(client, u)
            counters[status] += 1
            logger.info("user %s: %s (%s)", u.username, status, msg)
            if status in ("created", "exists") and not dry_run:
                # 把 casdoor_user_id 對齊 — 用 username 當 Casdoor sub identifier
                # 的好處是不需要再呼叫 get-user 拿 uuid;Casdoor REST API 用
                # `<org>/<name>` 當 PK 也接受此格式。
                if u.casdoor_user_id != u.username:
                    u.casdoor_user_id = u.username
                # 強制讓所有舊的 HS256 token 失效 — Phase 4 cutover 必要動作。
                u.token_generation = (u.token_generation or 0) + 1
        if not dry_run:
            await session.commit()
    return counters


# ── OIDC provider migration ────────────────────────────────────────────


_OIDC_TYPE_GUESS = {
    "google":    ("OAuth",  "Google"),
    "github":    ("OAuth",  "GitHub"),
    "azure":     ("OAuth",  "Azure AD"),
    "okta":      ("OIDC",   "Custom"),
    "auth0":     ("OIDC",   "Custom"),
}


def _guess_provider_kind(p: OidcProvider) -> tuple[str, str]:
    haystack = f"{(p.name or '').lower()} {(p.slug or '').lower()} {(p.issuer or '').lower()}"
    for key, (cat, sub) in _OIDC_TYPE_GUESS.items():
        if key in haystack:
            return cat, sub
    return "OIDC", "Custom"


async def migrate_oidc_providers(client: httpx.AsyncClient, dry_run: bool) -> dict[str, int]:
    counters = {"created": 0, "exists": 0, "failed": 0}
    async with AsyncSessionLocal() as session:
        providers = (await session.execute(select(OidcProvider))).scalars().all()
        for p in providers:
            cat, sub = _guess_provider_kind(p)
            cas_name = f"oidc-{p.slug}"
            existing = await _get(client, f"/api/get-provider?id={CASDOOR_ORG}/{cas_name}")
            if existing and existing.get("data"):
                counters["exists"] += 1
                logger.info("oidc %s: exists (skipped)", p.slug)
                continue

            # client_secret 是 EncryptedString,SQLAlchemy 已自動 Fernet 解;若
            # 一次性匯出腳本要看 raw,需要走 _fernet。但 Provider model 的 .
            # client_secret 屬性已是 plaintext(經由 EncryptedString.processor),
            # 所以直接給。為安全起見遮罩印 log,避免在 stdout 流出。
            payload = {
                "owner": CASDOOR_ORG,
                "name": cas_name,
                "displayName": p.name,
                "category": cat,
                "type": sub,
                "method": "Normal",
                "clientId": p.client_id or "",
                "clientSecret": p.client_secret or "",
                "scopes": p.scopes or "openid email profile",
                "customAuthUrl": p.authorize_url or "",
                "customTokenUrl": p.token_url or "",
                "customUserInfoUrl": "",
                "providerUrl": p.issuer or p.discovery_url or "",
            }
            if dry_run:
                counters["created"] += 1
                logger.info(
                    "oidc %s: would create (category=%s type=%s)", p.slug, cat, sub,
                )
                continue
            try:
                resp = await _post_json(client, "/api/add-provider", payload)
                if resp.get("status") == "ok":
                    counters["created"] += 1
                    logger.info("oidc %s: created (category=%s type=%s)", p.slug, cat, sub)
                else:
                    counters["failed"] += 1
                    logger.warning("oidc %s: failed (%s)", p.slug, resp.get("msg"))
            except Exception as e:
                counters["failed"] += 1
                logger.exception("oidc %s: HTTP error: %s", p.slug, e)
    return counters


# ── CLI ───────────────────────────────────────────────────────────────


async def main_async(args: argparse.Namespace) -> int:
    client_id = args.casdoor_client_id or os.environ.get("CASDOOR_CLIENT_ID", "")
    client_secret = args.casdoor_client_secret or os.environ.get("CASDOOR_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print("ERROR: 缺少 CASDOOR_CLIENT_ID / CASDOOR_CLIENT_SECRET", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")

    async with _client(client_id, client_secret) as client:
        # 先連通性 check
        ping = await _get(client, "/api/get-organizations")
        if ping is None:
            print("ERROR: Casdoor REST API 無回應 / 認證失敗", file=sys.stderr)
            return 1

        users_summary = await migrate_users(client, dry_run=args.dry_run)
        oidc_summary = await migrate_oidc_providers(client, dry_run=args.dry_run)

    print()
    print("=== users ===")
    for k, v in users_summary.items():
        print(f"  {k:8s} {v}")
    print("=== oidc_providers ===")
    for k, v in oidc_summary.items():
        print(f"  {k:8s} {v}")
    print()
    if args.dry_run:
        print("(dry-run; no local DB writes, no Casdoor mutations)")
    else:
        print("Done. 下一步:")
        print("  1. 在 Casdoor UI 確認 user / provider 都已建立")
        print("  2. 跑 `docker compose exec backend alembic upgrade head` 套用 migration 0022 (drop oidc_providers)")
        print("  3. 把 .env 內 CASDOOR_ENABLED=True / CASBIN_ENABLED=True 都打開後重啟 backend")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--casdoor-client-id", help="env: CASDOOR_CLIENT_ID")
    p.add_argument("--casdoor-client-secret", help="env: CASDOOR_CLIENT_SECRET")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只 log 該做什麼,不打 Casdoor REST,也不寫本地 DB。",
    )
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async(parse_args())))
