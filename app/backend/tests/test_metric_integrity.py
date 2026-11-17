"""Metrics read the client's own dimensions and never invent a value."""

from __future__ import annotations

from datetime import date, timedelta

from cfo_agent.models import (
    ErpCustomer,
    ErpExpense,
    ErpPayment,
    ErpPurchaseInvoice,
    ErpReceipt,
    ErpRevenueDaily,
    ErpSalesInvoice,
)
from cfo_agent.redlines import evaluate_rules
from cfo_agent.signals import (
    compute_all_signals,
    compute_board_pack,
    compute_cash,
    compute_cc_variance,
    compute_close_cycle,
    compute_collections_coverage,
    compute_covenant_headroom,
    compute_dpo,
    compute_fcf,
    compute_rev_concentration,
    compute_segment_growth,
    compute_unreconciled,
)
from cfo_agent.timeutil import iso_week_id, week_monday

WEEK = "2026-W30"
MON = week_monday(WEEK)


def _rev(s, day: date, seg: str, rupees: int):
    s.add(ErpRevenueDaily(business_key=f"R-{seg}-{day}", as_of_date=day, segment=seg, amount_paise=rupees * 100))


def test_segments_come_from_the_clients_data(db_session):
    # None of these is one of the demo's segment names.
    for seg, prev, cur in (("Pharma", 100, 150), ("Agri", 200, 210), ("Retail", 0, 500)):
        if prev:
            _rev(db_session, MON - timedelta(days=3), seg, prev)
        _rev(db_session, MON + timedelta(days=2), seg, cur)
    db_session.flush()

    growth = compute_segment_growth(db_session, WEEK)
    assert growth.available and growth.value == 50.0  # Pharma; Retail has no prior week
    assert all("Pharma" in c for c in growth.lineage)

    conc = compute_rev_concentration(db_session, WEEK)
    assert conc.available and conc.value == round(100 * 500 / 860, 2)  # Retail


def test_cost_centres_come_from_the_clients_data(db_session):
    for i in range(8):
        d = MON - timedelta(days=7 * (i + 1) - 1)
        db_session.add(ErpExpense(business_key=f"E-h{i}", expense_date=d, cost_center="R&D Pune", category="x", amount_paise=100_00))
    db_session.add(ErpExpense(business_key="E-cur", expense_date=MON + timedelta(days=1), cost_center="R&D Pune", category="x", amount_paise=180_00))
    db_session.flush()
    r = compute_cc_variance(db_session, WEEK)
    assert r.available and r.value == 80.0
    assert r.lineage == ["erp:erp_expenses:E-cur"]


def test_no_data_means_unavailable_never_a_made_up_number(db_session):
    for fn in (compute_cash, compute_covenant_headroom, compute_close_cycle, compute_unreconciled,
               compute_board_pack, compute_segment_growth, compute_rev_concentration, compute_cc_variance,
               compute_fcf, compute_dpo, compute_collections_coverage):
        assert not fn(db_session, WEEK).available, fn.__name__
    assert compute_all_signals(db_session, WEEK, persist=False) == [] or all(
        s.metric_id not in {"cash_balance_cr", "close_cycle_days", "unreconciled_items", "board_pack_readiness_score"}
        for s in compute_all_signals(db_session, WEEK, persist=False)
    )


def test_missing_cash_data_does_not_trip_a_cash_red_line(db_session):
    rules = [{"id": "RL-1", "type": "level", "metric": "cash_balance_cr", "operator": "lt", "threshold": 22}]
    assert evaluate_rules(db_session, rules, as_of=MON + timedelta(days=6)) == []


def test_cash_flow_and_collections_exclude_intercompany(db_session):
    s = db_session
    ext = ErpCustomer(business_key="C-EXT", name="External", segment="A")
    ic = ErpCustomer(business_key="C-IC", name="Group co", segment="A", is_intercompany=True)
    s.add_all([ext, ic])
    s.flush()
    day = MON + timedelta(days=1)
    inv_ext = ErpSalesInvoice(business_key="SI-EXT", customer_id=ext.id, invoice_date=MON - timedelta(days=30), due_date=day, amount_paise=1_000_00, segment="A")
    inv_ic = ErpSalesInvoice(business_key="SI-IC", customer_id=ic.id, invoice_date=MON - timedelta(days=30), due_date=day, amount_paise=9_000_00, segment="A")
    s.add_all([inv_ext, inv_ic])
    s.flush()
    s.add_all([
        ErpReceipt(business_key="RC-EXT", invoice_id=inv_ext.id, receipt_date=day, amount_paise=500_00),
        ErpReceipt(business_key="RC-IC", invoice_id=inv_ic.id, receipt_date=day, amount_paise=9_000_00),
    ])
    pin_ext = ErpPurchaseInvoice(business_key="PI-EXT", vendor_name="V", invoice_date=MON - timedelta(days=20), due_date=day, amount_paise=900_00)
    pin_ic = ErpPurchaseInvoice(business_key="PI-IC", vendor_name="Group", invoice_date=MON - timedelta(days=20), due_date=day, amount_paise=7_000_00, is_intercompany=True)
    s.add_all([pin_ext, pin_ic])
    s.flush()
    s.add_all([
        ErpPayment(business_key="PY-EXT", purchase_invoice_id=pin_ext.id, payment_date=day, amount_paise=200_00),
        ErpPayment(business_key="PY-IC", purchase_invoice_id=pin_ic.id, payment_date=day, amount_paise=7_000_00),
    ])
    s.flush()

    cov = compute_collections_coverage(s, WEEK)
    assert cov.value == 50.0 and cov.lineage == ["erp:erp_receipts:RC-EXT"]

    fcf = compute_fcf(s, WEEK)  # (500 − 200) rupees, in ₹ Cr
    assert fcf.available and abs(fcf.value - round(300 / 1e7, 2)) < 1e-9
    assert set(fcf.lineage) == {"erp:erp_receipts:RC-EXT", "erp:erp_payments:PY-EXT"}

    # DPO: open payables ÷ daily credit purchases over 90 days (external only)
    dpo = compute_dpo(s, WEEK)
    assert dpo.value == round((900 - 200) / (900 / 90.0), 2)
    assert iso_week_id(day) == WEEK


def test_pilot_scorecard_counts_the_success_test(db_session):
    from datetime import datetime

    from fastapi.testclient import TestClient

    from cfo_agent.main import app
    from cfo_agent.models import AuditEvent, Digest, DigestItem, Feedback, WeightingVersion

    s = db_session
    wv = s.query(WeightingVersion).first()
    d1 = Digest(week="2026-W29", weighting_version_id=wv.id, headline="a", created_at=datetime(2026, 7, 20, 8))
    d2 = Digest(week="2026-W30", weighting_version_id=wv.id, headline="b", created_at=datetime(2026, 7, 27, 8))
    s.add_all([d1, d2])
    s.flush()
    items = [DigestItem(digest_id=d1.id, rank=r, title=f"i{r}") for r in (1, 2, 3)] + [
        DigestItem(digest_id=d2.id, rank=1, title="j1", is_red_line=True)
    ]
    s.add_all(items)
    s.flush()
    s.add_all([
        Feedback(digest_item_id=items[0].id, verdict="acted_on", already_knew=False),
        Feedback(digest_item_id=items[1].id, verdict="acted_on", already_knew=True),
        Feedback(digest_item_id=items[2].id, verdict="set_aside"),
        AuditEvent(at=datetime(2026, 7, 21, 9), user_email="c@x", role="admin", action="view", method="GET", path="/api/briefing", status=200),
    ])
    s.commit()

    with TestClient(app) as client:
        body = client.get("/api/feedback/scorecard").json()
        types = client.get("/api/redlines/event-types").json()
    w29, w30 = body["weeks"]
    assert (w29["surfaced"], w29["reviewed"], w29["novel_acted_on"], w29["opens"]) == (3, 3, 1, 1)
    assert (w30["surfaced"], w30["red_lines"], w30["reviewed"], w30["opens"]) == (1, 1, 0, 0)
    t = body["totals"]
    assert t["success_test_met"] and t["review_rate_pct"] == 75.0 and t["weeks_opened"] == 1
    assert t["useful_rate_pct"] == round(100 * 2 / 3, 1)
    assert {x["value"] for x in types} >= {"show_cause_notice", "itc_mismatch"}
