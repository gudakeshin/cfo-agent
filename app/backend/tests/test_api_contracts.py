"""API contract tests — deterministic (no LLM calls)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cfo_agent.api import api_router
from cfo_agent.digest.builder import persist_digest
from cfo_agent.kra import upsert_kra_version
from cfo_agent.models import Digest, PipelineRun
from cfo_agent.pipeline.orchestrator import _week_locks
from cfo_agent.weighting import get_active_weighting


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(api_router)

    @app.get("/api/health")
    def health():
        return {"ok": True}

    return TestClient(app)


def test_generate_returns_run_id(seeded_erp, db_session, monkeypatch):
    """POST /digests/generate returns run_id immediately; do not execute LLM pipeline."""
    calls: list[tuple] = []

    def fake_run(run_id, week, weighting_version_id=None):
        calls.append((run_id, week, weighting_version_id))

    monkeypatch.setattr("cfo_agent.api.digests.run_pipeline", fake_run)

    week = seeded_erp["weeks"][-2]
    client = _client()
    res = client.post("/api/digests/generate", json={"week": week})
    assert res.status_code == 200
    body = res.json()
    assert "run_id" in body
    assert body["week"] == week
    assert body["status"] == "pending"
    # BackgroundTasks run after response in TestClient
    assert calls and calls[0][0] == body["run_id"]


def test_concurrent_generate_same_week_409(seeded_erp, db_session, monkeypatch):
    monkeypatch.setattr("cfo_agent.api.digests.run_pipeline", lambda *a, **k: None)

    week = seeded_erp["weeks"][-2]
    _week_locks[week] = 999
    try:
        client = _client()
        res = client.post("/api/digests/generate", json={"week": week})
        assert res.status_code == 409
        detail = res.json()["detail"]
        assert "already in progress" in str(detail).lower() or "already running" in str(detail).lower()
    finally:
        _week_locks.pop(week, None)


def test_data_regenerate_without_confirm_409(seeded_erp, db_session):
    wv = get_active_weighting(db_session)
    kra = upsert_kra_version(db_session)
    run = PipelineRun(
        week=seeded_erp["weeks"][-3],
        weighting_version_id=wv.id,
        status="completed",
        current_step="done",
    )
    db_session.add(run)
    db_session.flush()
    persist_digest(
        db_session,
        week=seeded_erp["weeks"][-3],
        weighting=wv,
        kra_version=kra,
        run=run,
        headline="Existing",
        surfaced=[],
        empty_bar=True,
        data_health={"ok": True},
    )
    db_session.commit()
    assert db_session.query(Digest).count() >= 1

    client = _client()
    res = client.post("/api/data/generate", json={"seed": 42, "stale_days": 0, "confirm": False})
    assert res.status_code == 409
    assert "confirm" in res.json()["detail"].lower()


def test_data_regenerate_with_confirm_wipes_dependents(seeded_erp, db_session):
    """confirm=true cascades wipe digests/alerts/signals/feedback/archival; ERP regenerates."""
    from datetime import date

    from cfo_agent.models import Alert, ArchivalMemory, Signal

    wv = get_active_weighting(db_session)
    kra = upsert_kra_version(db_session)
    week = seeded_erp["weeks"][-3]
    run = PipelineRun(
        week=week,
        weighting_version_id=wv.id,
        status="completed",
        current_step="done",
    )
    db_session.add(run)
    db_session.flush()
    digest = persist_digest(
        db_session,
        week=week,
        weighting=wv,
        kra_version=kra,
        run=run,
        headline="To wipe",
        surfaced=[],
        empty_bar=True,
        data_health={"ok": True},
    )
    db_session.add(
        Alert(
            rule_id="RL-1",
            episode_key="RL-1:test-cascade",
            week=seeded_erp["weeks"][-1],
            as_of_date=date.today(),
            title="test",
            description="test",
            evidence_json={},
            status="active",
        )
    )
    db_session.add(
        Signal(
            signal_id=f"dso_days:{week}",
            week=week,
            metric_id="dso_days",
            value=50.0,
            baseline=45.0,
            delta_wow=5.0,
            lineage_json=[],
            kra_ids_json=["STW-1"],
        )
    )
    db_session.add(
        ArchivalMemory(
            text="old insight",
            metadata_json={"week": week},
            week=week,
        )
    )
    db_session.commit()
    assert db_session.query(Digest).count() >= 1
    digest_id = digest.id

    client = _client()
    res = client.post("/api/data/generate", json={"seed": 42, "stale_days": 0, "confirm": True})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["meta"]["seed"] == 42
    assert "S1" in body["meta"]["scenario_flags"]

    # Fresh session view — cascade wiped dependents
    db_session.expire_all()
    assert db_session.query(Digest).count() == 0
    assert db_session.query(Alert).count() == 0
    assert db_session.query(Signal).count() == 0
    assert db_session.query(ArchivalMemory).count() == 0
    assert db_session.get(Digest, digest_id) is None
    # ERP re-seeded
    assert body["meta"]["sales_invoices"] > 0
