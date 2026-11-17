"""Tenant registry: the list of clients, kept in a control database.

Set CONTROL_DATABASE_URL and manage tenants with the command line, no restart needed
(running processes pick changes up within TENANT_REGISTRY_TTL_S seconds):

    python -m cfo_agent.registry add acme --name "Acme Industries" \\
        --database env:ACME_DATABASE_URL --host acme.cfo.example.com \\
        --email-to cfo@acme.com --set max_llm_cost_usd_per_run=2
    python -m cfo_agent.registry list
    python -m cfo_agent.registry set acme --set anthropic_model=claude-sonnet-5-5
    python -m cfo_agent.registry unset acme anthropic_model
    python -m cfo_agent.registry disable acme        # stops routing, jobs and alerts; keeps the data
    python -m cfo_agent.registry import tenants.yaml  # one-off move from a TENANTS_FILE
    python -m cfo_agent.registry events [acme]

The registry stores references to secrets (`env:NAME`, `file:/path`), never the secrets
themselves. It never drops a tenant's database: a new tenant's database is created and
migrated the first time a process uses it. Every
change is recorded in the registry's event log.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from sqlalchemy import JSON, DateTime, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from cfo_agent.secret_refs import SecretError, has_inline_password, is_reference, mask, resolve_secret
from cfo_agent.tenancy import ID_RE, Tenant, parse_recipients

logger = logging.getLogger(__name__)

STATUSES = ("active", "disabled")


class RegistryError(ValueError):
    pass


# ── per-tenant overrides ─────────────────────────────────────────────────────
# Only these global settings can differ by tenant. Schedules (DIGEST_CRON…) and the
# scheduler time zone are deliberately absent: jobs run on one shared schedule today.

def _text(v):
    s = str(v).strip()
    if not s:
        raise RegistryError("value is empty")
    return s


def _non_negative_float(v):
    f = float(v)
    if f < 0:
        raise RegistryError("must be 0 or more")
    return f


def _non_negative_int(v):
    i = int(v)
    if i < 0:
        raise RegistryError("must be 0 or more")
    return i


def _one_of(*allowed):
    def check(v):
        s = str(v).strip()
        if s not in allowed:
            raise RegistryError(f"must be one of {', '.join(allowed)}")
        return s
    return check


def _json_object(v):
    obj = json.loads(v) if isinstance(v, str) else v
    if not isinstance(obj, dict):
        raise RegistryError("must be a JSON object")
    return json.dumps(obj, sort_keys=True)


OVERRIDABLE = {
    "anthropic_model": _text,
    "news_model": _text,
    "max_llm_cost_usd_per_run": _non_negative_float,  # 0 = no model calls, deterministic text only
    "max_llm_calls_per_run": _non_negative_int,
    "monthly_llm_budget_usd": _non_negative_float,  # 0 = no monthly ceiling
    "data_mode": _one_of("replay", "live"),
    "auth_mode": _one_of("off", "local", "oidc"),
    "oidc_issuer": _text,
    "oidc_client_id": _text,
    "oidc_client_secret": _text,  # a secret reference
    "oidc_scopes": _text,
    "oidc_roles_claim": _text,
    "oidc_role_map": _json_object,
    "oidc_default_role": _one_of("viewer", "cfo", "delegate", "admin"),
}


def clean_overrides(raw: dict) -> dict:
    out = {}
    for key, value in raw.items():
        if key not in OVERRIDABLE:
            raise RegistryError(f"'{key}' cannot be set per tenant. Allowed: {', '.join(sorted(OVERRIDABLE))}")
        try:
            out[key] = OVERRIDABLE[key](value)
        except RegistryError as e:
            raise RegistryError(f"{key}: {e}") from None
        except (TypeError, ValueError) as e:
            raise RegistryError(f"{key}: {e}") from None
    secret = out.get("oidc_client_secret")
    if secret is not None and not is_reference(secret):
        raise RegistryError("oidc_client_secret must be a reference (env:NAME or file:/path), not the secret itself")
    return out


# ── storage ──────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class TenantRow(Base):
    __tablename__ = "tenant_registry"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(16), default="active")
    hosts_json: Mapped[list] = mapped_column(JSON, default=list)
    database_ref: Mapped[str] = mapped_column(Text)
    users_file: Mapped[str | None] = mapped_column(String(512), nullable=True)
    email_to_json: Mapped[list] = mapped_column(JSON, default=list)
    email_from: Mapped[str | None] = mapped_column(String(256), nullable=True)
    settings_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class RegistryEvent(Base):
    __tablename__ = "tenant_registry_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(128), default="")
    at: Mapped[datetime] = mapped_column(DateTime, default=_now)


@lru_cache(maxsize=4)
def _sessionmaker(url: str) -> sessionmaker:
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs = {"connect_args": {"check_same_thread": False, "timeout": 30}}
    engine = create_engine(url.replace("postgresql://", "postgresql+psycopg://", 1), **kwargs)
    Base.metadata.create_all(engine)  # two small tables; the control database has no migrations of its own
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _control_url() -> str:
    from cfo_agent.settings import global_settings

    url = global_settings().control_database_url
    if not url:
        raise RegistryError("CONTROL_DATABASE_URL is not set")
    return url


def _session(url: str | None = None) -> Session:
    return _sessionmaker(url or _control_url())()


def _log(s: Session, tenant_id: str, action: str, detail: str = "") -> None:
    try:
        actor = getpass.getuser()
    except Exception:  # noqa: BLE001
        actor = ""
    s.add(RegistryEvent(tenant_id=tenant_id, action=action, detail=detail[:2000], actor=actor))


# ── operations ───────────────────────────────────────────────────────────────

def _check_database_ref(ref: str, *, resolve: bool) -> None:
    if has_inline_password(ref):
        logger.warning("Database URL contains a password; use env:NAME or file:/path so the registry holds no secret")
    if resolve:
        try:
            url = resolve_secret(ref)
        except SecretError as e:
            raise RegistryError(f"database: {e}") from None
        if not url.startswith(("sqlite", "postgres")):
            raise RegistryError("database must resolve to a sqlite:/// or postgresql:// URL")


def _check_hosts(s: Session, tenant_id: str, hosts: list[str]) -> list[str]:
    hosts = [h.strip().lower() for h in hosts if h.strip()]
    for other in s.scalars(select(TenantRow).where(TenantRow.id != tenant_id)):
        clash = set(hosts) & set(other.hosts_json or [])
        if clash:
            raise RegistryError(f"host {', '.join(sorted(clash))} already belongs to tenant {other.id}")
    return list(dict.fromkeys(hosts))


def add_tenant(tenant_id: str, *, name: str, database: str, hosts: list[str], email_to=None, email_from=None,
               users_file=None, overrides: dict | None = None, check_database: bool = True,
               url: str | None = None) -> TenantRow:
    if not ID_RE.match(tenant_id):
        raise RegistryError("id must be lowercase letters, digits and '-' (2 to 32 characters)")
    with _session(url) as s:
        if s.get(TenantRow, tenant_id):
            raise RegistryError(f"tenant {tenant_id} already exists (use set)")
        _check_database_ref(database, resolve=check_database)
        row = TenantRow(
            id=tenant_id, name=name or tenant_id, database_ref=database, hosts_json=_check_hosts(s, tenant_id, hosts),
            email_to_json=list(parse_recipients(email_to)), email_from=(email_from or None), users_file=users_file or None,
            settings_json=clean_overrides(overrides or {}),
        )
        s.add(row)
        _log(s, tenant_id, "add", f"name={row.name} hosts={row.hosts_json} database={mask(database)}")
        s.commit()
    _changed()
    return row


def update_tenant(tenant_id: str, *, url: str | None = None, **fields) -> TenantRow:
    """Change a tenant. Accepted: name, database, hosts, email_to, email_from, users_file,
    overrides (merged into the existing ones), check_database."""
    with _session(url) as s:
        row = s.get(TenantRow, tenant_id)
        if row is None:
            raise RegistryError(f"no tenant {tenant_id}")
        changed = []
        if fields.get("name"):
            row.name = fields["name"]
            changed.append("name")
        if fields.get("database"):
            _check_database_ref(fields["database"], resolve=fields.get("check_database", True))
            row.database_ref = fields["database"]
            changed.append(f"database={mask(fields['database'])}")
        if fields.get("hosts") is not None:
            row.hosts_json = _check_hosts(s, tenant_id, fields["hosts"])
            changed.append(f"hosts={row.hosts_json}")
        if fields.get("email_to") is not None:
            row.email_to_json = list(parse_recipients(fields["email_to"]))
            changed.append("email_to")
        if "email_from" in fields and fields["email_from"] is not None:
            row.email_from = fields["email_from"] or None
            changed.append("email_from")
        if "users_file" in fields and fields["users_file"] is not None:
            row.users_file = fields["users_file"] or None
            changed.append("users_file")
        if fields.get("overrides"):
            row.settings_json = {**(row.settings_json or {}), **clean_overrides(fields["overrides"])}
            changed.append("settings:" + ",".join(sorted(fields["overrides"])))
        if fields.get("unset"):
            keep = {k: v for k, v in (row.settings_json or {}).items() if k not in fields["unset"]}
            row.settings_json = keep
            changed.append("unset:" + ",".join(fields["unset"]))
        row.updated_at = _now()
        _log(s, tenant_id, "update", "; ".join(changed))
        s.commit()
    _changed()
    return row


def set_status(tenant_id: str, status: str, *, url: str | None = None) -> None:
    if status not in STATUSES:
        raise RegistryError(f"status must be one of {', '.join(STATUSES)}")
    with _session(url) as s:
        row = s.get(TenantRow, tenant_id)
        if row is None:
            raise RegistryError(f"no tenant {tenant_id}")
        row.status = status
        row.updated_at = _now()
        _log(s, tenant_id, "enable" if status == "active" else "disable")
        s.commit()
    _changed()


def remove_tenant(tenant_id: str, *, url: str | None = None) -> None:
    """Remove the registry entry. The tenant's own database is never touched."""
    with _session(url) as s:
        row = s.get(TenantRow, tenant_id)
        if row is None:
            raise RegistryError(f"no tenant {tenant_id}")
        s.delete(row)
        _log(s, tenant_id, "remove", "registry entry only; the tenant's database is untouched")
        s.commit()
    _changed()


def list_rows(url: str | None = None) -> list[TenantRow]:
    with _session(url) as s:
        return list(s.scalars(select(TenantRow).order_by(TenantRow.id)))


def events(tenant_id: str | None = None, limit: int = 50, url: str | None = None) -> list[RegistryEvent]:
    with _session(url) as s:
        q = select(RegistryEvent).order_by(RegistryEvent.id.desc()).limit(limit)
        if tenant_id:
            q = q.where(RegistryEvent.tenant_id == tenant_id)
        return list(s.scalars(q))


def load_active(url: str) -> dict[str, Tenant]:
    """The active tenants, with database secrets resolved. One tenant whose secret cannot
    be read is left out (and logged); it never takes the others down."""
    out: dict[str, Tenant] = {}
    for row in list_rows(url):
        if row.status != "active":
            continue
        try:
            db_url = resolve_secret(row.database_ref)
        except SecretError as e:
            logger.error("Tenant %s left out: database secret unavailable (%s)", row.id, e)
            continue
        out[row.id] = Tenant(
            row.id, row.name, db_url, tuple(row.hosts_json or []), row.users_file,
            email_to=tuple(row.email_to_json or []), email_from=row.email_from, settings=dict(row.settings_json or {}),
        )
    return out


def import_yaml(path: str, *, url: str | None = None) -> list[str]:
    """One-off: copy the tenants of a TENANTS_FILE into the registry (existing ids are skipped)."""
    import yaml

    data = yaml.safe_load(open(path, encoding="utf-8").read()) or {}
    done = []
    existing = {r.id for r in list_rows(url)}
    for t in data.get("tenants") or []:
        tid = str(t.get("id", ""))
        if tid in existing:
            continue
        add_tenant(
            tid, name=str(t.get("name") or tid), database=str(t["database_url"]), hosts=[str(h) for h in t.get("hosts") or []],
            email_to=t.get("email_to"), email_from=t.get("email_from"), users_file=t.get("users_file"),
            overrides=t.get("settings"), check_database=False, url=url,
        )
        done.append(tid)
    return done


def _changed() -> None:
    from cfo_agent.tenancy import invalidate_registry_cache

    invalidate_registry_cache()


# ── command line ─────────────────────────────────────────────────────────────

def _parse_sets(pairs: list[str] | None) -> dict:
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise RegistryError(f"--set expects key=value, got '{p}'")
        k, v = p.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _show(row: TenantRow) -> str:
    lines = [
        f"{row.id}  [{row.status}]  {row.name}",
        f"  hosts:    {', '.join(row.hosts_json or []) or '-'}",
        f"  database: {mask(row.database_ref)}",
        f"  email to: {', '.join(row.email_to_json or []) or '-'}" + (f"  (from {row.email_from})" if row.email_from else ""),
    ]
    if row.users_file:
        lines.append(f"  users:    {row.users_file}")
    for k, v in sorted((row.settings_json or {}).items()):
        lines.append(f"  setting:  {k} = {v}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cfo_agent.registry", description="Manage the tenant registry.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="add a tenant")
    a.add_argument("id")
    a.add_argument("--name", default="")
    a.add_argument("--database", required=True, help="database URL or a reference: env:NAME / file:/path")
    a.add_argument("--host", action="append", default=[], help="host name routed to this tenant (repeatable)")
    a.add_argument("--email-to", default="")
    a.add_argument("--email-from", default="")
    a.add_argument("--users-file", default="")
    a.add_argument("--set", action="append", metavar="KEY=VALUE", help="per-tenant setting override (repeatable)")
    a.add_argument("--no-check", action="store_true", help="do not resolve the database reference now")

    s_ = sub.add_parser("set", help="change a tenant")
    s_.add_argument("id")
    s_.add_argument("--name")
    s_.add_argument("--database")
    s_.add_argument("--host", action="append", help="replace the host list (repeatable)")
    s_.add_argument("--email-to")
    s_.add_argument("--email-from")
    s_.add_argument("--users-file")
    s_.add_argument("--set", action="append", metavar="KEY=VALUE")
    s_.add_argument("--no-check", action="store_true")

    u = sub.add_parser("unset", help="remove per-tenant setting overrides")
    u.add_argument("id")
    u.add_argument("keys", nargs="+")

    for name, helptext in (("enable", "route and run jobs for the tenant"), ("disable", "stop routing and jobs; keep all data"),
                           ("remove", "delete the registry entry (the tenant's database is untouched)"), ("show", "show one tenant")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("id")
    sub.add_parser("list", help="list tenants")
    e = sub.add_parser("events", help="recent registry changes")
    e.add_argument("id", nargs="?")
    i = sub.add_parser("import", help="copy the tenants of a TENANTS_FILE into the registry")
    i.add_argument("file")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "add":
            add_tenant(args.id, name=args.name, database=args.database, hosts=args.host, email_to=args.email_to or None,
                       email_from=args.email_from or None, users_file=args.users_file or None,
                       overrides=_parse_sets(args.set), check_database=not args.no_check)
            print(f"Added {args.id}. It is live within {_ttl():g}s in running processes; "
                  "its database is prepared on first use.")
        elif args.cmd == "set":
            update_tenant(args.id, name=args.name, database=args.database, hosts=args.host, email_to=args.email_to,
                          email_from=args.email_from, users_file=args.users_file, overrides=_parse_sets(args.set),
                          check_database=not args.no_check)
            print(f"Updated {args.id}.")
        elif args.cmd == "unset":
            update_tenant(args.id, unset=args.keys)
            print(f"Updated {args.id}.")
        elif args.cmd in ("enable", "disable"):
            set_status(args.id, "active" if args.cmd == "enable" else "disabled")
            print(f"{args.id}: {args.cmd}d.")
        elif args.cmd == "remove":
            remove_tenant(args.id)
            print(f"Removed {args.id} from the registry (its database was not touched).")
        elif args.cmd == "show":
            row = next((r for r in list_rows() if r.id == args.id), None)
            if row is None:
                raise RegistryError(f"no tenant {args.id}")
            print(_show(row))
        elif args.cmd == "list":
            rows = list_rows()
            print("\n\n".join(_show(r) for r in rows) if rows else "No tenants.")
        elif args.cmd == "events":
            for ev in events(args.id):
                print(f"{ev.at:%Y-%m-%d %H:%M:%S}  {ev.tenant_id:<16} {ev.action:<8} {ev.actor:<12} {ev.detail}")
        elif args.cmd == "import":
            done = import_yaml(args.file)
            print(f"Imported: {', '.join(done) if done else 'nothing new'}")
    except (RegistryError, SecretError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def _ttl() -> float:
    from cfo_agent.settings import global_settings

    return global_settings().tenant_registry_ttl_s


if __name__ == "__main__":
    sys.exit(main())
