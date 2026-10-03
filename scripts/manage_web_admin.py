#!/usr/bin/env python3
"""Manage personal web panel logins (bot/web/access.py).

The owner always uses ADMIN_USERNAME/ADMIN_PASSWORD from .env (full access).
Extra people get personal logins bound to a bot role — the role's permission
bits decide which admin views they may open. Roles themselves stay
owner-only, so nobody can escalate their own rights.

Usage (from the repo root, venv active):
  python scripts/manage_web_admin.py create LOGIN --role ADMIN
  python scripts/manage_web_admin.py create LOGIN --role ADMIN --password '...'
  python scripts/manage_web_admin.py passwd LOGIN
  python scripts/manage_web_admin.py disable LOGIN
  python scripts/manage_web_admin.py enable LOGIN
  python scripts/manage_web_admin.py list
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import select  # noqa: E402

from bot.database.main import Database  # noqa: E402
from bot.database.models.main import Permission, Role, WebAdmin  # noqa: E402
from bot.web.access import hash_password  # noqa: E402


def _read_password() -> str:
    for _ in range(3):
        first = getpass.getpass("Password (min 8 chars): ")
        if len(first) < 8:
            print("Too short, try again.")
            continue
        second = getpass.getpass("Repeat password: ")
        if first != second:
            print("Mismatch, try again.")
            continue
        return first
    raise SystemExit("Aborted.")


async def _role_id(name: str) -> int:
    async with Database().session() as s:
        row = (await s.execute(select(Role).where(Role.name == name.upper()))).scalars().first()
    if row is None:
        raise SystemExit(f"No bot role named {name!r}.")
    if not Permission.has_any_admin_perm(int(row.permissions or 0)):
        raise SystemExit(f"Role {name!r} has no admin permissions — pointless login.")
    return row.id


async def cmd_create(login: str, role: str, password: str | None) -> None:
    if not login or len(login) > 64:
        raise SystemExit("Login must be 1-64 chars.")
    role_id = await _role_id(role)
    password = password or _read_password()
    if len(password) < 8:
        raise SystemExit("Password must be at least 8 chars.")
    async with Database().session() as s:
        exists = (await s.execute(select(WebAdmin).where(WebAdmin.login == login))).scalars().first()
        if exists is not None:
            raise SystemExit(f"Login {login!r} already exists (use passwd/disable).")
        s.add(WebAdmin(login=login, password_hash=hash_password(password), role_id=role_id))
    print(f"Web login {login!r} created with role {role.upper()}.")


async def cmd_passwd(login: str, password: str | None) -> None:
    password = password or _read_password()
    if len(password) < 8:
        raise SystemExit("Password must be at least 8 chars.")
    async with Database().session() as s:
        row = (await s.execute(select(WebAdmin).where(WebAdmin.login == login))).scalars().first()
        if row is None:
            raise SystemExit(f"No login {login!r}.")
        row.password_hash = hash_password(password)
    print(f"Password updated for {login!r}.")


async def cmd_active(login: str, active: bool) -> None:
    async with Database().session() as s:
        row = (await s.execute(select(WebAdmin).where(WebAdmin.login == login))).scalars().first()
        if row is None:
            raise SystemExit(f"No login {login!r}.")
        row.is_active = active
    print(f"Login {login!r} {'enabled' if active else 'disabled'}.")


async def cmd_list() -> None:
    async with Database().session() as s:
        rows = (await s.execute(select(WebAdmin, Role.name).outerjoin(
            Role, Role.id == WebAdmin.role_id).order_by(WebAdmin.login))).all()
    if not rows:
        print("No personal web logins (owner uses ADMIN_USERNAME from .env).")
        return
    for admin, role_name in rows:
        state = "active" if admin.is_active else "DISABLED"
        print(f"- {admin.login} [{state}] role={role_name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage web panel logins.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create")
    p.add_argument("login")
    p.add_argument("--role", default="ADMIN")
    p.add_argument("--password", default=None)
    p = sub.add_parser("passwd")
    p.add_argument("login")
    p.add_argument("--password", default=None)
    p = sub.add_parser("disable")
    p.add_argument("login")
    p = sub.add_parser("enable")
    p.add_argument("login")
    sub.add_parser("list")
    args = parser.parse_args()

    async def run():
        if args.cmd == "create":
            await cmd_create(args.login, args.role, args.password)
        elif args.cmd == "passwd":
            await cmd_passwd(args.login, args.password)
        elif args.cmd == "disable":
            await cmd_active(args.login, False)
        elif args.cmd == "enable":
            await cmd_active(args.login, True)
        elif args.cmd == "list":
            await cmd_list()

    asyncio.run(run())


if __name__ == "__main__":
    main()
