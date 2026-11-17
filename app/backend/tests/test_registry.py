"""Tenant registry: a control database of clients, per-tenant settings and secret references."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cfo_agent import registry
from cfo_agent.registry import RegistryError
from cfo_agent.secret_refs import SecretError, mask, resolve_secret


@pytest.fixture()
def reg(tmp_path, monkeypatch):
    """A registry in a throwaway control database; the app runs in multi-tenant mode."""
    import cfo_agent.db as dbmod
    import cfo_agent.tenancy as tenancy
    import cfo_agent.tenant_prepare as prep
    from cfo_agent.settings import get_settings

    monkeypatch.setenv("CONTROL_DATABASE_URL", f"sqlite:///{tmp_path / 'control.db'}")
    monkeypatch.setenv("TENANT_REGISTRY_TTL_S", "0")  # re-read on every look-up
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'default.db'}")
    monkeypatch.setenv("CSV_INBOX_DIR", str(tmp_path / "inbox"))
    monkeypatch.delenv("TENANTS_FILE", raising=False)
    get_settings.cache_clear()
    registry._sessionmaker.cache_clear()
    tenancy._reg.update(at=-1e9, url=None, data={})
    prep._ready.clear()
    dbmod._engine = None
    dbmod._SessionLocal = None
    dbmod._tenant_engines.clear()
    dbmod._tenant_urls.clear()
    yield tmp_path
    for eng, _ in dbmod._tenant_engines.values():
        eng.dispose()
    dbmod._tenant_engines.clear()
    dbmod._tenant_urls.clear()
    get_settings.cache_clear()
    registry._sessionmaker.cache_clear()


def _add(reg_path, tid, host, **kw):
    return registry.add_tenant(tid, name=tid.title(), database=f"sqlite:///{reg_path / (tid + '.db')}", hosts=[host], **kw)


# ── secret references ───────────────────────────────────────────────────────

def test_secret_references(tmp_path, monkeypatch):
    monkeypatch.setenv("ACME_DB", "postgresql://u:pw@db/acme")
    assert resolve_secret("env:ACME_DB") == "postgresql://u:pw@db/acme"
    f = tmp_path / "s"
    f.write_text("  sekret\n")
    assert resolve_secret(f"file:{f}") == "sekret"
    assert resolve_secret("sqlite:///x.db") == "sqlite:///x.db"  # a literal is used as written
    with pytest.raises(SecretError):
        resolve_secret("env:NOT_SET_ANYWHERE")
    with pytest.raises(SecretError):
        resolve_secret(f"file:{tmp_path / 'missing'}")
    assert mask("postgresql://cfo:hunter2@db/acme") == "postgresql://cfo:***@db/acme"
    assert mask("env:ACME_DB") == "env:ACME_DB"


# ── the registry ────────────────────────────────────────────────────────────

def test_add_list_update_disable_remove_and_the_event_log(reg):
    _add(reg, "acme", "acme.local", email_to="cfo@acme.test, fc@acme.test", overrides={"max_llm_cost_usd_per_run": "2"})
    row = registry.list_rows()[0]
    assert row.hosts_json == ["acme.local"] and row.email_to_json == ["cfo@acme.test", "fc@acme.test"]
    assert row.settings_json == {"max_llm_cost_usd_per_run": 2.0}

    registry.update_tenant("acme", overrides={"anthropic_model": "m-1"}, hosts=["acme.local", "cfo.acme.test"])
    registry.update_tenant("acme", unset=["max_llm_cost_usd_per_run"])
    row = registry.list_rows()[0]
    assert row.settings_json == {"anthropic_model": "m-1"} and row.hosts_json == ["acme.local", "cfo.acme.test"]

    registry.set_status("acme", "disabled")
    assert registry.load_active(registry._control_url()) == {}
    registry.set_status("acme", "active")
    assert list(registry.load_active(registry._control_url())) == ["acme"]
    registry.remove_tenant("acme")
    assert registry.list_rows() == []
    assert [e.action for e in reversed(registry.events("acme"))] == ["add", "update", "update", "disable", "enable", "remove"]


def test_validation(reg):
    _add(reg, "acme", "acme.local")
    with pytest.raises(RegistryError, match="id must be"):
        _add(reg, "Bad_ID", "x.local")
    with pytest.raises(RegistryError, match="already exists"):
        _add(reg, "acme", "other.local")
    with pytest.raises(RegistryError, match="already belongs to tenant acme"):
        _add(reg, "zenith", "ACME.local")  # hosts are case-insensitive
    with pytest.raises(RegistryError, match="cannot be set per tenant"):
        registry.update_tenant("acme", overrides={"digest_cron": "0 9 * * *"})  # schedules are shared
    with pytest.raises(RegistryError, match="must be a reference"):
        registry.update_tenant("acme", overrides={"oidc_client_secret": "plaintext"})
    with pytest.raises(RegistryError, match="0 or more"):
        registry.update_tenant("acme", overrides={"max_llm_cost_usd_per_run": "-1"})
    with pytest.raises(RegistryError, match="must be one of"):
        registry.update_tenant("acme", overrides={"auth_mode": "magic"})
    with pytest.raises(RegistryError, match="environment variable"):
        registry.add_tenant("zen", name="Z", database="env:NOPE_NOT_SET", hosts=["z.local"])
    assert [r.id for r in registry.list_rows()] == ["acme"]  # nothing half-added


def test_a_tenant_whose_secret_is_missing_is_left_out_not_fatal(reg, monkeypatch):
    monkeypatch.setenv("ZEN_DB", f"sqlite:///{reg / 'zen.db'}")
    registry.add_tenant("zen", name="Zen", database="env:ZEN_DB", hosts=["zen.local"])
    _add(reg, "acme", "acme.local")
    assert set(registry.load_active(registry._control_url())) == {"zen", "acme"}
    monkeypatch.delenv("ZEN_DB")
    assert set(registry.load_active(registry._control_url())) == {"acme"}


def test_list_never_prints_a_password(reg, capsys):
    registry.add_tenant("acme", name="Acme", database="postgresql://cfo:hunter2@db/acme", hosts=["a.local"],
                        check_database=False)
    registry.main(["list"])
    out = capsys.readouterr().out
    assert "hunter2" not in out and "cfo:***@db/acme" in out
    assert registry.main(["add", "acme", "--database", "sqlite:///x.db"]) == 1  # duplicate: error exit, no traceback


def test_import_from_a_tenants_file(reg, tmp_path):
    f = tmp_path / "tenants.yaml"
    f.write_text(f"""
tenants:
  - id: acme
    name: Acme
    database_url: sqlite:///{tmp_path / 'a.db'}
    hosts: [acme.local]
    email_to: [cfo@acme.test]
    settings: {{max_llm_cost_usd_per_run: 3}}
""")
    assert registry.import_yaml(str(f)) == ["acme"]
    assert registry.import_yaml(str(f)) == []  # idempotent
    t = registry.load_active(registry._control_url())["acme"]
    assert t.email_to == ("cfo@acme.test",) and t.settings == {"max_llm_cost_usd_per_run": 3.0}


# ── per-tenant settings ─────────────────────────────────────────────────────

def test_settings_follow_the_current_tenant(reg, monkeypatch):
    from cfo_agent.settings import get_settings
    from cfo_agent.tenancy import tenant_context

    monkeypatch.setenv("ACME_OIDC_SECRET", "s3cr3t")
    monkeypatch.setenv("MAX_LLM_COST_USD_PER_RUN", "5")
    get_settings.cache_clear()
    _add(reg, "acme", "acme.local", overrides={
        "max_llm_cost_usd_per_run": "0.5", "anthropic_model": "acme-model", "oidc_client_secret": "env:ACME_OIDC_SECRET"})
    _add(reg, "zenith", "zenith.local")
    assert get_settings().max_llm_cost_usd_per_run == 5.0  # outside any tenant: the global value
    with tenant_context("acme"):
        s = get_settings()
        assert (s.max_llm_cost_usd_per_run, s.anthropic_model, s.oidc_client_secret) == (0.5, "acme-model", "s3cr3t")
    with tenant_context("zenith"):
        assert get_settings().max_llm_cost_usd_per_run == 5.0  # untouched by acme's override
    assert get_settings().oidc_client_secret != "s3cr3t"


# ── routing, with tenants added while the app runs ──────────────────────────

def test_an_empty_registry_never_reaches_the_default_database(reg):
    from cfo_agent.main import app

    with TestClient(app) as c:
        assert c.get("/api/digests", headers={"host": "anything.local"}).status_code == 404


def test_tenants_added_enabled_disabled_and_moved_without_a_restart(reg):
    from cfo_agent.main import app

    with TestClient(app) as c:
        h = {"host": "acme.local"}
        assert c.get("/api/digests", headers=h).status_code == 404
        _add(reg, "acme", "acme.local")
        # Its database did not exist at startup: it is prepared on first use.
        assert c.get("/api/digests", headers=h).status_code == 200
        assert (reg / "acme.db").exists()

        registry.set_status("acme", "disabled")
        assert c.get("/api/digests", headers=h).status_code == 404
        registry.set_status("acme", "active")
        assert c.get("/api/digests", headers=h).status_code == 200

        # Point the tenant at a different database: the next request uses it.
        registry.update_tenant("acme", database=f"sqlite:///{reg / 'acme2.db'}")
        assert c.get("/api/digests", headers=h).status_code == 200
        assert (reg / "acme2.db").exists()
        registry.update_tenant("acme", hosts=["new.acme.local"])
        assert c.get("/api/digests", headers=h).status_code == 404
        assert c.get("/api/digests", headers={"host": "new.acme.local"}).status_code == 200


def test_each_tenants_data_stays_in_its_own_database(reg):
    from cfo_agent.db import get_session
    from cfo_agent.kra import upsert_kra_version
    from cfo_agent.main import app
    from cfo_agent.models import Digest, WeightingVersion
    from cfo_agent.tenancy import tenant_context
    from cfo_agent.weighting import seed_weighting_from_yaml

    _add(reg, "acme", "acme.local")
    _add(reg, "zenith", "zenith.local")
    with TestClient(app) as c:
        for tid in ("acme", "zenith"):  # first request prepares each database
            assert c.get("/api/digests", headers={"host": f"{tid}.local"}).status_code == 200
        for tid in ("acme", "zenith"):
            with tenant_context(tid):
                s = get_session()
                upsert_kra_version(s)
                wv = seed_weighting_from_yaml(s) or s.query(WeightingVersion).first()
                s.add(Digest(week="2026-W40", weighting_version_id=wv.id, headline=f"{tid} headline"))
                s.commit()
                s.close()
        assert [d["headline"] for d in c.get("/api/digests", headers={"host": "acme.local"}).json()] == ["acme headline"]
        assert [d["headline"] for d in c.get("/api/digests", headers={"host": "zenith.local"}).json()] == ["zenith headline"]


def test_jobs_skip_disabled_tenants_and_pick_up_new_ones(reg):
    from cfo_agent.tenancy import current_tenant_id, for_each_tenant

    seen: list[str] = []
    for_each_tenant(lambda: seen.append(current_tenant_id()))
    assert seen == []  # an empty registry runs nothing, not "once for the default database"
    _add(reg, "acme", "acme.local")
    _add(reg, "zenith", "zenith.local")
    registry.set_status("zenith", "disabled")
    for_each_tenant(lambda: seen.append(current_tenant_id()))
    assert seen == ["acme"]
    assert (reg / "acme.db").exists() and not (reg / "zenith.db").exists()


def test_email_recipients_come_from_the_registry(reg):
    from cfo_agent.tenancy import current_tenant, tenant_context

    _add(reg, "acme", "acme.local", email_to="cfo@acme.test", email_from="agent@acme.test")
    _add(reg, "zenith", "zenith.local")
    with tenant_context("acme"):
        assert current_tenant().email_to == ("cfo@acme.test",) and current_tenant().email_from == "agent@acme.test"
    with tenant_context("zenith"):
        assert current_tenant().email_to == ()
