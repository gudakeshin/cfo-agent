"""Database-per-tenant routing: isolation of data, sessions, caches and metrics."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def two_tenants(tmp_path, monkeypatch):
    import cfo_agent.db as dbmod
    from cfo_agent.settings import get_settings
    from cfo_agent.tenancy import _load

    cfg = tmp_path / "tenants.yaml"
    cfg.write_text(f"""
tenants:
  - id: acme
    name: Acme
    database_url: sqlite:///{tmp_path / 'acme.db'}
    hosts: [acme.local]
  - id: zenith
    name: Zenith
    database_url: sqlite:///{tmp_path / 'zenith.db'}
    hosts: [zenith.local]
""")
    monkeypatch.setenv("TENANTS_FILE", str(cfg))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'default.db'}")
    monkeypatch.setenv("CSV_INBOX_DIR", str(tmp_path / "inbox"))
    get_settings.cache_clear()
    _load.cache_clear()
    dbmod._engine = None
    dbmod._SessionLocal = None
    dbmod._tenant_engines.clear()
    yield
    for eng, _ in dbmod._tenant_engines.values():
        eng.dispose()
    dbmod._tenant_engines.clear()
    monkeypatch.delenv("TENANTS_FILE")
    get_settings.cache_clear()
    _load.cache_clear()


def test_requests_are_routed_and_isolated(two_tenants, tmp_path):
    from cfo_agent.db import get_session, init_db
    from cfo_agent.kra import upsert_kra_version
    from cfo_agent.main import app
    from cfo_agent.models import Digest, WeightingVersion
    from cfo_agent.tenancy import tenant_context
    from cfo_agent.weighting import seed_weighting_from_yaml

    init_db()
    for tid in ("acme", "zenith"):
        with tenant_context(tid):
            s = get_session()
            upsert_kra_version(s)
            wv = seed_weighting_from_yaml(s) or s.query(WeightingVersion).first()
            s.add(Digest(week="2026-W40", weighting_version_id=wv.id, headline=f"{tid} headline"))
            s.commit()
            s.close()
    assert (tmp_path / "acme.db").exists() and (tmp_path / "zenith.db").exists()

    from cfo_agent.api import api_router  # noqa: F401  (app already includes it)

    c = TestClient(app)
    a = c.get("/api/digests", headers={"host": "acme.local"}).json()
    z = c.get("/api/digests", headers={"host": "zenith.local"}).json()
    assert [d["headline"] for d in a] == ["acme headline"]
    assert [d["headline"] for d in z] == ["zenith headline"]
    assert c.get("/api/digests", headers={"host": "evil.local"}).status_code == 404


def test_cookie_from_one_tenant_does_not_open_another(two_tenants):
    from cfo_agent.auth import User, make_session_cookie, read_session_cookie
    from cfo_agent.tenancy import tenant_context

    with tenant_context("acme"):
        cookie = make_session_cookie(User("cfo@acme", "CFO", "cfo"))
        assert read_session_cookie(cookie).email == "cfo@acme"
    with tenant_context("zenith"):
        assert read_session_cookie(cookie) is None


def test_custom_metrics_and_inboxes_are_per_tenant(two_tenants, tmp_path):
    from cfo_agent.connectors.sync import file_drop_inbox
    from cfo_agent.db import get_session, init_db
    from cfo_agent.metrics_dsl import ensure_custom_metrics, save_definition, visible_metric_ids
    from cfo_agent.models import ConnectorConfig
    from cfo_agent.tenancy import tenant_context

    init_db()
    d = {"id": "acme_only", "label": "A", "unit": "count",
         "inputs": {"n": {"agg": "count", "source": "expenses"}}, "formula": "n"}
    with tenant_context("acme"):
        s = get_session()
        save_definition(s, d, user="t")
        s.commit()
        assert "acme_only" in visible_metric_ids()
        assert file_drop_inbox(ConnectorConfig(id="csv_drop", config_json={"folder": "x"})) == tmp_path / "inbox" / "acme" / "x"
        s.close()
    with tenant_context("zenith"):
        s = get_session()
        ensure_custom_metrics(s)
        assert "acme_only" not in visible_metric_ids()
        from cfo_agent.signals import COMPUTE_FNS

        assert not COMPUTE_FNS["acme_only"](s, "2026-W40").available  # registered, but not zenith's
        s.close()


def test_email_goes_only_to_the_current_tenants_recipients(two_tenants, tmp_path, monkeypatch):
    """One client's digest must never reach another client's (or the global) address."""
    import cfo_agent.digest.email as mail
    from cfo_agent.settings import get_settings
    from cfo_agent.tenancy import _load, tenant_context

    cfg = tmp_path / "tenants.yaml"
    cfg.write_text(cfg.read_text().replace(
        "hosts: [acme.local]", "hosts: [acme.local]\n    email_to: [cfo@acme.test, fc@acme.test]\n    email_from: agent@acme.test"
    ))
    monkeypatch.setenv("EMAIL_TO", "global@operator.test")
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    get_settings.cache_clear()
    _load.cache_clear()
    monkeypatch.setattr(mail, "OUTBOX_DIR", tmp_path / "outbox")

    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a): pass
        def sendmail(self, sender, to, body): sent.append((sender, list(to)))

    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)

    with tenant_context("acme"):
        assert mail.deliver_html("<p>acme</p>", "s", "digest-2026-W40-1.html") == ("sent", None)
    assert sent == [("agent@acme.test", ["cfo@acme.test", "fc@acme.test"])]

    # Zenith has no recipients: nothing is sent (not even to the global EMAIL_TO),
    # and its outbox file lands in its own folder.
    with tenant_context("zenith"):
        status, path = mail.deliver_html("<p>zenith</p>", "s", "digest-2026-W40-1.html")
    assert status == "outbox" and len(sent) == 1
    assert path == str(tmp_path / "outbox" / "zenith" / "digest-2026-W40-1.html")

    # Same file name for another tenant does not overwrite it.
    monkeypatch.delenv("SMTP_HOST")
    get_settings.cache_clear()
    with tenant_context("acme"):
        _, acme_path = mail.deliver_html("<p>acme</p>", "s", "digest-2026-W40-1.html")
    assert acme_path != path
    assert "zenith" in (tmp_path / "outbox" / "zenith" / "digest-2026-W40-1.html").read_text()


def test_single_tenant_email_uses_global_recipients(monkeypatch, tmp_path):
    import cfo_agent.digest.email as mail
    from cfo_agent.settings import get_settings
    from cfo_agent.tenancy import _load

    monkeypatch.delenv("TENANTS_FILE", raising=False)
    monkeypatch.setenv("EMAIL_TO", "cfo@one.test, delegate@one.test")
    get_settings.cache_clear()
    _load.cache_clear()
    monkeypatch.setattr(mail, "OUTBOX_DIR", tmp_path / "outbox")
    try:
        assert mail._recipients() == ["cfo@one.test", "delegate@one.test"]
        assert mail.outbox_dir() == tmp_path / "outbox"
    finally:
        get_settings.cache_clear()
