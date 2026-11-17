"""Inventory, cash cycle, leverage and foreign-currency metrics: worked examples, and
nothing is shown where a feed is missing."""

from __future__ import annotations

from datetime import date, timedelta

from cfo_agent.connectors.csv_drop import SPEC_BY_NAME, template_header
from cfo_agent.models import (
    ErpCashDaily,
    ErpCustomer,
    ErpDebtBalance,
    ErpFxRate,
    ErpGlEntry,
    ErpInventoryBalance,
    ErpPurchaseInvoice,
    ErpSalesInvoice,
)
from cfo_agent import signals
from cfo_agent.signals import (
    MetricResult,
    compute_ccc,
    compute_dio,
    compute_dscr,
    compute_fx_move,
    compute_interest_cover,
    compute_inventory,
    compute_leverage,
    compute_net_debt,
    compute_net_fx,
)

WEEK = "2026-W30"  # ends Sunday 26 Jul 2026; closed ledger months are Apr, May, Jun
END = date(2026, 7, 26)


def cr(x: float) -> int:
    return int(x * 10_000_000 * 100)


def _ledger(s, finance=2, months=((4, 30), (5, 31), (6, 30))):
    """Per month: revenue 100 Cr (a credit), cost of goods 60, opex 20, finance cost 2 (₹ Cr)."""
    for m, d in months:
        pe = date(2026, m, d)
        for kind, amt in (("revenue", -100), ("cogs", 60), ("opex", 20), ("finance_cost", finance)):
            s.add(ErpGlEntry(business_key=f"GL-{m}-{kind}", posting_date=pe + timedelta(days=3), period_end=pe,
                             account_code=kind[:3], account_type=kind, amount_paise=cr(amt)))
    s.flush()


def _inventory(s, day=date(2026, 7, 20)):
    s.add_all([
        ErpInventoryBalance(business_key="INV-RM", as_of_date=day, category="raw", value_paise=cr(30)),
        ErpInventoryBalance(business_key="INV-FG", as_of_date=day, category="finished", value_paise=cr(21)),
    ])
    s.flush()


def _debt(s, day=date(2026, 7, 20)):
    s.add_all([
        ErpDebtBalance(business_key="D-TL", as_of_date=day, facility="TL", lender="Bank A",
                       outstanding_paise=cr(120), principal_due_90d_paise=cr(10)),
        ErpDebtBalance(business_key="D-WC", as_of_date=day, facility="WC", lender="Bank B", outstanding_paise=cr(30)),
    ])
    s.add(ErpCashDaily(business_key="CASH-1", as_of_date=END, account_key="PRIMARY", balance_paise=cr(70)))
    s.flush()


def test_days_of_inventory(db_session):
    _ledger(db_session)
    _inventory(db_session)
    assert compute_inventory(db_session, WEEK).value == 51.0
    # 51 Cr ÷ (180 Cr cost of goods over the 91 days of Apr–Jun) = 25.8 days
    r = compute_dio(db_session, WEEK)
    assert r.available and r.value == 25.8
    assert "erp_inventory:INV-RM" in " ".join(r.lineage) or any("erp_inventory" in c for c in r.lineage)


def test_nothing_without_the_feed(db_session):
    # No ledger, no inventory, no debt, no foreign invoices: every new metric is blank.
    for fn in (compute_inventory, compute_dio, compute_ccc, compute_net_debt, compute_leverage,
               compute_interest_cover, compute_dscr, compute_net_fx, compute_fx_move):
        assert not fn(db_session, WEEK).available, fn.__name__


def test_dio_needs_three_closed_months_and_a_recent_balance(db_session):
    _inventory(db_session)
    _ledger(db_session, months=((5, 31), (6, 30)))
    assert not compute_dio(db_session, WEEK).available  # two months of ledger
    _ledger_extra = ErpGlEntry(business_key="GL-4-cogs", posting_date=date(2026, 5, 3), period_end=date(2026, 4, 30),
                               account_code="cog", account_type="cogs", amount_paise=cr(60))
    db_session.add(_ledger_extra)
    db_session.flush()
    assert compute_dio(db_session, WEEK).available
    # An inventory count older than 45 days no longer describes today.
    db_session.query(ErpInventoryBalance).update({"as_of_date": date(2026, 5, 1)})
    db_session.flush()
    assert not compute_dio(db_session, WEEK).available


def test_cash_conversion_cycle(db_session, monkeypatch):
    _ledger(db_session)
    _inventory(db_session)
    monkeypatch.setattr(signals, "compute_dso", lambda s, w: MetricResult("dso_days", 40.0, unit="days"))
    monkeypatch.setattr(signals, "compute_dpo", lambda s, w: MetricResult("dpo_days", 30.0, unit="days"))
    r = compute_ccc(db_session, WEEK)
    assert r.available and r.value == 35.8  # 40 + 25.8 − 30
    monkeypatch.setattr(signals, "compute_dpo", lambda s, w: signals.unavailable("dpo_days", "days"))
    assert not compute_ccc(db_session, WEEK).available


def test_leverage_and_cover(db_session):
    _ledger(db_session)
    _debt(db_session)
    # Debt 150 Cr less cash 70 Cr
    assert compute_net_debt(db_session, WEEK).value == 80.0
    # Quarter EBITDA = 300 − 180 − 60 = 60 Cr, so 240 Cr a year: 80 ÷ 240
    assert compute_leverage(db_session, WEEK).value == 0.33
    # 60 ÷ finance cost of 6
    assert compute_interest_cover(db_session, WEEK).value == 10.0
    # 60 ÷ (6 finance cost + 10 principal due in 90 days)
    assert compute_dscr(db_session, WEEK).value == 3.75


def test_leverage_is_undefined_when_ebitda_is_not_positive(db_session):
    _ledger(db_session)
    _debt(db_session)
    db_session.query(ErpGlEntry).filter_by(account_type="cogs").update({"amount_paise": cr(95)})
    db_session.flush()
    assert not compute_leverage(db_session, WEEK).available


def test_forex_exposure_and_rate_move(db_session):
    c = ErpCustomer(business_key="C1", name="Acme US", segment="Exports")
    db_session.add(c)
    db_session.flush()
    inv = dict(customer_id=c.id, due_date=END + timedelta(days=30), segment="Exports")
    db_session.add_all([
        ErpSalesInvoice(business_key="SI-USD", invoice_date=END - timedelta(days=20), amount_paise=cr(8),
                        currency="USD", fx_rate=83.0, **inv),
        ErpSalesInvoice(business_key="SI-INR", invoice_date=END - timedelta(days=20), amount_paise=cr(50), **inv),
        ErpPurchaseInvoice(business_key="PI-EUR", vendor_name="Euro Metals", invoice_date=END - timedelta(days=15),
                           due_date=END + timedelta(days=30), amount_paise=cr(3), currency="EUR", fx_rate=90.0),
    ])
    db_session.add_all([
        ErpFxRate(business_key="FX-1", rate_date=END - timedelta(days=28), currency="USD", rate_to_inr=80.0),
        ErpFxRate(business_key="FX-2", rate_date=END - timedelta(days=1), currency="USD", rate_to_inr=84.0),
        ErpFxRate(business_key="FX-3", rate_date=END - timedelta(days=28), currency="EUR", rate_to_inr=90.0),
        ErpFxRate(business_key="FX-4", rate_date=END - timedelta(days=1), currency="EUR", rate_to_inr=91.0),
    ])
    db_session.flush()
    # Only foreign-currency invoices count: 8 Cr receivable less 3 Cr payable
    assert compute_net_fx(db_session, WEEK).value == 5.0
    assert signals.compute_fx_receivables(db_session, WEEK).value == 8.0
    assert signals.compute_fx_payables(db_session, WEEK).value == 3.0
    # USD went 80 → 84 (+5%), EUR 90 → 91 (+1.1%): the larger move wins
    assert compute_fx_move(db_session, WEEK).value == 5.0


def test_csv_templates_exist_for_the_new_feeds():
    assert template_header("inventory") == ["business_key", "as_of_date", "category", "value_inr", "entity"]
    assert "principal_due_90d_inr" in template_header("debt")
    assert {"inventory", "debt"} <= set(SPEC_BY_NAME)


def test_covenant_on_a_ratio_metric(db_session):
    """A leverage covenant (net debt to EBITDA ≤ 3.5x) works with no extra code."""
    from cfo_agent.analytics import covenant_context
    from cfo_agent.models import ErpCovenant

    db_session.add(ErpCovenant(business_key="COV-LEV", name="Leverage", metric="net_debt_to_ebitda_x",
                               threshold_paise=350, buffered_threshold_paise=300, operator="lte"))
    db_session.flush()
    ctx = covenant_context(db_session, "net_debt_to_ebitda_x", 3.2, [])
    assert ctx["threshold"] == 3.5 and ctx["in_buffer"] is True and ctx["breached"] is False
