"""The Ask box: questions about a whole briefing, private to each user, answers gated."""

from __future__ import annotations

import json

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cfo_agent.api import api_router
from cfo_agent.ask import ask_history, briefing_context, run_ask
from cfo_agent.digest.builder import persist_digest
from cfo_agent.kra import upsert_kra_version
from cfo_agent.models import AskMessage, DigestItem, PipelineRun
from cfo_agent.pipeline.step1_map import step1_map
from cfo_agent.security import hash_password
from cfo_agent.settings import get_settings
from cfo_agent.weighting import get_active_weighting


def _text(t):
    return {"type": "text", "text": t}


def _tool(name, args, tid="t1"):
    return {"type": "tool_use", "id": tid, "name": name, "input": args}


def script(rounds):
    calls = {"n": 0, "systems": [], "messages": []}

    def fake(*, system, messages, tools, purpose, budget=None, on_text=None, max_tokens=2048):
        if budget is not None:
            budget.check()
            budget.calls += 1
        blocks, stop = rounds[calls["n"]]
        calls["n"] += 1
        calls["systems"].append(system)
        calls["messages"].append(list(messages))
        for b in blocks:
            if b["type"] == "text" and on_text:
                on_text(b["text"])
        return blocks, stop

    return fake, calls


@pytest.fixture()
def briefing(seeded_erp, db_session):
    """A stored digest with two insights, one carrying a driver breakdown."""
    week = seeded_erp["weeks"][-1]
    step1_map(db_session, week)
    wv = get_active_weighting(db_session)
    run = PipelineRun(week=week, weighting_version_id=wv.id, status="completed")
    db_session.add(run)
    db_session.flush()
    d = persist_digest(db_session, week=week, weighting=wv, kra_version=upsert_kra_version(db_session),
                       run=run, headline="Cash is tight", surfaced=[], empty_bar=False, data_health={})
    cash = f"metric:cash_balance_cr:{week}"
    dso = f"metric:dso_days:{week}"
    rcp = db_session.query(__import__("cfo_agent.models", fromlist=["ErpReceipt"]).ErpReceipt).first().business_key
    drivers = {"title": "t", "basis": "b", "reconciliation": None, "rows": [
        {"label": "Apex", "group": "received from customer", "current_paise": 124_00000000 // 100, "previous_paise": 0,
         "change_paise": 124_00000000 // 100, "citation": f"erp:erp_receipts:{rcp}", "note": ""}]}
    db_session.add_all([
        DigestItem(digest_id=d.id, rank=1, title="Cash below buffer", what="Cash fell.", why="Covenant.", action="Hold outflows.",
                   citations_json={"what": [cash], "why": [cash]}, signal_ids_json=[cash], is_red_line=True,
                   score_decomp_json={"drivers": drivers, "continuity": {"status": "new"}}),
        DigestItem(digest_id=d.id, rank=2, title="DSO rising", what="DSO up.", why="Collections.", action="Chase.",
                   citations_json={"what": [dso], "why": [dso]}, signal_ids_json=[dso],
                   score_decomp_json={"continuity": {"status": "continuing", "since_week": "2026-W20", "last_week": "2026-W27", "weeks_running": 3}}),
    ])
    db_session.commit()
    return d


def test_context_holds_every_insight_and_its_drivers(briefing, db_session):
    text, allowed, values = briefing_context(db_session, briefing)
    assert "#1 [RED LINE] Cash below buffer (new this week)" in text
    assert "#2 DSO rising (continuing since 2026-W20)" in text
    assert "WHAT MOVED IT:" in text and "Apex" in text
    assert any(c.startswith("erp:erp_receipts:") for c in allowed)  # a driver record is citable
    assert any(c.startswith("metric:cash_balance_cr:") for c in allowed) and values


def test_answer_is_gated_and_history_is_per_user(briefing, db_session, monkeypatch):
    week = briefing.week
    cash = f"metric:cash_balance_cr:{week}"
    forged = "erp:erp_sales_invoices:INV-FORGED"
    fake, calls = script([([_text(f"Cash is the urgent one [{cash}]. Also ₹77.77 Cr [{forged}].")], "end_turn")])
    monkeypatch.setattr("cfo_agent.llm.stream_round", fake)

    events = []
    msg = run_ask(db_session, "cfo@client.com", None, "What is most urgent?", emit=lambda e, d: events.append(e))
    db_session.commit()
    assert cash in msg.citations_json and forged not in msg.content
    assert forged in msg.verification_json["rejected_citations"] and "77.77" in msg.verification_json["unverified_figures"]
    assert events[-1] == "done"
    assert "Cash below buffer" in calls["systems"][0] and "Apex" in calls["systems"][0]  # the briefing is in context

    # Another user sees nothing of it, and cannot see it through their own question's history
    assert ask_history(db_session, "other@client.com", briefing.id) == []
    fake2, calls2 = script([([_text("Nothing cited.")], "end_turn")])
    monkeypatch.setattr("cfo_agent.llm.stream_round", fake2)
    run_ask(db_session, "other@client.com", None, "Hello?")
    assert calls2["messages"][0] == [{"role": "user", "content": "Hello?"}]  # no leakage of the first user's turns

    # A follow-up by the first user carries their earlier turns to the model
    fake3, calls3 = script([([_text("Follow-up.")], "end_turn")])
    monkeypatch.setattr("cfo_agent.llm.stream_round", fake3)
    run_ask(db_session, "cfo@client.com", None, "And DSO?")
    roles = [m["role"] for m in calls3["messages"][0]]
    assert roles == ["user", "assistant", "user"]


def test_no_briefing_is_a_clear_error(db_session):
    with pytest.raises(ValueError, match="no briefing"):
        run_ask(db_session, "a@b.c", None, "Hi")


@pytest.fixture()
def local_auth(db_session, tmp_path, monkeypatch):
    pws = {"viewer": "viewer-password-1", "cfo": "cfo-password-123", "cfo2": "cfo2-password-123"}
    users = [{"email": f"{r}@client.com", "name": r, "role": "viewer" if r == "viewer" else "cfo",
              "password_hash": hash_password(pw)} for r, pw in pws.items()]
    f = tmp_path / "users.yaml"
    f.write_text(yaml.safe_dump({"users": users}))
    monkeypatch.setenv("AUTH_MODE", "local")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("LOCAL_USERS_FILE", str(f))
    get_settings.cache_clear()
    from cfo_agent.auth import throttle

    throttle._fails.clear()
    yield pws
    get_settings.cache_clear()


def _login(client, who, pws):
    r = client.post("/api/auth/login", json={"email": f"{who}@client.com", "password": pws[who]},
                    headers={"X-Requested-With": "fetch"})
    assert r.status_code == 200, r.text


H = {"X-Requested-With": "fetch"}


def test_api_roles_privacy_stream_and_clear(briefing, db_session, local_auth, monkeypatch):
    fake, _ = script([([_text("Short answer.")], "end_turn")] * 3)
    monkeypatch.setattr("cfo_agent.llm.stream_round", fake)
    app = FastAPI()
    app.include_router(api_router)

    with TestClient(app) as viewer, TestClient(app) as cfo, TestClient(app) as cfo2:
        _login(viewer, "viewer", local_auth)
        _login(cfo, "cfo", local_auth)
        _login(cfo2, "cfo2", local_auth)

        # viewers can neither ask nor read the Ask history
        assert viewer.post("/api/ask", json={"message": "x"}, headers=H).status_code == 403
        assert viewer.get("/api/ask").status_code == 403

        res = cfo.post("/api/ask/stream", json={"message": "Hi"}, headers=H)
        assert res.status_code == 200 and res.headers["content-type"].startswith("text/event-stream")
        names = [b.split("\n")[0].removeprefix("event: ") for b in res.text.strip().split("\n\n")]
        assert names[0] == "delta" and names[-1] == "done"

        mine = cfo.get("/api/ask").json()
        assert mine["digest_id"] == briefing.id and [m["role"] for m in mine["messages"]] == ["user", "assistant"]
        assert cfo2.get("/api/ask").json()["messages"] == []  # private to the user

        # clearing deletes only the caller's own history
        assert cfo2.post("/api/ask/clear", json={}, headers=H).json() == {"cleared": 0}
        assert cfo.get("/api/ask").json()["messages"]
        assert cfo.post("/api/ask/clear", json={}, headers=H).json() == {"cleared": 2}
        assert cfo.get("/api/ask").json()["messages"] == []
    assert db_session.query(AskMessage).count() == 0


def test_ask_without_a_briefing_returns_empty_then_404(db_session):
    app = FastAPI()
    app.include_router(api_router)
    with TestClient(app) as client:
        assert client.get("/api/ask").json() == {"digest_id": None, "week": None, "headline": None, "messages": []}
        assert client.post("/api/ask/stream", json={"message": "x"}).status_code == 404
        assert client.post("/api/ask", json={"message": "x"}).status_code == 404
