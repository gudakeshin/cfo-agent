"""Impact and deadline of acting on an insight: worked figures, and no figure where the
records do not support one."""

from __future__ import annotations

from datetime import date, timedelta

from cfo_agent.impact import compute_impact
from cfo_agent.models import (
    ErpCashDaily,
    ErpCovenant,
    ErpCustomer,
    ErpGlEntry,
    ErpGstEvent,
    ErpPurchaseInvoice,
    ErpSalesInvoice,
)
from cfo_agent.settings import get_settings

WEEK = "2026-W30"  # ends Sunday 26 Jul 2026
END = date(2026, 7, 26)


def cr(x: float) -> int:
    return int(x * 10_000_000 * 100)


def _sales(s):
    a = ErpCustomer(business_key="CA", name="Apex Industries", segment="X")
    b = ErpCustomer(business_key="CB", name="Nexus Retail", segment="X")
    c = ErpCustomer(business_key="CC", name="Orbit Foods", segment="X")
    s.add_all([a, b, c])
    s.flush()
    s.add_all([
        ErpSalesInvoice(business_key="SI-A", customer_id=a.id, invoice_date=date(2026, 7, 2), due_date=date(2026, 7, 10),
                        amount_paise=cr(10), segment="X"),
        ErpSalesInvoice(business_key="SI-B", customer_id=b.id, invoice_date=date(2026, 7, 5), due_date=date(2026, 7, 20),
                        amount_paise=cr(4), segment="X"),
        ErpSalesInvoice(business_key="SI-C", customer_id=c.id, invoice_date=date(2026, 7, 20), due_date=date(2026, 8, 30),
                        amount_paise=cr(1), segment="X"),
    ])
    s.flush()


def test_receivables_impact_names_who_to_chase(db_session):
    _sales(db_session)
    r = compute_impact(db_session, "ar_overdue_pct", WEEK)
    assert r["amount_paise"] == cr(14)  # A and B are past due; C is not yet
    assert r["amount_inr"] == "₹14.00 Cr"
    assert "Apex Industries (₹10.00 Cr), Nexus Retail (₹4.00 Cr)" in r["sentence"]
    assert "Orbit" not in r["sentence"]
    assert r["citations"] == ["erp:erp_sales_invoices:SI-A", "erp:erp_sales_invoices:SI-B"]
    assert r["deadline"] is None  # nothing in the records sets a date for this


def test_dso_adds_what_one_day_is_worth(db_session):
    _sales(db_session)
    r = compute_impact(db_session, "dso_days", WEEK)
    # 15 Cr invoiced in the last 30 days ÷ 30 = ₹0.5 Cr a day
    assert r["per_day_paise"] == cr(0.5) and r["per_day_inr"] == "₹50.00 L"
    assert "Each day taken off DSO releases about ₹50.00 L" in r["sentence"]


def test_payables_impact(db_session):
    db_session.add_all([
        ErpPurchaseInvoice(business_key="PI-1", vendor_name="Steel Co", invoice_date=date(2026, 6, 1),
                           due_date=date(2026, 7, 1), amount_paise=cr(6)),
        ErpPurchaseInvoice(business_key="PI-2", vendor_name="Pack Ltd", invoice_date=date(2026, 7, 20),
                           due_date=date(2026, 8, 20), amount_paise=cr(2)),
    ])
    db_session.flush()
    r = compute_impact(db_session, "ap_overdue_pct", WEEK)
    assert r["amount_paise"] == cr(6) and "Steel Co (₹6.00 Cr)" in r["sentence"]


def test_cash_shortfall_to_the_covenant_buffer(db_session):
    db_session.add(ErpCovenant(business_key="COV-C", name="Cash", metric="cash_balance_cr",
                               threshold_paise=cr(20), buffered_threshold_paise=cr(22), operator="gte"))
    db_session.add(ErpCashDaily(business_key="CASH-1", as_of_date=END, account_key="PRIMARY", balance_paise=cr(21.4)))
    db_session.flush()
    r = compute_impact(db_session, "cash_balance_cr", WEEK)
    assert r["amount_paise"] == cr(0.6) and "₹60.00 L" in r["sentence"]
    assert "already breached" not in r["sentence"]
    db_session.query(ErpCashDaily).update({"balance_paise": cr(19)})
    db_session.flush()
    r = compute_impact(db_session, "cash_balance_cr", WEEK)
    assert r["amount_paise"] == cr(3) and "already breached" in r["sentence"]


def test_cash_comfortably_above_the_buffer_has_no_impact(db_session):
    db_session.add(ErpCovenant(business_key="COV-C", name="Cash", metric="cash_balance_cr",
                               threshold_paise=cr(20), buffered_threshold_paise=cr(22), operator="gte"))
    db_session.add(ErpCashDaily(business_key="CASH-1", as_of_date=END, account_key="PRIMARY", balance_paise=cr(40)))
    db_session.flush()
    assert compute_impact(db_session, "cash_balance_cr", WEEK) is None


def test_gst_notice_value_and_reply_deadline(db_session):
    db_session.add(ErpGstEvent(business_key="GST-1", event_date=date(2026, 7, 1), event_type="show_cause_notice",
                               amount_paise=cr(3.2), status="open"))
    db_session.add(ErpGstEvent(business_key="GST-2", event_date=date(2026, 7, 10), event_type="itc_mismatch",
                               amount_paise=cr(0.8), status="open"))
    db_session.flush()
    n = compute_impact(db_session, "gst_notice_exposure_cr", WEEK)
    assert n["amount_paise"] == cr(3.2)
    assert n["deadline"] == "2026-07-31" and n["window_days"] == 30
    assert "usual" in n["deadline_basis"]  # stated as a typical window, not as the law's date
    i = compute_impact(db_session, "itc_at_risk_cr", WEEK)
    assert i["amount_paise"] == cr(0.8) and i["deadline"] == "2026-08-20"  # next GSTR-3B
    # A red-line event rule has no metric: it is valued from its own events
    e = compute_impact(db_session, None, WEEK, event_keys=["GST-1"])
    assert e["amount_paise"] == cr(3.2)


def test_inventory_day_value_needs_a_ledger(db_session):
    assert compute_impact(db_session, "dio_days", WEEK) is None
    for m, d in ((4, 30), (5, 31), (6, 30)):
        db_session.add(ErpGlEntry(business_key=f"GL-{m}", posting_date=date(2026, m, d), period_end=date(2026, m, d),
                                  account_code="c", account_type="cogs", amount_paise=cr(60)))
    db_session.flush()
    r = compute_impact(db_session, "dio_days", WEEK)
    assert r["per_day_paise"] == int(cr(180) / 91) and r["amount_paise"] is None


def test_unsupported_metrics_get_no_impact(db_session):
    _sales(db_session)
    for m in ("opex_ratio_pct", "segment_revenue_growth_pct", "net_debt_to_ebitda_x", "no_such_metric", None):
        assert compute_impact(db_session, m, WEEK) is None


def test_digest_action_carries_the_value_and_a_cited_basis(seeded_erp, db_session, monkeypatch):
    from cfo_agent.models import DigestItem, PipelineRun
    from cfo_agent.pipeline.orchestrator import reserve_week, run_pipeline
    from cfo_agent.weighting import get_active_weighting

    monkeypatch.setattr("cfo_agent.pipeline.orchestrator.archival_insert", lambda *a, **k: None)
    monkeypatch.setattr("cfo_agent.pipeline.orchestrator.has_unconsolidated_activity", lambda s: False)
    monkeypatch.setattr("cfo_agent.pipeline.orchestrator.send_digest_email", lambda s, d: d)
    monkeypatch.setattr("cfo_agent.pipeline.step5_synthesize.archival_search", lambda *a, **k: [])
    monkeypatch.setenv("MAX_LLM_COST_USD_PER_RUN", "0")
    get_settings.cache_clear()

    week = seeded_erp["weeks"][-1]
    wv = get_active_weighting(db_session)
    run = reserve_week(db_session, week, wv.id)
    run_pipeline(run.id, week, wv.id)
    db_session.expire_all()
    assert db_session.get(PipelineRun, run.id).status == "completed"
    items = db_session.query(DigestItem).filter_by(digest_id=db_session.get(PipelineRun, run.id).digest_id).all()
    quantified = [i for i in items if (i.score_decomp_json or {}).get("impact")]
    assert quantified, "at least one surfaced item should carry an impact"
    for it in quantified:
        imp = it.score_decomp_json["impact"]
        assert imp["sentence"].split(" Aim to")[0] in it.action
        cited = {c for v in it.citations_json.values() for c in v}
        assert set(imp["citations"]) & cited or not imp["citations"]
        assert not it.verification_json.get("unverified_figures"), it.verification_json
