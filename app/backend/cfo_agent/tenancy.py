"""Multi-tenancy: one database per client (strongest isolation for finance data).

Off by default (single tenant). With TENANTS_FILE pointing at a YAML file:

    tenants:
      - id: acme
        name: Acme Industries
        database_url: postgresql://cfo:…@db/acme
        hosts: [acme.cfo.example.com]
        users_file: /config/acme-users.yaml   # optional, AUTH_MODE=local

every request is routed by its Host header to that tenant's database; unknown hosts
get 404. Session cookies are bound to the tenant that issued them, file-drop inboxes
live under CSV_INBOX_DIR/<tenant>/, caches are keyed per tenant, and the worker runs
every scheduled job once per tenant under that tenant's own scheduler lease.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_current: ContextVar[str | None] = ContextVar("tenant", default=None)
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")


@dataclass(frozen=True)
class Tenant:
    id: str
    name: str
    database_url: str
    hosts: tuple[str, ...] = field(default_factory=tuple)
    users_file: str | None = None


@lru_cache(maxsize=1)
def _load(path: str | None) -> dict[str, Tenant]:
    if not path:
        return {}
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out: dict[str, Tenant] = {}
    hosts_seen: set[str] = set()
    for t in data.get("tenants") or []:
        tid = str(t.get("id", ""))
        if not ID_RE.match(tid):
            raise ValueError(f"Invalid tenant id {tid!r} (lowercase letters, digits, '-')")
        if tid in out:
            raise ValueError(f"Duplicate tenant id {tid}")
        if not t.get("database_url"):
            raise ValueError(f"Tenant {tid} has no database_url")
        hosts = tuple(str(h).lower() for h in (t.get("hosts") or []))
        dup = hosts_seen & set(hosts)
        if dup:
            raise ValueError(f"Host(s) {', '.join(sorted(dup))} assigned to more than one tenant")
        hosts_seen |= set(hosts)
        out[tid] = Tenant(tid, str(t.get("name") or tid), str(t["database_url"]), hosts, t.get("users_file"))
    return out


def tenants() -> dict[str, Tenant]:
    from cfo_agent.settings import get_settings

    f = get_settings().tenants_file
    return _load(str(f) if f else None)


def is_multi_tenant() -> bool:
    return bool(tenants())


def current_tenant_id() -> str | None:
    return _current.get()


def current_tenant() -> Tenant | None:
    tid = _current.get()
    return tenants().get(tid) if tid else None


@contextmanager
def tenant_context(tenant_id: str | None):
    token = _current.set(tenant_id)
    try:
        yield
    finally:
        _current.reset(token)


def tenant_for_host(host: str | None) -> Tenant | None:
    h = (host or "").split(":")[0].lower()
    for t in tenants().values():
        if h in t.hosts:
            return t
    return None


def for_each_tenant(fn, *args, **kwargs) -> None:
    """Run fn once per tenant (or once, in single-tenant mode). One tenant's failure
    never stops the others."""
    if not is_multi_tenant():
        fn(*args, **kwargs)
        return
    for tid in tenants():
        with tenant_context(tid):
            try:
                fn(*args, **kwargs)
            except Exception:  # noqa: BLE001
                logger.exception("Job %s failed for tenant %s", getattr(fn, "__name__", fn), tid)


def cache_key() -> str:
    return _current.get() or "_"
