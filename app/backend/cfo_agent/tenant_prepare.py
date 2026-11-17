"""Prepare a tenant's database the first time this process uses it.

With the tenant registry a client can be added while the app and workers are running,
so its schema and seed data (KRA framework, weighting, memory blocks) cannot wait for
the next restart. This runs the same idempotent steps as startup, once per process per
database, under the tenant's own context."""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_ready: dict[str, str] = {}  # tenant id -> database url it was prepared for


def ensure_ready(tenant) -> None:
    if _ready.get(tenant.id) == tenant.database_url:
        return
    with _lock:
        if _ready.get(tenant.id) == tenant.database_url:
            return
        from cfo_agent.db import _init_one, get_session
        from cfo_agent.kra import upsert_kra_version
        from cfo_agent.memory.blocks import ensure_seed_blocks
        from cfo_agent.security import encrypt_plaintext_secrets
        from cfo_agent.tenancy import tenant_context
        from cfo_agent.weighting import seed_weighting_from_yaml

        with tenant_context(tenant.id):
            _init_one()
            session = get_session()
            try:
                upsert_kra_version(session)
                seed_weighting_from_yaml(session)
                ensure_seed_blocks(session)
                encrypt_plaintext_secrets(session)
                session.commit()
            finally:
                session.close()
        _ready[tenant.id] = tenant.database_url
        logger.info("Prepared database for tenant %s", tenant.id)


def uses_registry() -> bool:
    from cfo_agent.settings import global_settings

    return bool(global_settings().control_database_url)
