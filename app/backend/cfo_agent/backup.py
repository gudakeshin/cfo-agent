"""Scheduled backups of each tenant's database, each one checked before it is trusted.

    python -m cfo_agent.backup run        # back up every tenant now
    python -m cfo_agent.backup list
    python -m cfo_agent.backup verify FILE

Set BACKUP_DIR to turn the daily job on (BACKUP_CRON, default 02:30; BACKUP_KEEP newest
kept per tenant). Layout: BACKUP_DIR/<tenant or "default">/cfo-<UTC time>.sqlite|.dump
plus a .json note (size, checksum, row counts, whether it passed its check).

SQLite is copied with SQLite's online backup, safe while the app writes. PostgreSQL uses
`pg_dump --format=custom` (the pg_dump binary must be installed where the worker runs).
A backup is only counted once it has been checked:
  * SQLite: it is opened, `PRAGMA integrity_check` must say ok, and every table must hold at
    least as many rows as the live database did when the backup began;
  * PostgreSQL: `pg_restore --list` must read it and show the data of the core tables.
For PostgreSQL, also restore into a scratch database now and then: only that proves the
data comes back. Backups contain client data: put BACKUP_DIR on an encrypted volume."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

logger = logging.getLogger(__name__)

CORE_TABLES = ("digests", "digest_items", "weighting_versions", "kra_versions")


class BackupError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tenant_dir(root: str | Path) -> Path:
    from cfo_agent.tenancy import current_tenant_id

    d = Path(root) / (current_tenant_id() or "default")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _live_counts() -> dict[str, int]:
    from cfo_agent.db import get_engine
    from cfo_agent.models import Base

    out: dict[str, int] = {}
    with get_engine().connect() as conn:
        for t in Base.metadata.sorted_tables:
            try:
                out[t.name] = int(conn.execute(text(f'SELECT COUNT(*) FROM "{t.name}"')).scalar() or 0)
            except Exception:  # noqa: BLE001 — a table this database does not have (yet)
                conn.rollback()
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── taking and checking ──────────────────────────────────────────────────────

def _backup_sqlite(dest: Path) -> None:
    from cfo_agent.db import get_engine

    raw = get_engine().raw_connection()
    try:
        dst = sqlite3.connect(dest)
        try:
            raw.driver_connection.backup(dst)
        finally:
            dst.close()
    finally:
        raw.close()


def _pg_env() -> tuple[dict, str]:
    from cfo_agent.db import get_engine

    u = get_engine().url
    env = {**os.environ}
    if u.host:
        env["PGHOST"] = u.host
    if u.port:
        env["PGPORT"] = str(u.port)
    if u.username:
        env["PGUSER"] = u.username
    if u.password:
        env["PGPASSWORD"] = u.password  # in the environment, never on the command line
    return env, u.database or ""


def _backup_postgres(dest: Path) -> None:
    if not shutil.which("pg_dump"):
        raise BackupError("pg_dump is not installed on this machine")
    env, dbname = _pg_env()
    r = subprocess.run(["pg_dump", "--format=custom", "--no-owner", "--file", str(dest), dbname],
                       env=env, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise BackupError(f"pg_dump failed: {r.stderr.strip()[:300]}")


def verify_file(path: Path, live_counts: dict[str, int] | None = None) -> dict:
    """Check a backup is readable and complete. Returns its row counts (SQLite) or table list
    (PostgreSQL); raises BackupError if it fails."""
    if path.suffix == ".sqlite":
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "restore.sqlite"
            shutil.copy2(path, copy)  # restore into a scratch copy and open it, as a restore would
            conn = sqlite3.connect(copy)
            try:
                try:
                    verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
                except sqlite3.DatabaseError as e:
                    raise BackupError(f"not a readable SQLite database: {e}") from None
                if verdict != "ok":
                    raise BackupError(f"integrity check failed: {verdict}")
                # Virtual tables (the vector index) need an extension to open; their data lives in
                # ordinary shadow tables, which are checked like any other.
                tables = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                    "AND sql NOT LIKE 'CREATE VIRTUAL TABLE%'")]
                counts = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
            finally:
                conn.close()
        for t in CORE_TABLES:
            if t not in counts:
                raise BackupError(f"table {t} is missing from the backup")
        for t, n in (live_counts or {}).items():
            if t in counts and counts[t] < n:
                raise BackupError(f"table {t} has {counts[t]} rows in the backup but {n} in the live database")
        return {"tables": len(counts), "rows": counts}
    if path.suffix == ".dump":
        if not shutil.which("pg_restore"):
            raise BackupError("pg_restore is not installed on this machine")
        r = subprocess.run(["pg_restore", "--list", str(path)], capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise BackupError(f"pg_restore cannot read the dump: {r.stderr.strip()[:300]}")
        listed = [ln for ln in r.stdout.splitlines() if "TABLE DATA" in ln]
        missing = [t for t in CORE_TABLES if not any(f" {t} " in ln for ln in listed)]
        if missing:
            raise BackupError(f"the dump holds no data for: {', '.join(missing)}")
        return {"tables": len(listed)}
    raise BackupError(f"unknown backup type: {path.name}")


def backup_current_tenant(root: str | Path, keep: int = 14) -> Path:
    from cfo_agent.db import get_engine

    d = _tenant_dir(root)
    sqlite = get_engine().dialect.name == "sqlite"
    dest = d / f"cfo-{_now():%Y%m%dT%H%M%SZ}.{'sqlite' if sqlite else 'dump'}"
    live = _live_counts() if sqlite else None
    try:
        (_backup_sqlite if sqlite else _backup_postgres)(dest)
        os.chmod(dest, 0o600)
        detail = verify_file(dest, live)
    except Exception:
        dest.unlink(missing_ok=True)  # a backup that failed its check is not kept as if it were one
        raise
    note = {"created_at": _now().isoformat(), "size": dest.stat().st_size, "sha256": _sha256(dest), "verified": True, **detail}
    dest.with_suffix(dest.suffix + ".json").write_text(json.dumps(note, indent=1))
    _prune(d, keep)
    return dest


def _prune(d: Path, keep: int) -> None:
    files = sorted(p for p in d.iterdir() if p.suffix in (".sqlite", ".dump"))
    for old in files[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
        old.with_suffix(old.suffix + ".json").unlink(missing_ok=True)


def last_backup_age_hours() -> float | None:
    """Hours since the newest verified backup of the current tenant, or None if there is none."""
    from cfo_agent.settings import get_settings

    root = get_settings().backup_dir
    if not root:
        return None
    d = _tenant_dir(root)
    newest = None
    for note in d.glob("*.json"):
        try:
            n = json.loads(note.read_text())
        except (OSError, ValueError):
            continue
        if n.get("verified"):
            ts = datetime.fromisoformat(n["created_at"])
            newest = ts if newest is None or ts > newest else newest
    return None if newest is None else (_now() - newest).total_seconds() / 3600


def backup_job() -> str:
    """Scheduled entry point (once per tenant, through the job queue)."""
    from cfo_agent.settings import get_settings

    s = get_settings()
    if not s.backup_dir:
        return "skipped: BACKUP_DIR is not set"
    return str(backup_current_tenant(s.backup_dir, s.backup_keep))


# ── command line ─────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cfo_agent.backup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="back up every tenant now")
    sub.add_parser("list", help="show backups and their age")
    v = sub.add_parser("verify", help="check one backup file")
    v.add_argument("file")
    args = ap.parse_args(argv)

    from cfo_agent.settings import global_settings
    from cfo_agent.tenancy import for_each_tenant

    g = global_settings()
    if args.cmd == "verify":
        try:
            print(json.dumps(verify_file(Path(args.file)), indent=1)[:2000])
            print("OK")
            return 0
        except BackupError as e:
            print(f"FAILED: {e}", file=sys.stderr)
            return 1
    if not g.backup_dir:
        print("BACKUP_DIR is not set", file=sys.stderr)
        return 1
    failures = []

    def one():
        from cfo_agent.tenancy import current_tenant_id

        name = current_tenant_id() or "default"
        if args.cmd == "run":
            try:
                print(f"{name}: {backup_current_tenant(g.backup_dir, g.backup_keep)}")
            except Exception as e:  # noqa: BLE001
                failures.append(name)
                print(f"{name}: FAILED: {e}", file=sys.stderr)
        else:
            age = last_backup_age_hours()
            print(f"{name}: " + ("no verified backup" if age is None else f"last verified backup {age:.1f} h ago"))

    from cfo_agent.db import init_db

    init_db()
    for_each_tenant(one)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
