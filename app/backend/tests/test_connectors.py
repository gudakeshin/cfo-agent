"""WS1 ERP connector catalog + simulated sync engine tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cfo_agent.api import api_router
from cfo_agent.connectors.catalog import OWNERSHIP, CONNECTORS
from cfo_agent.connectors.sync import run_connector_sync
from cfo_agent.models import (
    ConnectorConfig,
    ConnectorSyncRun,
    ErpCashDaily,
    ErpCustomer,
    ErpExpense,
    ErpPayment,
    ErpPlanMetric,
    ErpPurchaseInvoice,
    ErpReceipt,
    ErpRevenueDaily,
    ErpSalesInvoice,
)
from cfo_agent.synthetic.adapter import ERP_TABLES


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(api_router)

    @app.get("/api/health")
    def health():
        return {"ok": True}

    return TestClient(app)


def _full_sync(db_session, connector_id: str, mode: str = "full") -> ConnectorSyncRun:
    cfg = db_session.get(ConnectorConfig, connector_id)
    if cfg is None:
        cfg = ConnectorConfig(id=connector_id, enabled=True, config_json={}, secret=None)
        db_session.add(cfg)
    run = ConnectorSyncRun(connector_id=connector_id, trigger="manual", mode=mode, status="running")
    db_session.add(run)
    db_session.commit()
    run_connector_sync(run.id)
    db_session.expire_all()
    return db_session.get(ConnectorSyncRun, run.id)


# ─── Catalog integrity ─────────────────────────────────────────────────────────


def test_catalog_ownership_integrity():
    for table, owners in OWNERSHIP.items():
        if table == ErpPlanMetric.__tablename__:
            assert set(owners) == {"anaplan", "sap_sac"}, owners
        else:
            assert len(owners) == 1, f"{table} has {len(owners)} owners: {owners}"

    for model in ERP_TABLES:
        assert model.__tablename__ in OWNERSHIP, f"{model.__tablename__} has no connector owner"


def test_catalog_ids_unique_and_have_secret_field():
    ids = [c.id for c in CONNECTORS]
    assert len(ids) == len(set(ids))
    for c in CONNECTORS:
        secret_fields = [f for f in c.config_fields if f.secret]
        assert len(secret_fields) == 1, f"{c.id} should declare exactly one secret field"


# ─── Config: secret masking / preservation, cron validation ───────────────────


def test_secret_masking_and_preservation(db_session):
    client = _client()
    res = client.put(
        "/api/connectors/sap_s4hana",
        json={"config": {"client_id": "abc", "tenant_url": "https://x"}, "secret": "supersecretvalue123"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["secret_masked"] == "••••" + "supersecretvalue123"[-4:]
    assert body["secret_set"] is True
    assert "supersecretvalue123" not in str(body)

    # Empty secret on PUT preserves the stored value
    res2 = client.put("/api/connectors/sap_s4hana", json={"secret": ""})
    assert res2.json()["secret_masked"] == body["secret_masked"]

    # GET list also masks, never returns raw secret
    res3 = client.get("/api/connectors")
    assert res3.status_code == 200
    payload = res3.json()
    assert "erp_seed" in payload
    conn = next(c for c in payload["connectors"] if c["id"] == "sap_s4hana")
    assert conn["secret_masked"] == body["secret_masked"]
    assert "supersecretvalue123" not in str(payload)


def test_bad_cron_422(db_session):
    client = _client()
    res = client.put("/api/connectors/salesforce", json={"cron": "not a cron"})
    assert res.status_code == 422


def test_good_cron_accepted_and_scheduler_noop(db_session):
    from cfo_agent.scheduler import get_scheduler

    assert get_scheduler() is None  # never started in tests
    client = _client()
    res = client.put("/api/connectors/salesforce", json={"enabled": True, "cron": "0 * * * *"})
    assert res.status_code == 200
    assert res.json()["cron"] == "0 * * * *"


def test_unknown_connector_404(db_session):
    client = _client()
    res = client.put("/api/connectors/not_a_real_connector", json={"enabled": True})
    assert res.status_code == 404


# ─── test-connection ────────────────────────────────────────────────────────────


def test_connection_missing_field_then_ok(db_session):
    client = _client()
    res = client.post("/api/connectors/anaplan/test")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False
    assert "api_key" in body["missing_fields"]
    assert "workspace_id" in body["missing_fields"]

    res2 = client.put(
        "/api/connectors/anaplan", json={"config": {"workspace_id": "ws-1"}, "secret": "sk-anaplan-key"}
    )
    assert res2.status_code == 200

    res3 = client.post("/api/connectors/anaplan/test")
    body3 = res3.json()
    assert body3["ok"] is True


# ─── Full sync: owned-table mutation, FK integrity, auto-provisioned masters ──


def test_full_sync_mutates_only_owned_tables_with_fk_integrity(db_session):
    run = _full_sync(db_session, "sap_s4hana", mode="full")
    assert run.status == "ok", run.error

    rows = run.rows_pulled_json
    assert rows["erp_sales_invoices"]["created"] > 0
    assert rows["erp_receipts"]["created"] > 0
    assert rows["erp_purchase_invoices"]["created"] > 0
    assert rows["erp_payments"]["created"] > 0
    assert rows["referenced_masters"]["erp_customers"] > 0  # auto-provisioned master

    cfg = db_session.get(ConnectorConfig, "sap_s4hana")
    assert cfg.watermark_week is not None
    assert cfg.last_status == "ok"

    assert db_session.query(ErpSalesInvoice).count() > 0
    assert db_session.query(ErpCustomer).count() > 0

    # sap_s4hana doesn't own these — must remain untouched
    assert db_session.query(ErpCashDaily).count() == 0
    assert db_session.query(ErpExpense).count() == 0
    assert db_session.query(ErpRevenueDaily).count() == 0

    # FK integrity across the auto-provisioned + owned rows
    for inv in db_session.query(ErpSalesInvoice).all():
        assert db_session.get(ErpCustomer, inv.customer_id) is not None
    for rcpt in db_session.query(ErpReceipt).all():
        assert db_session.get(ErpSalesInvoice, rcpt.invoice_id) is not None
    for pay in db_session.query(ErpPayment).all():
        assert db_session.get(ErpPurchaseInvoice, pay.purchase_invoice_id) is not None


def test_cross_connector_customer_ownership_preserves_ids(db_session):
    """sap_s4hana auto-provisions customers it doesn't own; salesforce's own full sync
    of erp_customers must upsert those same rows (preserve ids) — never wipe them,
    since sales invoices still hold live FKs into them."""
    run1 = _full_sync(db_session, "sap_s4hana", mode="full")
    assert run1.status == "ok", run1.error

    before = {c.id: c.business_key for c in db_session.query(ErpCustomer).all()}
    assert len(before) > 0

    run2 = _full_sync(db_session, "salesforce", mode="full")
    assert run2.status == "ok", run2.error

    after = {c.id: c.business_key for c in db_session.query(ErpCustomer).all()}
    assert len(after) == 15  # 3 anchors + 12 numbered customers from the generator
    for cid, bk in before.items():
        assert after[cid] == bk  # same id preserved, no FK breakage

    for inv in db_session.query(ErpSalesInvoice).all():
        assert db_session.get(ErpCustomer, inv.customer_id) is not None


def test_plan_metrics_sync_writes_kind_scoped_rows(db_session):
    run_plan = _full_sync(db_session, "anaplan", mode="full")
    assert run_plan.status == "ok", run_plan.error
    run_analytics = _full_sync(db_session, "sap_sac", mode="full")
    assert run_analytics.status == "ok", run_analytics.error

    plan_rows = db_session.query(ErpPlanMetric).filter_by(kind="plan").all()
    analytics_rows = db_session.query(ErpPlanMetric).filter_by(kind="analytics").all()
    assert len(plan_rows) > 0
    assert len(analytics_rows) > 0
    assert all(r.source_connector == "anaplan" for r in plan_rows)
    assert all(r.source_connector == "sap_sac" for r in analytics_rows)


# ─── Incremental after full: delta/zero + watermark advance ──────────────────


def test_incremental_after_full_pulls_zero_and_keeps_watermark(db_session):
    run1 = _full_sync(db_session, "sap_s4hana", mode="full")
    assert run1.status == "ok", run1.error
    watermark_after_full = db_session.get(ConnectorConfig, "sap_s4hana").watermark_week
    count_after_full = db_session.query(ErpSalesInvoice).count()

    run2 = _full_sync(db_session, "sap_s4hana", mode="incremental")
    assert run2.status == "ok", run2.error
    assert run2.rows_pulled_json["erp_sales_invoices"]["scanned"] == 0

    cfg = db_session.get(ConnectorConfig, "sap_s4hana")
    assert cfg.watermark_week == watermark_after_full
    assert db_session.query(ErpSalesInvoice).count() == count_after_full  # no duplication


# ─── Determinism ───────────────────────────────────────────────────────────────


def test_determinism_same_seed_same_rows_pulled(db_session):
    def _run() -> dict:
        cfg = db_session.get(ConnectorConfig, "oracle_fusion")
        if cfg is not None:
            cfg.watermark_week = None
        run = _full_sync(db_session, "oracle_fusion", mode="full")
        assert run.status == "ok", run.error
        return run.rows_pulled_json

    rows1 = _run()
    rows2 = _run()
    assert rows1 == rows2


# ─── Concurrency / errors ──────────────────────────────────────────────────────


def test_sync_conflict_409(db_session):
    cfg = ConnectorConfig(id="ms_dynamics_365", enabled=True, config_json={}, secret=None)
    db_session.add(cfg)
    running = ConnectorSyncRun(
        connector_id="ms_dynamics_365", trigger="manual", mode="incremental", status="running"
    )
    db_session.add(running)
    db_session.commit()

    client = _client()
    res = client.post("/api/connectors/ms_dynamics_365/sync")
    assert res.status_code == 409


def test_stale_run_recovery_marks_interrupted(db_session):
    from cfo_agent.main import _recover_stale_runs

    cfg = ConnectorConfig(id="salesforce", enabled=True, last_status="running")
    db_session.add(cfg)
    run = ConnectorSyncRun(connector_id="salesforce", trigger="manual", mode="incremental", status="running")
    db_session.add(run)
    db_session.commit()

    _recover_stale_runs(db_session)
    db_session.commit()
    db_session.expire_all()

    run = db_session.get(ConnectorSyncRun, run.id)
    assert run.status == "error"
    assert "interrupted" in (run.error or "").lower()
    cfg = db_session.get(ConnectorConfig, "salesforce")
    assert cfg.last_status == "error"


def test_runs_list_and_detail_endpoints(db_session):
    run = _full_sync(db_session, "oracle_fusion", mode="full")
    assert run.status == "ok", run.error

    client = _client()
    res = client.get("/api/connectors/oracle_fusion/runs?limit=5")
    assert res.status_code == 200
    runs = res.json()
    assert len(runs) >= 1
    assert runs[0]["id"] == run.id
    assert runs[0]["connector_id"] == "oracle_fusion"

    res2 = client.get(f"/api/connectors/runs/{run.id}")
    assert res2.status_code == 200
    assert res2.json()["status"] == "ok"

    res3 = client.get("/api/connectors/runs/999999")
    assert res3.status_code == 404


def test_sync_all_enabled_connectors_sequential(db_session):
    for cid in ("oracle_fusion", "ms_dynamics_365"):
        db_session.add(ConnectorConfig(id=cid, enabled=True, config_json={}, secret=None))
    db_session.add(ConnectorConfig(id="salesforce", enabled=False, config_json={}, secret=None))
    db_session.commit()

    client = _client()
    res = client.post("/api/connectors/sync-all")
    assert res.status_code == 200
    body = res.json()
    assert len(body["run_ids"]) == 2
    assert body["skipped"] == []

    db_session.expire_all()
    for run_id in body["run_ids"]:
        run = db_session.get(ConnectorSyncRun, run_id)
        assert run.status == "ok", run.error
        assert run.trigger == "sync_all"


def test_scheduler_upsert_job_noop_in_tests():
    from cfo_agent.scheduler import get_scheduler, remove_job, upsert_job

    assert get_scheduler() is None
    upsert_job("connector:test_noop", lambda: None, "0 * * * *")  # must not raise
    remove_job("connector:test_noop")  # must not raise


# ─── /data/generate resets connector watermarks + records seed ───────────────


def test_data_generate_resets_watermarks_and_records_seed(db_session, seeded_erp):
    cfg = ConnectorConfig(id="sap_s4hana", enabled=True, watermark_week="2026-W10", last_status="ok")
    db_session.add(cfg)
    db_session.commit()

    client = _client()
    res = client.post("/api/data/generate", json={"seed": 7, "confirm": True})
    assert res.status_code == 200
    body = res.json()
    assert body["meta"]["seed"] == 7

    db_session.expire_all()
    cfg = db_session.get(ConnectorConfig, "sap_s4hana")
    assert cfg.watermark_week is None
    assert cfg.last_status == "never_run"

    from cfo_agent.connectors.sync import get_erp_seed

    assert get_erp_seed(db_session) == 7
