"""Operator CLI for AutoTest.

Usage:
    docker compose exec backend python -m app.cli create-admin
    docker compose exec backend python -m app.cli create-admin --username alice --email alice@example.com
    AUTOTEST_ADMIN_USERNAME=alice AUTOTEST_ADMIN_PASSWORD=... \\
        docker compose exec -T backend python -m app.cli create-admin --non-interactive

    docker compose exec backend python -m app.cli seed-casbin

Subcommands:
    create-admin
        Create a superuser (is_superuser=True, attached to the Admin role and
        default organization if they exist). Reads username / password / email
        from CLI flags, then environment variables (AUTOTEST_ADMIN_USERNAME /
        AUTOTEST_ADMIN_PASSWORD / AUTOTEST_ADMIN_EMAIL), then interactive prompt.
        Refuses to create a user that already exists; refuses passwords shorter
        than 8 characters.

    seed-casbin
        Idempotent seed of the casbin_rule table from the DB (Role +
        OrgMembership + ProjectMember). Re-runnable any time(全表
        truncate-and-rewrite),不會破壞既有 enforcer state。也可在切開
        CASBIN_SHADOW_ENABLED=True 之前先跑一次,確保 shadow 比對有資料。
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from typing import Optional


MIN_PASSWORD_LENGTH = 8


def _read_value(
    cli_value: Optional[str],
    env_key: str,
    prompt_label: str,
    *,
    secret: bool = False,
    non_interactive: bool = False,
    default: Optional[str] = None,
) -> str:
    if cli_value:
        return cli_value
    env_value = os.environ.get(env_key, "").strip()
    if env_value:
        return env_value
    if non_interactive:
        if default is not None:
            return default
        raise SystemExit(
            f"--non-interactive set but {env_key} is empty and no --{prompt_label} flag provided"
        )
    suffix = f" [{default}]" if default else ""
    prompt = f"{prompt_label}{suffix}: "
    if secret:
        value = getpass.getpass(prompt)
    else:
        value = input(prompt)
    value = value.strip() or (default or "")
    return value


async def _create_admin(
    username: str,
    password: str,
    email: str,
) -> None:
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.auth.security import hash_password
    from app.models import User, Role, Organization

    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if existing is not None:
            raise SystemExit(f"User '{username}' already exists; refusing to overwrite.")

        admin_role = (
            await session.execute(select(Role).where(Role.name == "Admin"))
        ).scalar_one_or_none()
        default_org = (
            await session.execute(select(Organization).where(Organization.slug == "default"))
        ).scalar_one_or_none()

        user = User(
            username=username,
            display_name=username,
            email=email or None,
            password_hash=hash_password(password),
            role_id=admin_role.id if admin_role else None,
            organization_id=default_org.id if default_org else None,
            is_superuser=True,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        print(f"Created superuser: {username}")
        if admin_role is None:
            print("  (note: Admin role was not present; user has is_superuser=True only)")
        if default_org is None:
            print("  (note: default organization was not present; user has organization_id=None)")


def cmd_create_admin(args: argparse.Namespace) -> None:
    username = _read_value(
        args.username, "AUTOTEST_ADMIN_USERNAME", "username",
        non_interactive=args.non_interactive,
    )
    if not username:
        raise SystemExit("username is required")

    password = _read_value(
        args.password, "AUTOTEST_ADMIN_PASSWORD", "password",
        secret=True, non_interactive=args.non_interactive,
    )
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )

    email = _read_value(
        args.email, "AUTOTEST_ADMIN_EMAIL", "email (optional)",
        non_interactive=args.non_interactive, default="",
    )

    asyncio.run(_create_admin(username, password, email))


async def _seed_casbin() -> dict[str, int]:
    """從 DB 重新計算 Casbin policy + grouping rules 並寫入 casbin_rule 表。

    強制 init enforcer(即使 ``CASBIN_ENABLED=False``),這樣可以在「正式
    切開 gate 之前」先把 policy 灌好,operator 再去翻 env。
    """
    from app.auth import casbin as _casbin
    from app.auth.casbin_sync import rebuild_all_policies
    from app.database import AsyncSessionLocal

    # CLI 進程沒走過 lifespan,enforcer 不會自動初始化;force 是給 CLI 用的。
    _casbin.init_enforcer(force=True)
    try:
        async with AsyncSessionLocal() as session:
            return await rebuild_all_policies(session)
    finally:
        _casbin.shutdown_enforcer()


def cmd_seed_casbin(args: argparse.Namespace) -> None:
    counts = asyncio.run(_seed_casbin())
    print(
        f"casbin seed done: wrote {counts.get('p', 0)} policy lines + "
        f"{counts.get('g', 0)} grouping(g) lines"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="AutoTest operator CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_admin = sub.add_parser("create-admin", help="Create a superuser account.")
    p_admin.add_argument("--username", help="Username (env: AUTOTEST_ADMIN_USERNAME)")
    p_admin.add_argument("--password", help="Password (env: AUTOTEST_ADMIN_PASSWORD; prefer interactive prompt)")
    p_admin.add_argument("--email", help="Optional email (env: AUTOTEST_ADMIN_EMAIL)")
    p_admin.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail instead of prompting when values are missing.",
    )
    p_admin.set_defaults(func=cmd_create_admin)

    p_casbin = sub.add_parser(
        "seed-casbin",
        help="Idempotent re-seed of the casbin_rule table from DB state.",
    )
    p_casbin.set_defaults(func=cmd_seed_casbin)

    return parser


def main(argv: Optional[list[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
