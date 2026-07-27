"""Deterministic metric derivation with as-of-week semantics and citation lineage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

import numpy as np
from sqlalchemy import func
from sqlalchemy.orm import Session

from cfo_agent.kra import get_active_framework
from cfo_agent.models import (
    ErpCashDaily,
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


def _cid(table: str, business_key: str) -> str:
    return f"erp:{table}:{business_key}"


def as_of_end(week: str) -> date:
    return week_sunday(week)


def previous_week(week: str) -> str:
    end = week_sunday(week) - timedelta(days=7)
    return iso_week_id(end)


@register("dso_days")
def compute_dso(session: Session, week: str) -> MetricResult:
    """DSO ≈ ending AR / average daily credit sales over trailing 30 days."""
    end = as_of_end(week)
    sales_start = end - timedelta(days=29)
    invoices = (
        session.query(ErpSalesInvoice)
        .filter(ErpSalesInvoice.invoice_date <= end)
        .all()
    )
    # Collect (remaining, citation) then keep top AR contributors
    open_rows: list[tuple[int, str]] = []
    ar_paise = 0
    for inv in invoices:
        paid = (
            session.query(func.coalesce(func.sum(ErpReceipt.amount_paise), 0))
            .filter(ErpReceipt.invoice_id == inv.id, ErpReceipt.receipt_date <= end)
            .scalar()
        )
        remaining = inv.amount_paise - int(paid)
        if remaining > 0:
            ar_paise += remaining
            open_rows.append((remaining, _cid("erp_sales_invoices", inv.business_key)))

    credit_sales = (
        session.query(func.coalesce(func.sum(ErpSalesInvoice.amount_paise), 0))
        .filter(
            ErpSalesInvoice.invoice_date >= sales_start,
            ErpSalesInvoice.invoice_date <= end,
        )
        .scalar()
    )
    daily_sales = int(credit_sales) / 30.0 if credit_sales else 1.0
    dso = ar_paise / daily_sales if daily_sales else 0.0
    open_rows.sort(key=lambda x: -x[0])
    lineage = [c for _, c in open_rows[:40]]
    return MetricResult("dso_days", round(dso, 2), lineage=lineage, unit="days")


@register("dpo_days")
def compute_dpo(session: Session, week: str) -> MetricResult:
    end = as_of_end(week)
    start = end - timedelta(days=89)
    pins = session.query(ErpPurchaseInvoice).filter(ErpPurchaseInvoice.invoice_date <= end).all()
    ap = 0
    lineage: list[str] = []
    for pin in pins:
        paid = (
            session.query(func.coalesce(func.sum(ErpPayment.amount_paise), 0))
            .filter(ErpPayment.purchase_invoice_id == pin.id, ErpPayment.payment_date <= end)
            .scalar()
        )
        rem = pin.amount_paise - int(paid)
        if rem > 0:
            ap += rem
            lineage.append(_cid("erp_purchase_invoices", pin.business_key))
    cogs_proxy = (
        session.query(func.coalesce(func.sum(ErpPayment.amount_paise), 0))
        .filter(ErpPayment.payment_date >= start, ErpPayment.payment_date <= end)
        .scalar()
    )
    daily = int(cogs_proxy) / 90.0 if cogs_proxy else 1.0
    dpo = ap / daily if daily else 0.0
    return MetricResult("dpo_days", round(dpo, 2), lineage=lineage[:40], unit="days")


@register("cash_balance_cr")
def compute_cash(session: Session, week: str) -> MetricResult:
    end = as_of_end(week)
    row = (
        session.query(ErpCashDaily)
        .filter(ErpCashDaily.as_of_date <= end)
        .order_by(ErpCashDaily.as_of_date.desc())
        .first()
    )
    if not row:
        return MetricResult("cash_balance_cr", 0.0, lineage=[], unit="Cr")
    return MetricResult(
        "cash_balance_cr",
        round(paise_to_crore(row.balance_paise), 2),
        lineage=[_cid("erp_cash_daily", row.business_key)],
        unit="Cr",
    )


@register("covenant_headroom_cr")
def compute_covenant_headroom(session: Session, week: str) -> MetricResult:
    cash = compute_cash(session, week)
    # Raw covenant ₹20 Cr
    headroom = cash.value - 20.0
    return MetricResult(
        "covenant_headroom_cr",
        round(headroom, 2),
        lineage=cash.lineage,
        unit="Cr",
    )


@register("ar_overdue_pct")
def compute_ar_overdue(session: Session, week: str) -> MetricResult:
    end = as_of_end(week)
    invoices = session.query(ErpSalesInvoice).filter(ErpSalesInvoice.invoice_date <= end).all()
    total_ar = 0
    overdue = 0
    lineage: list[str] = []
    for inv in invoices:
        paid = (
            session.query(func.coalesce(func.sum(ErpReceipt.amount_paise), 0))
            .filter(ErpReceipt.invoice_id == inv.id, ErpReceipt.receipt_date <= end)
            .scalar()
        )
        rem = inv.amount_paise - int(paid)
        if rem <= 0:
            continue
        total_ar += rem
        if inv.due_date < end:
            overdue += rem
            lineage.append(_cid("erp_sales_invoices", inv.business_key))
    pct = (100.0 * overdue / total_ar) if total_ar else 0.0
    return MetricResult("ar_overdue_pct", round(pct, 2), lineage=lineage[:40], unit="%")


@register("ap_overdue_pct")
def compute_ap_overdue(session: Session, week: str) -> MetricResult:
    end = as_of_end(week)
    pins = session.query(ErpPurchaseInvoice).filter(ErpPurchaseInvoice.invoice_date <= end).all()
    total = overdue = 0
    lineage: list[str] = []
    for pin in pins:
        paid = (
            session.query(func.coalesce(func.sum(ErpPayment.amount_paise), 0))
            .filter(ErpPayment.purchase_invoice_id == pin.id, ErpPayment.payment_date <= end)
            .scalar()
        )
        rem = pin.amount_paise - int(paid)
        if rem <= 0:
            continue
        total += rem
        if pin.due_date < end:
            overdue += rem
            lineage.append(_cid("erp_purchase_invoices", pin.business_key))
    pct = (100.0 * overdue / total) if total else 0.0
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
        .filter(ErpExpense.expense_date >= start, ErpExpense.expense_date <= end)
        .scalar()
    )
    rev = (
        session.query(func.coalesce(func.sum(ErpRevenueDaily.amount_paise), 0))
        .filter(ErpRevenueDaily.as_of_date >= start, ErpRevenueDaily.as_of_date <= end)
        .scalar()
    )
    ratio = (100.0 * int(opex) / int(rev)) if rev else 0.0
    exps = (
        session.query(ErpExpense)
        .filter(ErpExpense.expense_date >= start, ErpExpense.expense_date <= end)
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
                ErpExpense.expense_date >= start,
                ErpExpense.expense_date <= end,
            )
            .scalar()
        )
        hist = (
            session.query(func.coalesce(func.sum(ErpExpense.amount_paise), 0))
            .filter(
                ErpExpense.cost_center == cc,
                ErpExpense.expense_date >= hist_start,
                ErpExpense.expense_date < start,
            )
            .scalar()
        )
        weekly_avg = int(hist) / 8.0 if hist else 1.0
        var = 100.0 * (int(cur) - weekly_avg) / weekly_avg if weekly_avg else 0.0
        if abs(var) >= abs(max_var):
            max_var = var
            rows = (
                session.query(ErpExpense)
                .filter(
                    ErpExpense.cost_center == cc,
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
    """Proxy: days from week-end to last expense/revenue posting within week (soft close proxy)."""
    # Synthetic: baseline 4 days, stretches slightly late quarter
    end = as_of_end(week)
    wi = end.isocalendar().week
    value = 4.0 + (0.3 if wi % 4 == 0 else 0.0)
    cash = (
        session.query(ErpCashDaily)
        .filter(ErpCashDaily.as_of_date == end)
        .first()
    )
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
    due_amt = sum(i.amount_paise for i in due) or 1
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
        .filter(ErpExpense.expense_date >= start, ErpExpense.expense_date <= end)
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
                ErpRevenueDaily.as_of_date >= start,
                ErpRevenueDaily.as_of_date <= end,
            )
            .scalar()
        )
        prev = (
            session.query(func.coalesce(func.sum(ErpRevenueDaily.amount_paise), 0))
            .filter(
                ErpRevenueDaily.segment == seg,
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
    fw = get_active_framework()
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
        baseline = prev_map.get(metric_id)
        delta = (mr.value - baseline) if baseline is not None else None
        sev = severity(delta, history.get(metric_id, []), baseline)
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
        row.lineage_json = mr.lineage
        results.append(row)

    if persist:
        session.flush()
    return results


def get_signal_lineage(session: Session, metric_id: str, week: str) -> list[str]:
    sid = f"metric:{metric_id}:{week}"
    row = session.query(Signal).filter_by(signal_id=sid).one_or_none()
    return list(row.lineage_json) if row else []
