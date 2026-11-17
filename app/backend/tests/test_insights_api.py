"""CFO read-model APIs: metrics metadata, trend history, briefing diff, weighting preview."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cfo_agent.api import api_router
from cfo_agent.digest.builder import persist_digest
from cfo_agent.kra import upsert_kra_version
from cfo_agent.models import DigestItem, PipelineRun, Signal
from cfo_agent.weighting import get_active_weighting, load_preset


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(api_router)
    return TestClient(app)


def test_metrics_have_labels_and_units(db_session):
    res = _client().get("/api/metrics")
    assert res.status_code == 200
    by_id = {m["id"]: m for m in res.json()}
    assert by_id["dso_days"]["unit"] == "days"
    assert by_id["cash_balance_cr"]["label"] == "Cash balance"
    assert by_id["cash_balance_cr"]["adverse_direction"] == "down"


def test_signal_history_covers_quarter(seeded_erp, db_session):
    res = _client().get("/api/signals/history?metrics=cash_balance_cr,dso_days&n=13")
    assert res.status_code == 200
    body = res.json()
    assert len(body["cash_balance_cr"]["points"]) == 13
    assert body["cash_balance_cr"]["points"][-1]["week"] == seeded_erp["weeks"][-1]
    assert all(p["value"] is not None for p in body["cash_balance_cr"]["points"])


def _digest(db_session, week, items):
    wv = get_active_weighting(db_session)
    run = PipelineRun(week=week, weighting_version_id=wv.id, status="completed")
    db_session.add(run)
    db_session.flush()
    d = persist_digest(db_session, week=week, weighting=wv, kra_version=upsert_kra_version(db_session),
                       run=run, headline=f"h {week}", surfaced=[], empty_bar=not items, data_health={})
    for rank, (title, sid, red) in enumerate(items, start=1):
        db_session.add(DigestItem(digest_id=d.id, rank=rank, title=title, signal_ids_json=[sid] if sid else [],
                                  score_decomp_json={} if sid else {"red_line": "RL-2"}, is_red_line=red))
    db_session.commit()
    return d


def test_briefing_reports_what_changed(seeded_erp, db_session):
    w = seeded_erp["weeks"]
    _digest(db_session, w[-2], [("DSO up", f"metric:dso_days:{w[-2]}", False),
                                ("Opex up", f"metric:opex_ratio_pct:{w[-2]}", False)])
    latest = _digest(db_session, w[-1], [("GST notice", None, True),
                                         ("DSO still up", f"metric:dso_days:{w[-1]}", False)])
    body = _client().get("/api/briefing").json()
    assert body["digest_id"] == latest.id and body["previous_week"] == w[-2]
    assert [c["title"] for c in body["changes"]["new"]] == ["GST notice"]
    assert [c["title"] for c in body["changes"]["continuing"]] == ["DSO still up"]
    assert [c["title"] for c in body["changes"]["dropped"]] == ["Opex up"]
    cash = next(k for k in body["kpis"] if k["id"] == "cash_balance_cr")
    assert cash["value"] is not None and cash["previous"] is not None


def test_briefing_empty_without_digests(db_session):
    assert _client().get("/api/briefing").json() == {"digest_id": None}


def test_weighting_preview_is_dry_run(seeded_erp, db_session):
    before = db_session.query(Signal).count()
    res = _client().post("/api/weighting/preview", json={"content": load_preset("derisking")})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["week"] == seeded_erp["weeks"][-1]
    assert body["active"]["top"] and body["draft"]["top"]
    assert "label" in body["draft"]["top"][0]
    db_session.expire_all()
    assert db_session.query(Signal).count() == before, "preview must not persist signals"


def test_weighting_preview_rejects_invalid(seeded_erp, db_session):
    bad = dict(load_preset("derisking"), kra_ranks=["NOPE-1"])
    res = _client().post("/api/weighting/preview", json={"content": bad})
    assert res.status_code == 422
