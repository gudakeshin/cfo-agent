"""Digest export (PDF, PowerPoint) and read-only share links."""

from __future__ import annotations

import io
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cfo_agent.api import api_router
from cfo_agent.digest.builder import persist_digest
from cfo_agent.digest.export import digest_payload, render_pdf, render_pptx
from cfo_agent.kra import upsert_kra_version
from cfo_agent.models import AuditEvent, DigestItem, PipelineRun, ShareLink
from cfo_agent.pipeline.step1_map import step1_map
from cfo_agent.weighting import get_active_weighting


@pytest.fixture()
def digest(seeded_erp, db_session):
    weeks = seeded_erp["weeks"]
    for w in weeks[-4:]:
        step1_map(db_session, w)
    week = weeks[-1]
    wv = get_active_weighting(db_session)
    run = PipelineRun(week=week, weighting_version_id=wv.id, status="completed")
    db_session.add(run)
    db_session.flush()
    d = persist_digest(db_session, week=week, weighting=wv, kra_version=upsert_kra_version(db_session),
                       run=run, headline="Cash ₹21.4 Cr sits below the ₹22 Cr buffer", surfaced=[],
                       empty_bar=False, data_health={})
    cid = f"metric:cash_balance_cr:{week}"
    db_session.add(DigestItem(digest_id=d.id, rank=1, title="Cash below buffer", what="Cash is ₹21.4 Cr.",
                              why="Covenant headroom is thin.", action="Defer the capex draw.",
                              citations_json={"what": [cid], "why": [cid]}, signal_ids_json=[cid],
                              kra_ids_json=["STW-2"], is_red_line=True,
                              score_decomp_json={"metric_id": "cash_balance_cr", "value": 21.4}))
    db_session.commit()
    return d


def test_payload_has_sources_history_and_kra_names(digest, db_session):
    p = digest_payload(db_session, digest)
    it = p["items"][0]
    assert it["priority"] == "Red line" and it["kras"][0]["name"]
    assert it["sources"][0]["n"] == 1 and it["sources"][0]["id"].startswith("metric:cash_balance_cr")
    assert len(it["metric"]["history"]) == 4
    assert "score_decomp" not in it  # internals are not exported


def test_pdf_and_pptx_render(digest, db_session):
    p = digest_payload(db_session, digest)
    pdf = render_pdf(p)
    assert pdf[:5] == b"%PDF-" and len(pdf) > 2000
    from pptx import Presentation

    prs = Presentation(io.BytesIO(render_pptx(p)))
    texts = [sh.text_frame.text for s in prs.slides for sh in s.shapes if sh.has_text_frame]
    assert len(prs.slides) == 4  # title, summary, 1 insight, method
    assert any("Cash below buffer" in t for t in texts)
    assert any(sh.has_chart for s in prs.slides for sh in s.shapes)


def _client():
    app = FastAPI()
    app.include_router(api_router)
    return TestClient(app)


def test_export_endpoints(digest):
    c = _client()
    r = c.get(f"/api/digests/{digest.id}/export.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert "attachment" in r.headers["content-disposition"]
    assert c.get(f"/api/digests/{digest.id}/export.pptx").status_code == 200
    assert c.get(f"/api/digests/{digest.id}/export.docx").status_code == 404


def test_share_link_lifecycle(digest, db_session):
    c = _client()
    link = c.post(f"/api/digests/{digest.id}/share", json={"days": 3, "note": "Board"}).json()
    token = link["url"].rsplit("/", 1)[-1]
    r = c.get(f"/api/shared/{token}")
    assert r.status_code == 200 and r.json()["headline"].startswith("Cash")
    assert "run_audit" not in r.json()
    assert c.get(f"/api/shared/{token}/export.pdf").status_code == 200
    db_session.expire_all()
    assert db_session.get(ShareLink, link["id"]).views == 1
    assert db_session.query(AuditEvent).filter_by(user_email=f"share-link:{link['id']}").count() == 2
    # tampered token, revoked link, expired link → same 404
    assert c.get(f"/api/shared/{token[:-2]}xx").status_code == 404
    c.post(f"/api/shares/{link['id']}/revoke")
    assert c.get(f"/api/shared/{token}").status_code == 404
    link2 = c.post(f"/api/digests/{digest.id}/share", json={}).json()
    row = db_session.get(ShareLink, link2["id"])
    row.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db_session.commit()
    assert c.get(f"/api/shared/{link2['url'].rsplit('/', 1)[-1]}").status_code == 404


def test_shared_route_is_public_under_auth(digest, db_session, monkeypatch):
    from cfo_agent.api.sharing import make_share_token
    from cfo_agent.settings import get_settings

    link = ShareLink(digest_id=digest.id, created_by="cfo@x", expires_at=datetime.utcnow() + timedelta(days=1))
    db_session.add(link)
    db_session.commit()
    monkeypatch.setenv("AUTH_MODE", "local")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    get_settings.cache_clear()
    c = _client()
    assert c.get(f"/api/digests/{digest.id}").status_code == 401
    assert c.get(f"/api/shared/{make_share_token(link.id)}").status_code == 200
    get_settings.cache_clear()
