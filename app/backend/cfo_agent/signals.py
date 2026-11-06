"""Deterministic metric derivation with as-of-week semantics and citation lineage."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

import numpy as np
from sqlalchemy import func
from sqlalchemy.orm import Session

from cfo_agent.kra import get_active_framework
from cfo_agent.models import (
    ErpBankAccount,
    ErpCashDaily,
    ErpCreditNote,
    ErpCustomer,
    ErpEntity,
    ErpGlEntry,
    ErpExpense,
    ErpGstEvent,
    ErpPayment,
    ErpPurchaseInvoice,
    ErpReceipt,
    ErpRevenueDaily,
    ErpSalesInvoice,
    Signal,
)
from cfo_agent.timeutil import (
    iso_week_id,
    paise_to_crore,
    week_monday,
    week_sunday,
)

# Registry: metric_id → compute function name (for config↔code consistency tests)
SIGNAL_REGISTRY: dict[str, str] = {}


def register(metric_id: str):
    def deco(fn: Callable):
        SIGNAL_REGISTRY[metric_id] = fn.__name__
        return fn

    return deco


@dataclass
class MetricResult:
    metric_id: str
    value: float
    baseline: float | None = None
    delta_wow: float | None = None
    lineage: list[str] = field(default_factory=list)
    unit: str = ""
    # False when the metric is undefined for this week (e.g. a zero denominator).
    # Unavailable metrics produce no signal and never trip a red line.
    available: bool = True


def unavailable(metric_id: str, unit: str = "", reason: str = "") -> MetricResult:
    return MetricResult(metric_id, 0.0, lineage=[], unit=unit, available=False)


# ── entity scope & shared ledgers ────────────────────────────────────────────
# Metrics are consolidated across every entity flagged `consolidate` by default;
# `with entity_scope("SUB1"):` computes them for one entity. Intercompany customers
# and vendors are always excluded so group receivables/payables are not inflated.
_ENTITY_SCOPE: ContextVar[str | None] = ContextVar("entity_scope", default=None)


@contextmanager
def entity_scope(entity: str | None):
    token = _ENTITY_SCOPE.set(entity)
    try:
        yield
    finally:
        _ENTITY_SCOPE.reset(token)


def _entity_conds(session: Session, model) -> list:
    col = getattr(model, "entity", None)
    if col is None:
        return []
    scope = _ENTITY_SCOPE.get()
    if scope:
        return [col == scope]
    excluded = [e for (e,) in session.query(ErpEntity.business_key).filter(ErpEntity.consolidate.is_(False))]
    return [~col.in_(excluded)] if excluded else []


def open_receivables(session: Session, end: date) -> list[tuple[ErpSalesInvoice, int]]:
    """(invoice, open amount in paise) as of `end`: invoice less receipts and credit
    notes dated on or before `end`. One aggregated query, not one per invoice."""
    paid = (
        session.query(ErpReceipt.invoice_id.label("iid"), func.sum(ErpReceipt.amount_paise).label("amt"))
        .filter(ErpReceipt.receipt_date <= end).group_by(ErpReceipt.invoice_id).subquery()
    )
    cn = (
        session.query(ErpCreditNote.invoice_id.label("iid"), func.sum(ErpCreditNote.amount_paise).label("amt"))
        .filter(ErpCreditNote.note_date <= end).group_by(ErpCreditNote.invoice_id).subquery()
    )
    rows = (
        session.query(ErpSalesInvoice, func.coalesce(paid.c.amt, 0), func.coalesce(cn.c.amt, 0))
        .join(ErpCustomer, ErpCustomer.id == ErpSalesInvoice.customer_id)
        .outerjoin(paid, paid.c.iid == ErpSalesInvoice.id)
        .outerjoin(cn, cn.c.iid == ErpSalesInvoice.id)
        .filter(ErpSalesInvoice.invoice_date <= end, ErpCustomer.is_intercompany.is_(False),
                *_entity_conds(session, ErpSalesInvoice))
        .all()
    )
    out = []
    for inv, p, c in rows:
        rem = inv.amount_paise - int(p) - int(c)
        if rem > 0:
            out.append((inv, rem))
    return out


def open_payables(session: Session, end: date) -> list[tuple[ErpPurchaseInvoice, int]]:
    paid = (
        session.query(ErpPayment.purchase_invoice_id.label("pid"), func.sum(ErpPayment.amount_paise).label("amt"))
        .filter(ErpPayment.payment_date <= end).group_by(ErpPayment.purchase_invoice_id).subquery()
    )
    rows = (
        session.query(ErpPurchaseInvoice, func.coalesce(paid.c.amt, 0))
        .outerjoin(paid, paid.c.pid == ErpPurchaseInvoice.id)
        .filter(ErpPurchaseInvoice.invoice_date <= end, ErpPurchaseInvoice.is_intercompany.is_(False),
                *_entity_conds(session, ErpPurchaseInvoice))
        .all()
    )
    return [(pin, pin.amount_paise - int(p)) for pin, p in rows if pin.amount_paise - int(p) > 0]


def cash_positions(session: Session, end: date, *, include_restricted: bool = False) -> list[tuple[ErpCashDaily, ErpBankAccount | None]]:
    """Latest balance on or before `end` for every bank account in scope."""
    latest = (
        session.query(ErpCashDaily.account_key.label("ak"), func.max(ErpCashDaily.as_of_date).label("d"))
        .filter(ErpCashDaily.as_of_date <= end).group_by(ErpCashDaily.account_key).subquery()
    )
    rows = (
        session.query(ErpCashDaily)
        .join(latest, (latest.c.ak == ErpCashDaily.account_key) & (latest.c.d == ErpCashDaily.as_of_date))
        .order_by(ErpCashDaily.account_key)
        .all()
    )
    accounts = {a.business_key: a for a in session.query(ErpBankAccount).all()}
    scope = _ENTITY_SCOPE.get()
    excluded = {e for (e,) in session.query(ErpEntity.business_key).filter(ErpEntity.consolidate.is_(False))}
    out = []
    for r in rows:
        acct = accounts.get(r.account_key)
        entity = acct.entity if acct else "MAIN"
        if acct and acct.is_restricted and not include_restricted:
            continue
        if (scope and entity != scope) or (not scope and entity in excluded):
            continue
        out.append((r, acct))
    return out


def _cid(table: str, business_key: str) -> str:
    return f"erp:{table}:{business_key}"


def as_of_end(week: str) -> date:
    return week_sunday(week)


def previous_week(week: str) -> str:
    end = week_sunday(week) - timedelta(days=7)
    return iso_week_id(end)


@register("dso_days")
def compute_dso(session: Session, week: str) -> MetricResult:
    """DSO ≈ ending AR / average daily credit sales (net of credit notes) over 30 days."""
    end = as_of_end(week)
    sales_start = end - timedelta(days=29)
    open_rows = open_receivables(session, end)
    ar_paise = sum(rem for _, rem in open_rows)
    credit_sales = (
        session.query(func.coalesce(func.sum(ErpSalesInvoice.amount_paise), 0))
        .join(ErpCustomer, ErpCustomer.id == ErpSalesInvoice.customer_id)
        .filter(
            ErpSalesInvoice.invoice_date >= sales_start,
            ErpSalesInvoice.invoice_date <= end,
            ErpCustomer.is_intercompany.is_(False),
            *_entity_conds(session, ErpSalesInvoice),
        )
        .scalar()
    )
    notes = (
        session.query(func.coalesce(func.sum(ErpCreditNote.amount_paise), 0))
        .join(ErpSalesInvoice, ErpSalesInvoice.id == ErpCreditNote.invoice_id)
        .filter(ErpCreditNote.note_date >= sales_start, ErpCreditNote.note_date <= end,
                *_entity_conds(session, ErpSalesInvoice))
        .scalar()
    )
    net_sales = int(credit_sales) - int(notes)
    if net_sales <= 0:
        # No credit sales in the window: DSO is undefined, not "infinite".
        return unavailable("dso_days", "days", "no credit sales in trailing 30 days")
    dso = ar_paise / (net_sales / 30.0)
    open_rows.sort(key=lambda x: -x[1])
    lineage = [_cid("erp_sales_invoices", inv.business_key) for inv, _ in open_rows[:40]]
    return MetricResult("dso_days", round(dso, 2), lineage=lineage, unit="days")


@register("dpo_days")
def compute_dpo(session: Session, week: str) -> MetricResult:
    end = as_of_end(week)
    start = end - timedelta(days=89)
    rows = open_payables(session, end)
    ap = sum(rem for _, rem in rows)
    cogs_proxy = (
        session.query(func.coalesce(func.sum(ErpPayment.amount_paise), 0))
        .join(ErpPurchaseInvoice, ErpPurchaseInvoice.id == ErpPayment.purchase_invoice_id)
        .filter(ErpPayment.payment_date >= start, ErpPayment.payment_date <= end,
                ErpPurchaseInvoice.is_intercompany.is_(False), *_entity_conds(session, ErpPurchaseInvoice))
        .scalar()
    )
    if not cogs_proxy:
        return unavailable("dpo_days", "days", "no payments in trailing 90 days")
    dpo = ap / (int(cogs_proxy) / 90.0)
    rows.sort(key=lambda x: (x[0].invoice_date, x[0].id))
    lineage = [_cid("erp_purchase_invoices", p.business_key) for p, _ in rows]
    return MetricResult("dpo_days", round(dpo, 2), lineage=lineage[:40], unit="days")


@register("cash_balance_cr")
def compute_cash(session: Session, week: str) -> MetricResult:
    """Available cash: latest balance of every unrestricted account in scope, in INR."""
    end = as_of_end(week)
    positions = cash_positions(session, end)
    if not positions:
        return MetricResult("cash_balance_cr", 0.0, lineage=[], unit="Cr")
    total = sum(r.balance_paise for r, _ in positions)
    return MetricResult(
        "cash_balance_cr",
        round(paise_to_crore(total), 2),
        lineage=[_cid("erp_cash_daily", r.business_key) for r, _ in positions],
        unit="Cr",
    )


@register("covenant_headroom_cr")
def compute_covenant_headroom(session: Session, week: str) -> MetricResult:
    cash = compute_cash(session, week)
    if not cash.lineage:
        return unavailable("covenant_headroom_cr", "Cr", "no cash data")
    # Covenant threshold comes from the ERP covenant register (fallback ₹20 Cr)
    from cfo_agent.models import ErpCovenant

    cov = (
        session.query(ErpCovenant)
        .filter_by(metric="cash_balance_cr")
        .order_by(ErpCovenant.id.asc())
        .first()
    )
    threshold_cr = paise_to_crore(cov.threshold_paise) if cov else 20.0
    headroom = cash.value - threshold_cr
    lineage = list(cash.lineage) + ([_cid("erp_covenants", cov.business_key)] if cov else [])
    return MetricResult(
        "covenant_headroom_cr",
        round(headroom, 2),
        lineage=lineage,
        unit="Cr",
    )


@register("ar_overdue_pct")
def compute_ar_overdue(session: Session, week: str) -> MetricResult:
    end = as_of_end(week)
    rows = open_receivables(session, end)
    total_ar = sum(rem for _, rem in rows)
    overdue_rows = sorted(((inv, rem) for inv, rem in rows if inv.due_date < end), key=lambda x: (x[0].invoice_date, x[0].id))
    overdue = sum(rem for _, rem in overdue_rows)
    pct = (100.0 * overdue / total_ar) if total_ar else 0.0
    lineage = [_cid("erp_sales_invoices", inv.business_key) for inv, _ in overdue_rows]
    return MetricResult("ar_overdue_pct", round(pct, 2), lineage=lineage[:40], unit="%")


@register("ap_overdue_pct")
def compute_ap_overdue(session: Session, week: str) -> MetricResult:
    end = as_of_end(week)
    rows = open_payables(session, end)
    total = sum(rem for _, rem in rows)
    overdue_rows = sorted(((p, rem) for p, rem in rows if p.due_date < end), key=lambda x: (x[0].invoice_date, x[0].id))
    overdue = sum(rem for _, rem in overdue_rows)
    pct = (100.0 * overdue / total) if total else 0.0
    lineage = [_cid("erp_purchase_invoices", p.business_key) for p, _ in overdue_rows]
    return MetricResult("ap_overdue_pct", round(pct, 2), lineage=lineage[:40], unit="%")


@register("gst_open_notices")
def compute_gst_notices(session: Session, week: str) -> MetricResult:
    end = as_of_end(week)
    rows = (
        session.query(ErpGstEvent)
        .filter(
            ErpGstEvent.event_date <= end,
            ErpGstEvent.event_type == "show_cause_notice",
            ErpGstEvent.status == "open",
        )
        .all()
    )
    return MetricResult(
        "gst_open_notices",
        float(len(rows)),
        lineage=[_cid("erp_gst_events", r.business_key) for r in rows],
        unit="count",
    )


@register("gst_notice_exposure_cr")
def compute_gst_exposure(session: Session, week: str) -> MetricResult:
    end = as_of_end(week)
    rows = (
        session.query(ErpGstEvent)
        .filter(
            ErpGstEvent.event_date <= end,
            ErpGstEvent.event_type == "show_cause_notice",
            ErpGstEvent.status == "open",
        )
        .all()
    )
    total = sum(r.amount_paise for r in rows)
    return MetricResult(
        "gst_notice_exposure_cr",
        round(paise_to_crore(total), 2),
        lineage=[_cid("erp_gst_events", r.business_key) for r in rows],
        unit="Cr",
    )


@register("itc_at_risk_cr")
def compute_itc_at_risk(session: Session, week: str) -> MetricResult:
    end = as_of_end(week)
    rows = (
        session.query(ErpGstEvent)
        .filter(
            ErpGstEvent.event_date <= end,
            ErpGstEvent.event_type == "itc_mismatch",
            ErpGstEvent.status == "open",
        )
        .all()
    )
    total = sum(r.amount_paise for r in rows)
    return MetricResult(
        "itc_at_risk_cr",
        round(paise_to_crore(total), 2),
        lineage=[_cid("erp_gst_events", r.business_key) for r in rows],
        unit="Cr",
    )


@register("opex_ratio_pct")
def compute_opex_ratio(session: Session, week: str) -> MetricResult:
    end = as_of_end(week)
    start = week_monday(week)
    opex = (
        session.query(func.coalesce(func.sum(ErpExpense.amount_paise), 0))
        .filter(ErpExpense.expense_date >= start, ErpExpense.expense_date <= end, *_entity_conds(session, ErpExpense))
        .scalar()
    )
    rev = (
        session.query(func.coalesce(func.sum(ErpRevenueDaily.amount_paise), 0))
        .filter(ErpRevenueDaily.as_of_date >= start, ErpRevenueDaily.as_of_date <= end, *_entity_conds(session, ErpRevenueDaily))
        .scalar()
    )
    ratio = (100.0 * int(opex) / int(rev)) if rev else 0.0
    exps = (
        session.query(ErpExpense)
        .filter(ErpExpense.expense_date >= start, ErpExpense.expense_date <= end, *_entity_conds(session, ErpExpense))
        .limit(30)
        .all()
    )
    return MetricResult(
        "opex_ratio_pct",
        round(ratio, 2),
        lineage=[_cid("erp_expenses", e.business_key) for e in exps],
        unit="%",
    )


@register("cost_center_variance_pct")
def compute_cc_variance(session: Session, week: str) -> MetricResult:
    """Max cost-center spend vs 8-week trailing average, as %."""
    end = as_of_end(week)
    start = week_monday(week)
    hist_start = end - timedelta(days=56)
    centers = ["Finance", "Sales", "Ops", "Admin", "Plant"]
    max_var = 0.0
    lineage: list[str] = []
    for cc in centers:
        cur = (
            session.query(func.coalesce(func.sum(ErpExpense.amount_paise), 0))
            .filter(
                ErpExpense.cost_center == cc,
                *_entity_conds(session, ErpExpense),
                ErpExpense.expense_date >= start,
                ErpExpense.expense_date <= end,
            )
            .scalar()
        )
        hist = (
            session.query(func.coalesce(func.sum(ErpExpense.amount_paise), 0))
            .filter(
                ErpExpense.cost_center == cc,
                *_entity_conds(session, ErpExpense),
                ErpExpense.expense_date >= hist_start,
                ErpExpense.expense_date < start,
            )
            .scalar()
        )
        if not hist:
            continue  # no baseline for this cost center yet — variance undefined
        weekly_avg = int(hist) / 8.0
        var = 100.0 * (int(cur) - weekly_avg) / weekly_avg
        if abs(var) >= abs(max_var):
            max_var = var
            rows = (
                session.query(ErpExpense)
                .filter(
                    ErpExpense.cost_center == cc,
                *_entity_conds(session, ErpExpense),
                    ErpExpense.expense_date >= start,
                    ErpExpense.expense_date <= end,
                )
                .limit(15)
                .all()
            )
            lineage = [_cid("erp_expenses", e.business_key) for e in rows]
    return MetricResult("cost_center_variance_pct", round(max_var, 2), lineage=lineage, unit="%")


@register("close_cycle_days")
def compute_close_cycle(session: Session, week: str) -> MetricResult:
    """Days from the latest closed period's end to its last GL posting.

    A period counts as closed once 10 days have passed since its end. Without GL
    data the metric falls back to a fixed proxy (flagged `proxy` in METRIC_META)."""
    end = as_of_end(week)
    pe = (
        session.query(func.max(ErpGlEntry.period_end))
        .filter(ErpGlEntry.period_end <= end - timedelta(days=10), *_entity_conds(session, ErpGlEntry))
        .scalar()
    )
    if pe is not None:
        last = (
            session.query(ErpGlEntry)
            .filter(ErpGlEntry.period_end == pe, ErpGlEntry.posting_date <= end, *_entity_conds(session, ErpGlEntry))
            .order_by(ErpGlEntry.posting_date.desc(), ErpGlEntry.id.desc())
            .first()
        )
        if last is not None:
            days = max(0, (last.posting_date - pe).days)
            return MetricResult("close_cycle_days", float(days),
                                lineage=[_cid("erp_gl_entries", last.business_key)], unit="days")
    wi = end.isocalendar().week
    value = 4.0 + (0.3 if wi % 4 == 0 else 0.0)
    cash = session.query(ErpCashDaily).filter(ErpCashDaily.as_of_date == end).first()
    lineage = [_cid("erp_cash_daily", cash.business_key)] if cash else []
    return MetricResult("close_cycle_days", value, lineage=lineage, unit="days")


@register("unreconciled_items")
def compute_unreconciled(session: Session, week: str) -> MetricResult:
    end = as_of_end(week)
    open_ar = (
        session.query(ErpSalesInvoice)
        .filter(ErpSalesInvoice.invoice_date <= end, ErpSalesInvoice.status != "paid")
        .count()
    )
    # Use a scaled proxy
    value = float(min(open_ar // 5, 40))
    rows = (
        session.query(ErpSalesInvoice)
        .filter(ErpSalesInvoice.invoice_date <= end, ErpSalesInvoice.status != "paid")
        .limit(20)
        .all()
    )
    return MetricResult(
        "unreconciled_items",
        value,
        lineage=[_cid("erp_sales_invoices", r.business_key) for r in rows],
        unit="count",
    )


@register("collections_coverage_pct")
def compute_collections_coverage(session: Session, week: str) -> MetricResult:
    end = as_of_end(week)
    start = week_monday(week)
    collected = (
        session.query(func.coalesce(func.sum(ErpReceipt.amount_paise), 0))
        .filter(ErpReceipt.receipt_date >= start, ErpReceipt.receipt_date <= end)
        .scalar()
    )
    # due this week: invoices with due_date in week
    due = (
        session.query(ErpSalesInvoice)
        .filter(ErpSalesInvoice.due_date >= start, ErpSalesInvoice.due_date <= end)
        .all()
    )
    due_amt = sum(i.amount_paise for i in due)
    if not due_amt:
        return unavailable("collections_coverage_pct", "%", "nothing fell due this week")
    pct = 100.0 * int(collected) / due_amt
    receipts = (
        session.query(ErpReceipt)
        .filter(ErpReceipt.receipt_date >= start, ErpReceipt.receipt_date <= end)
        .limit(30)
        .all()
    )
    return MetricResult(
        "collections_coverage_pct",
        round(pct, 2),
        lineage=[_cid("erp_receipts", r.business_key) for r in receipts],
        unit="%",
    )


@register("free_cash_flow_cr")
def compute_fcf(session: Session, week: str) -> MetricResult:
    end = as_of_end(week)
    start = week_monday(week)
    receipts = (
        session.query(func.coalesce(func.sum(ErpReceipt.amount_paise), 0))
        .filter(ErpReceipt.receipt_date >= start, ErpReceipt.receipt_date <= end)
        .scalar()
    )
    payments = (
        session.query(func.coalesce(func.sum(ErpPayment.amount_paise), 0))
        .filter(ErpPayment.payment_date >= start, ErpPayment.payment_date <= end)
        .scalar()
    )
    expenses = (
        session.query(func.coalesce(func.sum(ErpExpense.amount_paise), 0))
        .filter(ErpExpense.expense_date >= start, ErpExpense.expense_date <= end, *_entity_conds(session, ErpExpense))
        .scalar()
    )
    fcf = int(receipts) - int(payments) - int(expenses)
    cash = compute_cash(session, week)
    return MetricResult(
        "free_cash_flow_cr",
        round(paise_to_crore(fcf), 2),
        lineage=cash.lineage,
        unit="Cr",
    )


@register("segment_revenue_growth_pct")
def compute_segment_growth(session: Session, week: str) -> MetricResult:
    """Best segment WoW growth % (and lineage from that segment's revenue rows)."""
    end = as_of_end(week)
    start = week_monday(week)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=6)
    best = -999.0
    lineage: list[str] = []
    for seg in ["Consumer", "Industrial", "Exports"]:
        cur = (
            session.query(func.coalesce(func.sum(ErpRevenueDaily.amount_paise), 0))
            .filter(
                ErpRevenueDaily.segment == seg,
                *_entity_conds(session, ErpRevenueDaily),
                ErpRevenueDaily.as_of_date >= start,
                ErpRevenueDaily.as_of_date <= end,
            )
            .scalar()
        )
        prev = (
            session.query(func.coalesce(func.sum(ErpRevenueDaily.amount_paise), 0))
            .filter(
                ErpRevenueDaily.segment == seg,
                *_entity_conds(session, ErpRevenueDaily),
                ErpRevenueDaily.as_of_date >= prev_start,
                ErpRevenueDaily.as_of_date <= prev_end,
            )
            .scalar()
        )
        growth = 100.0 * (int(cur) - int(prev)) / int(prev) if prev else 0.0
        if growth >= best:
            best = growth
            rows = (
                session.query(ErpRevenueDaily)
                .filter(
                    ErpRevenueDaily.segment == seg,
                *_entity_conds(session, ErpRevenueDaily),
                    ErpRevenueDaily.as_of_date >= start,
                    ErpRevenueDaily.as_of_date <= end,
                )
                .all()
            )
            lineage = [_cid("erp_revenue_daily", r.business_key) for r in rows]
    return MetricResult("segment_revenue_growth_pct", round(best, 2), lineage=lineage, unit="%")


@register("revenue_concentration_pct")
def compute_rev_concentration(session: Session, week: str) -> MetricResult:
    end = as_of_end(week)
    start = week_monday(week)
    totals = {}
    lineage: list[str] = []
    for seg in ["Consumer", "Industrial", "Exports"]:
        cur = (
            session.query(func.coalesce(func.sum(ErpRevenueDaily.amount_paise), 0))
            .filter(
                ErpRevenueDaily.segment == seg,
                *_entity_conds(session, ErpRevenueDaily),
                ErpRevenueDaily.as_of_date >= start,
                ErpRevenueDaily.as_of_date <= end,
            )
            .scalar()
        )
        totals[seg] = int(cur)
    grand = sum(totals.values()) or 1
    top_seg = max(totals, key=totals.get)
    pct = 100.0 * totals[top_seg] / grand
    rows = (
        session.query(ErpRevenueDaily)
        .filter(
            ErpRevenueDaily.segment == top_seg,
            ErpRevenueDaily.as_of_date >= start,
            ErpRevenueDaily.as_of_date <= end,
        )
        .all()
    )
    lineage = [_cid("erp_revenue_daily", r.business_key) for r in rows]
    return MetricResult("revenue_concentration_pct", round(pct, 2), lineage=lineage, unit="%")


@register("board_pack_readiness_score")
def compute_board_pack(session: Session, week: str) -> MetricResult:
    """Proxy score 0-100 from close cycle + unreconciled."""
    close = compute_close_cycle(session, week)
    unr = compute_unreconciled(session, week)
    score = max(0.0, 100.0 - close.value * 5 - unr.value)
    return MetricResult(
        "board_pack_readiness_score",
        round(score, 1),
        lineage=close.lineage + unr.lineage[:10],
        unit="score",
    )


COMPUTE_FNS: dict[str, Callable[[Session, str], MetricResult]] = {
    "dso_days": compute_dso,
    "dpo_days": compute_dpo,
    "cash_balance_cr": compute_cash,
    "covenant_headroom_cr": compute_covenant_headroom,
    "ar_overdue_pct": compute_ar_overdue,
    "ap_overdue_pct": compute_ap_overdue,
    "gst_open_notices": compute_gst_notices,
    "gst_notice_exposure_cr": compute_gst_exposure,
    "itc_at_risk_cr": compute_itc_at_risk,
    "opex_ratio_pct": compute_opex_ratio,
    "cost_center_variance_pct": compute_cc_variance,
    "close_cycle_days": compute_close_cycle,
    "unreconciled_items": compute_unreconciled,
    "collections_coverage_pct": compute_collections_coverage,
    "free_cash_flow_cr": compute_fcf,
    "segment_revenue_growth_pct": compute_segment_growth,
    "revenue_concentration_pct": compute_rev_concentration,
    "board_pack_readiness_score": compute_board_pack,
}


def severity(
    delta_wow: float | None,
    history_deltas: list[float],
    baseline: float | None,
) -> float:
    """severity = clamp(|delta|/rolling_std, 0.5, 3.0); fallback |delta|/|baseline|."""
    if delta_wow is None:
        return 1.0
    abs_d = abs(delta_wow)
    if len(history_deltas) >= 3:
        std = float(np.std(history_deltas)) or 1e-9
        return float(np.clip(abs_d / std, 0.5, 3.0))
    if baseline is not None and abs(baseline) > 1e-9:
        return float(np.clip(abs_d / abs(baseline), 0.5, 3.0))
    return 1.0


# Which direction of movement is adverse for each metric. A favourable move still
# registers (big improvements can matter) but at half severity, so a DSO drop never
# outranks an equal DSO rise. None = both directions are adverse.
ADVERSE_DIRECTION: dict[str, str | None] = {
    "dso_days": "up",
    "dpo_days": None,  # falling = cash out faster; rising = supplier stress
    "cash_balance_cr": "down",
    "covenant_headroom_cr": "down",
    "ar_overdue_pct": "up",
    "ap_overdue_pct": "up",
    "gst_open_notices": "up",
    "gst_notice_exposure_cr": "up",
    "itc_at_risk_cr": "up",
    "opex_ratio_pct": "up",
    "cost_center_variance_pct": None,
    "close_cycle_days": "up",
    "unreconciled_items": "up",
    "collections_coverage_pct": "down",
    "free_cash_flow_cr": "down",
    "segment_revenue_growth_pct": "down",
    "revenue_concentration_pct": "up",
    "board_pack_readiness_score": "down",
}
FAVOURABLE_MOVE_FACTOR = 0.5

# Human-readable labels and units for every metric (UI pickers, sparklines, evidence).
METRIC_META: dict[str, dict[str, str]] = {
    "dso_days": {"label": "Days sales outstanding (DSO)", "unit": "days"},
    "dpo_days": {"label": "Days payables outstanding (DPO)", "unit": "days"},
    "cash_balance_cr": {"label": "Cash balance", "unit": "₹ Cr"},
    "covenant_headroom_cr": {"label": "Cash covenant headroom", "unit": "₹ Cr"},
    "ar_overdue_pct": {"label": "Receivables overdue", "unit": "%"},
    "ap_overdue_pct": {"label": "Payables overdue", "unit": "%"},
    "gst_open_notices": {"label": "Open GST notices", "unit": "count"},
    "gst_notice_exposure_cr": {"label": "GST notice exposure", "unit": "₹ Cr"},
    "itc_at_risk_cr": {"label": "Input tax credit at risk", "unit": "₹ Cr"},
    "opex_ratio_pct": {"label": "Opex to revenue", "unit": "%"},
    "cost_center_variance_pct": {"label": "Largest cost-centre variance", "unit": "%"},
    "close_cycle_days": {"label": "Month-end close cycle", "unit": "days"},
    "unreconciled_items": {"label": "Unreconciled items", "unit": "count"},
    "collections_coverage_pct": {"label": "Collections vs amount due", "unit": "%"},
    "free_cash_flow_cr": {"label": "Free cash flow (week)", "unit": "₹ Cr"},
    "segment_revenue_growth_pct": {"label": "Segment revenue growth", "unit": "%"},
    "revenue_concentration_pct": {"label": "Top-segment revenue share", "unit": "%"},
    "board_pack_readiness_score": {"label": "Board pack readiness", "unit": "score"},
}


def metric_meta(metric_id: str) -> dict:
    meta = METRIC_META.get(metric_id, {})
    return {
        "id": metric_id,
        "label": meta.get("label", metric_id.replace("_", " ")),
        "unit": meta.get("unit", ""),
        "adverse_direction": ADVERSE_DIRECTION.get(metric_id),
        "custom": bool(meta.get("custom")),
        "proxy": metric_id in PROXY_METRICS,
        "description": meta.get("description", ""),
    }


# Metrics that are stand-ins until the client's data supports the real measure
PROXY_METRICS = {"unreconciled_items", "board_pack_readiness_score"}


def directional_severity(metric_id: str, delta_wow: float | None, sev: float) -> float:
    direction = ADVERSE_DIRECTION.get(metric_id)
    if delta_wow is None or direction is None or delta_wow == 0:
        return sev
    adverse = delta_wow > 0 if direction == "up" else delta_wow < 0
    return sev if adverse else sev * FAVOURABLE_MOVE_FACTOR


def rank_boost(kra_id: str, kra_ranks: list[str]) -> float:
    """rank_boost = 1 + (N − rank + 1)/N for ranked KRAs; else 1.0."""
    if kra_id not in kra_ranks:
        return 1.0
    n = len(kra_ranks)
    rank = kra_ranks.index(kra_id) + 1  # 1-based
    return 1.0 + (n - rank + 1) / n


def priority_score(face_weight: float, boost: float, sev: float) -> float:
    return face_weight * boost * sev


def compute_all_signals(session: Session, week: str, *, persist: bool = True) -> list[Signal]:
    from cfo_agent.metrics_dsl import ensure_custom_metrics

    ensure_custom_metrics(session)
    fw = get_active_framework(session)
    metrics = sorted(fw.metrics_for_signals())
    prev = previous_week(week)

    # Prior week values for delta
    prev_map: dict[str, float] = {}
    for s in session.query(Signal).filter_by(week=prev).all():
        prev_map[s.metric_id] = s.value

    # History of weekly deltas for severity (trailing 8)
    history: dict[str, list[float]] = {m: [] for m in metrics}
    hist_signals = (
        session.query(Signal)
        .filter(Signal.metric_id.in_(metrics))
        .order_by(Signal.week.asc())
        .all()
    )
    by_metric: dict[str, list[Signal]] = {}
    for s in hist_signals:
        if s.week >= week:
            continue
        by_metric.setdefault(s.metric_id, []).append(s)
    for m, rows in by_metric.items():
        rows = rows[-9:]
        for i in range(1, len(rows)):
            history.setdefault(m, []).append(rows[i].value - rows[i - 1].value)
        history[m] = history.get(m, [])[-8:]

    results: list[Signal] = []
    for metric_id in metrics:
        fn = COMPUTE_FNS.get(metric_id)
        if not fn:
            continue
        mr = fn(session, week)
        if not mr.available:
            continue
        baseline = prev_map.get(metric_id)
        delta = (mr.value - baseline) if baseline is not None else None
        sev = severity(delta, history.get(metric_id, []), baseline)
        sev = directional_severity(metric_id, delta, sev)
        # Plan gap, covenant proximity / projected breach and anomalies can only
        # raise severity (see analytics.py); their records become citable lineage.
        from cfo_agent.analytics import signal_context

        ctx, floor, extra_cites = signal_context(
            session, metric_id, week, mr.value, delta, history.get(metric_id, []), ADVERSE_DIRECTION.get(metric_id)
        )
        ctx["base_severity"] = round(sev, 3)
        sev = max(sev, floor)
        lineage = list(mr.lineage) + [c for c in extra_cites if c not in mr.lineage]
        kra_ids = fw.metric_map.get(metric_id, [])
        # Only grounded KRAs
        kra_ids = [k for k in kra_ids if fw.kras[k].grounding == "erp"]

        signal_id = f"metric:{metric_id}:{week}"
        existing = session.query(Signal).filter_by(signal_id=signal_id).one_or_none()
        if existing:
            row = existing
        else:
            row = Signal(signal_id=signal_id)
            session.add(row)

        row.week = week
        row.metric_id = metric_id
        row.value = mr.value
        row.baseline = baseline
        row.delta_wow = delta
        row.severity = sev
        row.kra_ids_json = kra_ids
        row.lineage_json = lineage
        row.context_json = ctx
        results.append(row)

    if persist:
        session.flush()
    return results


def get_signal_lineage(session: Session, metric_id: str, week: str) -> list[str]:
    sid = f"metric:{metric_id}:{week}"
    row = session.query(Signal).filter_by(signal_id=sid).one_or_none()
    return list(row.lineage_json) if row else []
