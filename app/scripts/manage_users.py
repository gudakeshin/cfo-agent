#!/usr/bin/env python3
"""Manage local sign-in users (AUTH_MODE=local).

  python scripts/manage_users.py add cfo@client.com --name "A. Sharma" --role cfo
  python scripts/manage_users.py set-role cfo@client.com delegate
  python scripts/manage_users.py remove cfo@client.com
  python scripts/manage_users.py list

Passwords are prompted (never passed on the command line) and stored as
PBKDF2-SHA256 hashes in LOCAL_USERS_FILE (default backend/config/users.yaml).
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import yaml  # noqa: E402

from cfo_agent.auth import ROLES  # noqa: E402
from cfo_agent.security import hash_password  # noqa: E402
from cfo_agent.settings import get_settings  # noqa: E402


def _load(path: Path) -> dict:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    path.chmod(0o600)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("email")
    a.add_argument("--name", default="")
    a.add_argument("--role", choices=ROLES, required=True)
    r = sub.add_parser("set-role")
    r.add_argument("email")
    r.add_argument("role", choices=ROLES)
    p = sub.add_parser("reset-password")
    p.add_argument("email")
    d = sub.add_parser("remove")
    d.add_argument("email")
    sub.add_parser("list")
    args = ap.parse_args()

    path = get_settings().local_users_file
    data = _load(path)
    users = data.setdefault("users", [])
    by_email = {u["email"].lower(): u for u in users}
    email = getattr(args, "email", "").strip().lower()

    if args.cmd == "list":
        for u in users:
            print(f"{u['email']:<36} {u.get('role', '?'):<9} {u.get('name', '')}")
        return 0
    if args.cmd in ("add", "reset-password"):
        if args.cmd == "reset-password" and email not in by_email:
            print(f"No user {email}", file=sys.stderr)
            return 1
        pw = getpass.getpass("Password (min 12 chars): ")
        if len(pw) < 12 or pw != getpass.getpass("Repeat: "):
            print("Passwords must match and be at least 12 characters.", file=sys.stderr)
            return 1
        if args.cmd == "add":
            u = by_email.get(email) or {"email": email}
            u.update({"name": args.name or u.get("name") or email, "role": args.role})
            if email not in by_email:
                users.append(u)
        else:
            u = by_email[email]
        u["password_hash"] = hash_password(pw)
    elif args.cmd == "set-role":
        if email not in by_email:
            print(f"No user {email}", file=sys.stderr)
            return 1
        by_email[email]["role"] = args.role
    elif args.cmd == "remove":
        data["users"] = [u for u in users if u["email"].lower() != email]
    _save(path, data)
    print(f"Saved {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
