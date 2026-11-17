"""Multi-tenancy: one database per client (strongest isolation for finance data).

Off by default (single tenant). With TENANTS_FILE pointing at a YAML file:

    tenants:
      - id: acme
        name: Acme Industries
        database_url: postgresql://cfo:…@db/acme
        hosts: [acme.cfo.example.com]
        users_file: /config/acme-users.yaml   # optional, AUTH_MODE=local
        email_to: [cfo@acme.com]              # digest + red-line alert recipients
        email_from: cfo-agent@acme.com        # optional, defaults to EMAIL_FROM

every request is routed by its Host header to that tenant's database; unknown hosts
get 404. Email goes only to the tenant's own `email_to` (the global EMAIL_TO is never
used in multi-tenant mode; a tenant without recipients gets outbox files only). Session cookies are bound to the tenant that issued them, file-drop inboxes
live under CSV_INBOX_DIR/<tenant>/, caches are keyed per tenant, and the worker runs
every scheduled job once per tenant under that tenant's own scheduler lease.
"""

from __future__ import annotations

import logging
import re
import threading
import time
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
    email_to: tuple[str, ...] = field(default_factory=tuple)
    email_from: str | None = None
    # Per-tenant overrides of global settings (the registry's whitelist), e.g.
    # {"anthropic_model": "…", "max_llm_cost_usd_per_run": 2.0, "auth_mode": "oidc"}
    settings: dict = field(default_factory=dict, compare=False, hash=False)


def parse_recipients(value) -> tuple[str, ...]:
    """Recipients from a YAML list or a comma-separated string; de-duplicated."""
    if not value:
        return ()
    parts = value.split(",") if isinstance(value, str) else list(value)
    return tuple(dict.fromkeys(a for a in (str(p).strip() for p in parts) if a))


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
        from cfo_agent.secret_refs import resolve_secret

        out[tid] = Tenant(
            tid, str(t.get("name") or tid), resolve_secret(str(t["database_url"])), hosts, t.get("users_file"),
            email_to=parse_recipients(t.get("email_to")),
            email_from=(str(t["email_from"]).strip() or None) if t.get("email_from") else None,
            settings=dict(t.get("settings") or {}),
        )
    return out


# The registry is re-read at most every TENANT_REGISTRY_TTL_S seconds, so adding,
# changing or disabling a tenant takes effect without a restart.
_reg_lock = threading.Lock()
_reg: dict = {"at": -1e9, "url": None, "data": {}}


def invalidate_registry_cache() -> None:
    with _reg_lock:
        _reg["at"] = -1e9


def _registry_tenants(url: str, ttl: float) -> dict[str, Tenant]:
    with _reg_lock:
        fresh = _reg["url"] == url and time.monotonic() - _reg["at"] < ttl
        if fresh:
            return _reg["data"]
        try:
            from cfo_agent.registry import load_active

            data = load_active(url)
        except Exception:  # noqa: BLE001 — a control-database blip must not take every tenant down
            logger.exception("Tenant registry unavailable; keeping the last known tenants")
            _reg["at"] = time.monotonic()
            return _reg["data"] if _reg["url"] == url else {}
        _reg.update(at=time.monotonic(), url=url, data=data)
        return data


def tenants() -> dict[str, Tenant]:
    # Read the global settings directly: the tenant-aware get_settings() would call
    # back into this function to find the current tenant.
    from cfo_agent.settings import global_settings

    g = global_settings()
    if g.control_database_url:
        return _registry_tenants(g.control_database_url, g.tenant_registry_ttl_s)
    f = g.tenants_file
    return _load(str(f) if f else None)


def is_multi_tenant() -> bool:
    """With a control database the app is multi-tenant even before the first client is
    added: unknown hosts get 404 rather than silently reaching the default database."""
    from cfo_agent.settings import global_settings

    return bool(global_settings().control_database_url) or bool(tenants())


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
    from cfo_agent.tenant_prepare import ensure_ready, uses_registry

    for tid, tenant in list(tenants().items()):
        with tenant_context(tid):
            try:
                if uses_registry():
                    ensure_ready(tenant)  # a tenant added since startup has no schema yet
                fn(*args, **kwargs)
            except Exception:  # noqa: BLE001
                logger.exception("Job %s failed for tenant %s", getattr(fn, "__name__", fn), tid)


def cache_key() -> str:
    return _current.get() or "_"
