"""Driver breakdowns: what moved a metric, computed from records and carried into
the synthesis, the citation gate and the stored digest item."""

from __future__ import annotations

import json
import re
from datetime import timedelta

from cfo_agent.drivers import compute_drivers, driver_citations
from cfo_agent.models import DigestItem, ErpCustomer, ErpReceipt, ErpSalesInvoice, PipelineRun
from cfo_agent.pipeline.schemas import ConnectOutput, HeadlineOutput, SectionBlock, SynthesizeItemOutput
from cfo_agent.settings import get_settings
from cfo_agent.timeutil import week_monday
from cfo_agent.weighting import get_active_weighting

WEEK = "2026-W30"
MON = week_monday(WEEK)


def test_receivable_drivers_rank_customers_by_change(db_session):
    s = db_session
    a = ErpCustomer(business_key="C-A", name="Alpha", segment="X")
    b = ErpCustomer(business_key="C-B", name="Beta", segment="X")
    ic = ErpCustomer(business_key="C-IC", name="Group co", segment="X", is_intercompany=True)
    s.add_all([a, b, ic])
    s.flush()
    old, new = MON - timedelta(days=40), MON + timedelta(days=2)
    s.add_all([
        # Alpha: 100 open before, a further 900 raised this week → +900
        ErpSalesInvoice(business_key="A-1", customer_id=a.id, invoice_date=old, due_date=old + timedelta(days=30), amount_paise=100_00, segment="X"),
        ErpSalesInvoice(business_key="A-2", customer_id=a.id, invoice_date=new, due_date=new + timedelta(days=30), amount_paise=900_00, segment="X"),
        # Beta: 500 open before, paid 400 this week → −400
        ErpSalesInvoice(business_key="B-1", customer_id=b.id, invoice_date=old, due_date=old + timedelta(days=30), amount_paise=500_00, segment="X"),
        # Intercompany never appears
        ErpSalesInvoice(business_key="IC-1", customer_id=ic.id, invoice_date=new, due_date=new + timedelta(days=30), amount_paise=9_000_00, segment="X"),
    ])
    s.flush()
    b1 = s.query(ErpSalesInvoice).filter_by(business_key="B-1").one()
    s.add(ErpReceipt(business_key="RC-B", invoice_id=b1.id, receipt_date=new, amount_paise=400_00))
    s.flush()

    d = compute_drivers(s, "dso_days", WEEK)
    assert [(r["label"], r["change_paise"]) for r in d["rows"]] == [("Alpha", 900_00), ("Beta", -400_00)]
    assert d["rows"][0]["citation"] == "erp:erp_sales_invoices:A-2"  # Alpha's largest open invoice
    assert d["reconciliation"] == {"total_current_paise": 1_100_00, "total_previous_paise": 600_00}

    # Overdue-only view: only invoices past due at week-end count
    od = compute_drivers(s, "ar_overdue_pct", WEEK)
    assert {r["label"] for r in od["rows"]} == {"Beta"} and od["rows"][0]["current_paise"] == 100_00


def test_no_records_or_unknown_metric_gives_no_drivers(db_session):
    assert compute_drivers(db_session, "dso_days", WEEK) is None
    assert compute_drivers(db_session, "some_custom_metric", WEEK) is None
    assert compute_drivers(db_session, None, WEEK) is None


def test_cash_drivers_state_what_the_feed_does_not_explain(seeded_erp, db_session):
    d = compute_drivers(db_session, "cash_balance_cr", seeded_erp["weeks"][-1])
    rec = d["reconciliation"]
    assert rec["net_recorded_flow_paise"] == rec["inflows_paise"] - rec["outflows_paise"]
    assert rec["not_in_feed_paise"] == rec["balance_change_paise"] - rec["net_recorded_flow_paise"]
    assert all(r["citation"] for r in d["rows"])


def test_drivers_reach_the_model_the_gate_and_the_stored_item(seeded_erp, db_session, monkeypatch):
    """The synthesis prompt carries the driver rows; a driver's record is citable;
    the digest item keeps the breakdown for the card."""
    from cfo_agent.pipeline.orchestrator import reserve_week, run_pipeline

    seen: list[dict] = []

    def fake_parse(schema, *, system, user, purpose, budget=None, **_):
        if budget is not None:
            budget.check()
            budget.calls += 1
        if schema is ConnectOutput:
            return ConnectOutput(connections=[])
        if schema is HeadlineOutput:
            return HeadlineOutput(headline="h")
        allowed = json.loads(re.search(r"ALLOWED_CITATIONS: (\[.*?\])\n", user).group(1))
        item = json.loads(user.split("ITEM:\n", 1)[1])
        seen.append(item)
        rows = (item.get("drivers") or {}).get("rows") or []
        cite = [rows[0]["citation"]] if rows else allowed[:1]
        assert cite[0] in allowed
        text = f"{rows[0]['label']} accounts for {rows[0]['current_inr']}." if rows else "Moved."
        return SynthesizeItemOutput(
            title="t", what=SectionBlock(text=text, citations=cite), why=SectionBlock(text="Matters.", citations=cite),
            connects=SectionBlock(text="", citations=[]), action=SectionBlock(text="Act.", citations=cite),
        )

    monkeypatch.setattr("cfo_agent.pipeline.step5_synthesize.archival_search", lambda *a, **k: [])
    monkeypatch.setattr("cfo_agent.pipeline.orchestrator.archival_insert", lambda *a, **k: None)
    monkeypatch.setattr("cfo_agent.pipeline.orchestrator.has_unconsolidated_activity", lambda s: False)
    monkeypatch.setattr("cfo_agent.pipeline.orchestrator.send_digest_email", lambda s, d: d)
    monkeypatch.setattr("cfo_agent.pipeline.step3_connect.parse", fake_parse)
    monkeypatch.setattr("cfo_agent.pipeline.step5_synthesize.parse", fake_parse)

    week = seeded_erp["weeks"][-1]
    wv = get_active_weighting(db_session)
    run = reserve_week(db_session, week, wv.id)
    run_pipeline(run.id, week, wv.id)
    db_session.expire_all()
    run = db_session.get(PipelineRun, run.id)
    assert run.status == "completed", run.error
    assert not run.suppressed_json, run.suppressed_json

    with_drivers = [i for i in seen if i.get("drivers")]
    assert with_drivers, "at least one surfaced item should carry a breakdown"
    row = with_drivers[0]["drivers"]["rows"][0]
    assert row["current_inr"].startswith(("₹", "-₹"))  # pre-formatted: the model never converts paise
    assert "drivers" not in (with_drivers[0].get("signal") or {}).get("score_decomp", {})

    stored = [i for i in db_session.query(DigestItem).filter_by(digest_id=run.digest_id) if (i.score_decomp_json or {}).get("drivers")]
    assert stored
    for it in stored:
        cited = {c for v in it.citations_json.values() for c in v}
        assert cited & set(driver_citations(it.score_decomp_json["drivers"]))
        assert not it.verification_json.get("unverified_figures"), it.verification_json


def test_deterministic_fallback_names_the_largest_contributor(seeded_erp, db_session, monkeypatch):
    from cfo_agent.pipeline.orchestrator import reserve_week, run_pipeline

    monkeypatch.setattr("cfo_agent.pipeline.orchestrator.archival_insert", lambda *a, **k: None)
    monkeypatch.setattr("cfo_agent.pipeline.orchestrator.has_unconsolidated_activity", lambda s: False)
    monkeypatch.setattr("cfo_agent.pipeline.orchestrator.send_digest_email", lambda s, d: d)
    monkeypatch.setattr("cfo_agent.pipeline.step5_synthesize.archival_search", lambda *a, **k: [])
    monkeypatch.setenv("MAX_LLM_COST_USD_PER_RUN", "0")  # no LLM at all → deterministic text
    get_settings.cache_clear()

    week = seeded_erp["weeks"][-1]
    wv = get_active_weighting(db_session)
    run = reserve_week(db_session, week, wv.id)
    run_pipeline(run.id, week, wv.id)
    db_session.expire_all()
    run = db_session.get(PipelineRun, run.id)
    assert run.status == "completed", run.error
    items = [i for i in db_session.query(DigestItem).filter_by(digest_id=run.digest_id) if (i.score_decomp_json or {}).get("drivers")]
    assert items and all("Largest contributor:" in i.what or "is not in the feed" in i.what for i in items)
    assert all(not i.verification_json.get("unverified_figures") for i in items), [i.verification_json for i in items]
    get_settings.cache_clear()


def test_prompt_carries_label_and_earlier_findings_and_stays_in_evidence(seeded_erp, db_session, monkeypatch):
    """Later items are told what higher-ranked items already reported, every item is
    told to use the display label, and the fallback text uses labels with units."""
    from cfo_agent.pipeline.orchestrator import reserve_week, run_pipeline

    prompts: list[str] = []

    def fake_parse(schema, *, system, user, purpose, budget=None, **_):
        if budget is not None:
            budget.check()
            budget.calls += 1
        if schema is ConnectOutput:
            return ConnectOutput(connections=[])
        if schema is HeadlineOutput:
            return HeadlineOutput(headline="h")
        prompts.append(user)
        allowed = json.loads(re.search(r"ALLOWED_CITATIONS: (\[.*?\])\n", user).group(1))
        n = len(prompts)
        return SynthesizeItemOutput(
            title=f"Finding {n}", what=SectionBlock(text="x", citations=allowed[:1]),
            why=SectionBlock(text="y", citations=allowed[:1]), connects=SectionBlock(text="", citations=[]),
            action=SectionBlock(text="z", citations=allowed[:1]),
        )

    monkeypatch.setattr("cfo_agent.pipeline.step5_synthesize.archival_search", lambda *a, **k: [])
    monkeypatch.setattr("cfo_agent.pipeline.orchestrator.archival_insert", lambda *a, **k: None)
    monkeypatch.setattr("cfo_agent.pipeline.orchestrator.has_unconsolidated_activity", lambda s: False)
    monkeypatch.setattr("cfo_agent.pipeline.orchestrator.send_digest_email", lambda s, d: d)
    monkeypatch.setattr("cfo_agent.pipeline.step3_connect.parse", fake_parse)
    monkeypatch.setattr("cfo_agent.pipeline.step5_synthesize.parse", fake_parse)

    week = seeded_erp["weeks"][-1]
    wv = get_active_weighting(db_session)
    run = reserve_week(db_session, week, wv.id)
    run_pipeline(run.id, week, wv.id)
    assert len(prompts) >= 2
    first = json.loads(prompts[0].split("ITEM:\n", 1)[1])
    second = json.loads(prompts[1].split("ITEM:\n", 1)[1])
    assert "already_reported_above" not in first
    assert second["already_reported_above"] == ["Finding 1 — x"]  # title and the start of 'what'
    for p in prompts:
        item = json.loads(p.split("ITEM:\n", 1)[1])
        if item.get("signal"):
            assert item["signal"]["metric_label"] and "_" not in item["signal"]["metric_label"]

    from cfo_agent.pipeline.fallback import _val
    assert _val("cash_balance_cr", 21.4) == "₹21.4 Cr" and _val("ar_overdue_pct", 35.7) == "35.7%"
    assert _val("dso_days", 73.06) == "73.06 days" and _val("gst_open_notices", 2.0) == "2"
