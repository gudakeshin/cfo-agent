"""Drill-down chat: read-only data tools, citation gate on answers, SSE streaming.
The model is replaced by a scripted fake of llm.stream_round."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cfo_agent.api import api_router
from cfo_agent.chat import run_chat
from cfo_agent.chat_tools import ChatDataContext
from cfo_agent.digest.builder import persist_digest
from cfo_agent.kra import upsert_kra_version
from cfo_agent.models import DigestItem, ErpSalesInvoice, PipelineRun
from cfo_agent.pipeline.step1_map import step1_map
from cfo_agent.timeutil import week_sunday
from cfo_agent.weighting import get_active_weighting


@pytest.fixture()
def item(seeded_erp, db_session, monkeypatch):
    monkeypatch.setattr("cfo_agent.memory.recall.embed_chat_message", lambda *a, **k: None)
    week = seeded_erp["weeks"][-1]
    step1_map(db_session, week)
    wv = get_active_weighting(db_session)
    run = PipelineRun(week=week, weighting_version_id=wv.id, status="completed")
    db_session.add(run)
    db_session.flush()
    d = persist_digest(db_session, week=week, weighting=wv, kra_version=upsert_kra_version(db_session),
                       run=run, headline="h", surfaced=[], empty_bar=False, data_health={})
    cid = f"metric:cash_balance_cr:{week}"
    it = DigestItem(digest_id=d.id, rank=1, title="Cash below buffer", what="w", why="y",
                    citations_json={"what": [cid], "why": [cid]}, signal_ids_json=[cid])
    db_session.add(it)
    db_session.commit()
    return it


def script(rounds):
    """Fake stream_round that replays scripted turns: each is (blocks, stop_reason)."""
    calls = {"n": 0, "messages": []}

    def fake(*, system, messages, tools, purpose, budget=None, on_text=None, max_tokens=2048):
        if budget is not None:
            budget.check()
            budget.calls += 1
        blocks, stop = rounds[calls["n"]]
        calls["n"] += 1
        calls["messages"].append(list(messages))
        for b in blocks:
            if b["type"] == "text" and on_text:
                for chunk in (b["text"][:10], b["text"][10:]):
                    if chunk:
                        on_text(chunk)
        return blocks, stop

    return fake, calls


def _tool(name, args, tid="t1"):
    return {"type": "tool_use", "id": tid, "name": name, "input": args}


def _text(t):
    return {"type": "text", "text": t}


# ── data tools ──────────────────────────────────────────────────────────────

def test_receivables_ageing_buckets_add_up(item, db_session, seeded_erp):
    ctx = ChatDataContext(db_session, seeded_erp["weeks"][-1])
    r = ctx.receivables_ageing()
    assert abs(sum(r["buckets_cr"].values()) - r["total_open_cr"]) < 0.05
    assert r["top_overdue_invoices"]
    assert r["totals_citation"] == f"metric:ar_overdue_pct:{seeded_erp['weeks'][-1]}"
    assert all(c.startswith("erp:erp_sales_invoices:") for c in r["citations"][1:])


def test_list_records_never_reads_past_as_of_week(item, db_session, seeded_erp):
    week = seeded_erp["weeks"][-3]
    ctx = ChatDataContext(db_session, week)
    r = ctx.list_records("sales_invoices", date_to="2099-01-01", limit=25)
    assert r["rows"]
    assert all(row["invoice_date"] <= week_sunday(week).isoformat() for row in r["rows"])
    assert "amount_inr" in r["rows"][0] and "customer" in r["rows"][0]


def test_what_if_cash_outflow(item, db_session, seeded_erp):
    ctx = ChatDataContext(db_session, seeded_erp["weeks"][-1])
    r = ctx.what_if("cash_outflow", amount_cr=5)
    assert r["cash_after_cr"] == round(r["cash_now_cr"] - 5, 2)
    assert r["headroom_after_cr"] == round(r["cash_after_cr"] - r["covenant_minimum_cr"], 2)
    assert any(c.startswith("erp:erp_covenants:") for c in r["citations"])


def test_get_metric_is_citable(item, db_session, seeded_erp):
    from cfo_agent.citations import resolve_citation

    ctx = ChatDataContext(db_session, seeded_erp["weeks"][-2])
    r = ctx.get_metric("dso_days")
    assert resolve_citation(db_session, r["citations"][0]).ok


# ── answer gate ─────────────────────────────────────────────────────────────

def test_answer_keeps_tool_citations_and_strips_forged(item, db_session, seeded_erp, monkeypatch):
    week = seeded_erp["weeks"][-1]
    forged = f"erp:erp_sales_invoices:{db_session.query(ErpSalesInvoice).first().business_key}"
    ctx = ChatDataContext(db_session, week)
    after = ctx.what_if("cash_outflow", amount_cr=5)["cash_after_cr"]
    cash_cid = [c for c in ctx.what_if("cash_outflow", amount_cr=5)["citations"] if "cash_daily" in c][0]
    answer = (
        f"After a ₹5 Cr outflow cash would be ₹{after} Cr [{cash_cid}]. "
        f"One invoice is ₹99.99 Cr [{forged}]."
    )
    fake, calls = script([
        ([_text("Let me check."), _tool("what_if", {"scenario": "cash_outflow", "amount_cr": 5})], "tool_use"),
        ([_text(answer)], "end_turn"),
    ])
    monkeypatch.setattr("cfo_agent.llm.stream_round", fake)
    events = []
    msg = run_chat(db_session, item.id, "What if we pay ₹5 Cr?", emit=lambda e, d: events.append(e))
    assert cash_cid in msg.citations_json
    assert forged not in msg.content and forged in msg.verification_json["rejected_citations"]
    assert "99.99" in msg.verification_json["unverified_figures"]
    assert msg.verification_json["tools_used"] == ["what_if"]
    assert events[:3] == ["delta", "delta", "reset"] and "tool" in events and events[-1] == "done"
    # the tool result went back to the model
    assert calls["messages"][1][-1]["content"][0]["type"] == "tool_result"


def test_uncited_answer_is_flagged(item, db_session, monkeypatch):
    fake, _ = script([([_text("Cash looks tight.")], "end_turn")])
    monkeypatch.setattr("cfo_agent.llm.stream_round", fake)
    msg = run_chat(db_session, item.id, "How is cash?")
    assert msg.verification_json["uncited"] is True


# ── SSE ─────────────────────────────────────────────────────────────────────

def test_stream_endpoint_emits_events(item, db_session, monkeypatch):
    fake, _ = script([([_text("Short answer with no figures.")], "end_turn")])
    monkeypatch.setattr("cfo_agent.llm.stream_round", fake)
    app = FastAPI()
    app.include_router(api_router)
    with TestClient(app) as client:
        res = client.post(f"/api/chat/{item.id}/stream", json={"message": "Hi"})
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        events = [blk.split("\n") for blk in res.text.strip().split("\n\n")]
        names = [e[0].removeprefix("event: ") for e in events]
        assert names[0] == "delta" and names[-1] == "done"
        done = json.loads(events[-1][1].removeprefix("data: "))
        assert done["message"]["content"] == "Short answer with no figures."
        hist = client.get(f"/api/chat/{item.id}").json()
        assert [m["role"] for m in hist] == ["user", "assistant"]
        assert "verification" in hist[1]


def test_stream_endpoint_404(db_session):
    app = FastAPI()
    app.include_router(api_router)
    with TestClient(app) as client:
        assert client.post("/api/chat/999999/stream", json={"message": "x"}).status_code == 404
