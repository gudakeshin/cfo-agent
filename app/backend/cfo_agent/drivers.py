"""What moved a metric: a deterministic breakdown by customer, vendor, cost centre,
segment or event, with one citable record per line.

The synthesis step used to receive only a metric's value, its change and a list of
record IDs, so "why" was written from the KRA description. `compute_drivers` gives
it (and the insight card) the actual contributors, computed here in SQL — the model
never derives them.

Shape (JSON-able, stored on the digest item under score_decomp["drivers"]):

    {"title": "...", "basis": "...",
     "rows": [{"label", "group", "current_paise", "previous_paise", "change_paise",
               "citation", "note"}],
     "reconciliation": {...} | None}

Amounts are INR paise; `previous_paise` / `change_paise` are None where a prior
period does not apply. Rows are the largest few only — `basis` says so.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from cfo_agent.models import (
    ErpCustomer,
    ErpExpense,
    ErpGstEvent,
    ErpPayment,
    ErpPurchaseInvoice,
    ErpReceipt,
    ErpRevenueDaily,
    ErpSalesInvoice,
)
from cfo_agent.signals import (
    _cid,
    _dimension_values,
    _entity_conds,
    _external_payments,
    _external_receipts,
    as_of_end,
    cash_positions,
    open_payables,
    open_receivables,
)
from cfo_agent.timeutil import week_monday

logger = logging.getLogger(__name__)

MAX_ROWS = 5

RECEIVABLE_METRICS = {"dso_days": False, "ar_overdue_pct": True}  # value: overdue only
PAYABLE_METRICS = {"dpo_days": False, "ap_overdue_pct": True}
CASH_METRICS = {"cash_balance_cr", "covenant_headroom_cr", "free_cash_flow_cr"}
COST_METRICS = {"opex_ratio_pct", "cost_center_variance_pct"}
SEGMENT_METRICS = {"segment_revenue_growth_pct", "revenue_concentration_pct"}
GST_METRICS = {
    "gst_open_notices": "show_cause_notice",
    "gst_notice_exposure_cr": "show_cause_notice",
    "itc_at_risk_cr": "itc_mismatch",
}


def _row(label: str, group: str, current: int, previous: int | None, citation: str | None, note: str = "") -> dict:
    return {
        "label": label,
        "group": group,
        "current_paise": int(current),
        "previous_paise": None if previous is None else int(previous),
        "change_paise": None if previous is None else int(current) - int(previous),
        "citation": citation,
        "note": note,
    }


def _top(rows: list[dict], *, by_change: bool) -> list[dict]:
    key = (lambda r: -abs(r["change_paise"] or 0)) if by_change else (lambda r: -abs(r["current_paise"]))
    rows = [r for r in rows if (r["change_paise"] if by_change else r["current_paise"])]
    return sorted(rows, key=lambda r: (key(r), r["label"]))[:MAX_ROWS]


# ── receivables / payables ───────────────────────────────────────────────────

def _balances(pairs, end: date, overdue_only: bool, key_fn) -> dict[Any, tuple[int, Any]]:
    """group key → (open amount, the largest open document in the group)."""
    out: dict[Any, list] = {}
    for doc, rem in pairs:
        if overdue_only and not doc.due_date < end:
            continue
        slot = out.setdefault(key_fn(doc), [0, None, 0])
        slot[0] += rem
        if rem > slot[2]:
            slot[1], slot[2] = doc, rem
    return {k: (v[0], v[1]) for k, v in out.items()}


def _receivable_drivers(session: Session, week: str, overdue_only: bool) -> dict | None:
    end = as_of_end(week)
    prev_end = end - timedelta(days=7)
    cur = _balances(open_receivables(session, end), end, overdue_only, lambda inv: inv.customer_id)
    prev = _balances(open_receivables(session, prev_end), prev_end, overdue_only, lambda inv: inv.customer_id)
    names = dict(session.query(ErpCustomer.id, ErpCustomer.name).all())
    rows = []
    for cust_id in set(cur) | set(prev):
        amount, doc = cur.get(cust_id, (0, None))
        before, doc_before = prev.get(cust_id, (0, None))
        ref = doc or doc_before
        rows.append(_row(names.get(cust_id, "Unknown customer"), "customer", amount, before,
                         _cid("erp_sales_invoices", ref.business_key) if ref else None))
    rows = _top(rows, by_change=True)
    if not rows:
        return None
    what = "overdue receivables" if overdue_only else "open receivables"
    return {
        "title": f"Customers behind the change in {what}",
        "basis": f"Change in {what} by customer against the prior week-end; the {len(rows)} largest moves. "
                 "Each line cites that customer's largest open invoice.",
        "rows": rows,
        "reconciliation": {
            "total_current_paise": sum(v[0] for v in cur.values()),
            "total_previous_paise": sum(v[0] for v in prev.values()),
        },
    }


def _payable_drivers(session: Session, week: str, overdue_only: bool) -> dict | None:
    end = as_of_end(week)
    prev_end = end - timedelta(days=7)
    cur = _balances(open_payables(session, end), end, overdue_only, lambda p: p.vendor_name)
    prev = _balances(open_payables(session, prev_end), prev_end, overdue_only, lambda p: p.vendor_name)
    rows = []
    for vendor in set(cur) | set(prev):
        amount, doc = cur.get(vendor, (0, None))
        before, doc_before = prev.get(vendor, (0, None))
        ref = doc or doc_before
        rows.append(_row(vendor, "vendor", amount, before,
                         _cid("erp_purchase_invoices", ref.business_key) if ref else None))
    rows = _top(rows, by_change=True)
    if not rows:
        return None
    what = "overdue payables" if overdue_only else "open payables"
    return {
        "title": f"Vendors behind the change in {what}",
        "basis": f"Change in {what} by vendor against the prior week-end; the {len(rows)} largest moves. "
                 "Each line cites that vendor's largest open invoice.",
        "rows": rows,
        "reconciliation": {
            "total_current_paise": sum(v[0] for v in cur.values()),
            "total_previous_paise": sum(v[0] for v in prev.values()),
        },
    }


# ── cash ─────────────────────────────────────────────────────────────────────

def _receipts_by_customer(session: Session, start: date, end: date) -> dict[str, int]:
    q = _external_receipts(session, start, end).with_entities(ErpCustomer.name, func.sum(ErpReceipt.amount_paise))
    return {n: int(v or 0) for n, v in q.group_by(ErpCustomer.name)}


def _payments_by_vendor(session: Session, start: date, end: date) -> dict[str, int]:
    q = _external_payments(session, start, end).with_entities(
        ErpPurchaseInvoice.vendor_name, func.sum(ErpPayment.amount_paise))
    return {n: int(v or 0) for n, v in q.group_by(ErpPurchaseInvoice.vendor_name)}


def _expenses_by_cost_centre(session: Session, start: date, end: date) -> dict[str, int]:
    q = (
        session.query(ErpExpense.cost_center, func.sum(ErpExpense.amount_paise))
        .filter(ErpExpense.expense_date >= start, ErpExpense.expense_date <= end, *_entity_conds(session, ErpExpense))
        .group_by(ErpExpense.cost_center)
    )
    return {n: int(v or 0) for n, v in q}


def _largest_expense(session: Session, cc: str, start: date, end: date) -> str | None:
    e = (
        session.query(ErpExpense)
        .filter(ErpExpense.cost_center == cc, ErpExpense.expense_date >= start, ErpExpense.expense_date <= end,
                *_entity_conds(session, ErpExpense))
        .order_by(ErpExpense.amount_paise.desc(), ErpExpense.id).first()
    )
    return _cid("erp_expenses", e.business_key) if e else None


def _cash_drivers(session: Session, week: str) -> dict | None:
    end = as_of_end(week)
    start = week_monday(week)
    p_start, p_end = start - timedelta(days=7), start - timedelta(days=1)

    rows: list[dict] = []
    cur_in, prev_in = _receipts_by_customer(session, start, end), _receipts_by_customer(session, p_start, p_end)
    for name, amount in cur_in.items():
        r = (_external_receipts(session, start, end).filter(ErpCustomer.name == name)
             .order_by(ErpReceipt.amount_paise.desc(), ErpReceipt.id).first())
        rows.append(_row(name, "received from customer", amount, prev_in.get(name, 0),
                         _cid("erp_receipts", r.business_key) if r else None))
    cur_out, prev_out = _payments_by_vendor(session, start, end), _payments_by_vendor(session, p_start, p_end)
    for name, amount in cur_out.items():
        r = (_external_payments(session, start, end).filter(ErpPurchaseInvoice.vendor_name == name)
             .order_by(ErpPayment.amount_paise.desc(), ErpPayment.id).first())
        rows.append(_row(name, "paid to vendor", -amount, -prev_out.get(name, 0),
                         _cid("erp_payments", r.business_key) if r else None))
    cur_exp, prev_exp = _expenses_by_cost_centre(session, start, end), _expenses_by_cost_centre(session, p_start, p_end)
    for name, amount in cur_exp.items():
        rows.append(_row(name, "expenses, cost centre", -amount, -prev_exp.get(name, 0),
                         _largest_expense(session, name, start, end)))
    net = sum(cur_in.values()) - sum(cur_out.values()) - sum(cur_exp.values())
    rows = _top(rows, by_change=False)
    if not rows:
        return None

    recon: dict[str, Any] = {
        "inflows_paise": sum(cur_in.values()),
        "outflows_paise": sum(cur_out.values()) + sum(cur_exp.values()),
        "net_recorded_flow_paise": net,
    }
    now, before = cash_positions(session, end), cash_positions(session, p_end)
    if now and before:
        change = sum(r.balance_paise for r, _ in now) - sum(r.balance_paise for r, _ in before)
        recon["balance_change_paise"] = change
        # Bank balance moves that are not in the receipts / payments / expenses feed
        # (financing, capex, tax, transfers). Stated, never guessed at.
        recon["not_in_feed_paise"] = change - net
    return {
        "title": "Largest cash movements this week",
        "basis": f"Receipts by customer (+), supplier payments by vendor (−) and expenses by cost centre (−) "
                 f"for the week; the {len(rows)} largest. `previous` is the same line last week. "
                 "`not_in_feed` is the part of the bank-balance change these records do not explain.",
        "rows": rows,
        "reconciliation": recon,
    }


# ── cost, segments, collections, GST ─────────────────────────────────────────

def _cost_drivers(session: Session, week: str) -> dict | None:
    end = as_of_end(week)
    start = week_monday(week)
    hist_start = start - timedelta(days=56)
    cur = _expenses_by_cost_centre(session, start, end)
    hist = _expenses_by_cost_centre(session, hist_start, start - timedelta(days=1))
    rows = [
        _row(cc, "cost centre", cur.get(cc, 0), round(hist.get(cc, 0) / 8), _largest_expense(session, cc, start, end))
        for cc in set(cur) | set(hist)
    ]
    rows = _top(rows, by_change=True)
    if not rows:
        return None
    return {
        "title": "Cost centres against their 8-week average",
        "basis": f"This week's spend by cost centre against its average week over the prior 8 weeks "
                 f"(`previous`); the {len(rows)} largest differences. Each line cites the week's largest expense.",
        "rows": rows,
        "reconciliation": {"total_current_paise": sum(cur.values()),
                           "total_previous_paise": round(sum(hist.values()) / 8)},
    }


def _segment_drivers(session: Session, week: str) -> dict | None:
    end = as_of_end(week)
    start = week_monday(week)
    p_start, p_end = start - timedelta(days=7), start - timedelta(days=1)

    def totals(a: date, b: date) -> dict[str, int]:
        q = (
            session.query(ErpRevenueDaily.segment, func.sum(ErpRevenueDaily.amount_paise))
            .filter(ErpRevenueDaily.as_of_date >= a, ErpRevenueDaily.as_of_date <= b,
                    *_entity_conds(session, ErpRevenueDaily))
            .group_by(ErpRevenueDaily.segment)
        )
        return {n: int(v or 0) for n, v in q}

    cur, prev = totals(start, end), totals(p_start, p_end)
    rows = []
    for seg in _dimension_values(session, ErpRevenueDaily, ErpRevenueDaily.segment, ErpRevenueDaily.as_of_date, p_start, end):
        top = (
            session.query(ErpRevenueDaily)
            .filter(ErpRevenueDaily.segment == seg, ErpRevenueDaily.as_of_date >= start, ErpRevenueDaily.as_of_date <= end,
                    *_entity_conds(session, ErpRevenueDaily))
            .order_by(ErpRevenueDaily.amount_paise.desc(), ErpRevenueDaily.id).first()
        )
        rows.append(_row(seg, "segment", cur.get(seg, 0), prev.get(seg, 0),
                         _cid("erp_revenue_daily", top.business_key) if top else None))
    rows = _top(rows, by_change=False)
    if not rows:
        return None
    return {
        "title": "Revenue by segment, this week against last",
        "basis": "Week's revenue by segment against the prior week. Each line cites the segment's largest revenue day.",
        "rows": rows,
        "reconciliation": {"total_current_paise": sum(cur.values()), "total_previous_paise": sum(prev.values())},
    }


def _collection_drivers(session: Session, week: str) -> dict | None:
    end = as_of_end(week)
    start = week_monday(week)
    by_cust: dict[int, list] = {}
    for inv, rem in open_receivables(session, end):
        if start <= inv.due_date <= end:
            slot = by_cust.setdefault(inv.customer_id, [0, None, 0])
            slot[0] += rem
            if rem > slot[2]:
                slot[1], slot[2] = inv, rem
    names = dict(session.query(ErpCustomer.id, ErpCustomer.name).all())
    rows = _top(
        [_row(names.get(cid, "Unknown customer"), "customer", v[0], None, _cid("erp_sales_invoices", v[1].business_key))
         for cid, v in by_cust.items()],
        by_change=False,
    )
    if not rows:
        return None
    return {
        "title": "Customers who did not pay what fell due",
        "basis": f"Invoices that fell due this week and are still unpaid at week-end, by customer; the {len(rows)} "
                 "largest. Each line cites the customer's largest such invoice.",
        "rows": rows,
        "reconciliation": {"total_current_paise": sum(v[0] for v in by_cust.values())},
    }


def _gst_drivers(session: Session, week: str, event_type: str) -> dict | None:
    end = as_of_end(week)
    start = week_monday(week)
    events = (
        session.query(ErpGstEvent)
        .filter(ErpGstEvent.event_type == event_type, ErpGstEvent.status == "open", ErpGstEvent.event_date <= end)
        .order_by(ErpGstEvent.amount_paise.desc(), ErpGstEvent.id).all()
    )
    if not events:
        return None
    rows = [
        _row(e.business_key, "open event", e.amount_paise, None, _cid("erp_gst_events", e.business_key),
             "new this week" if e.event_date >= start else f"open since {e.event_date.isoformat()}")
        for e in events[:MAX_ROWS]
    ]
    return {
        "title": "Open items behind this figure",
        "basis": f"Open {event_type.replace('_', ' ')} records at week-end, largest first.",
        "rows": rows,
        "reconciliation": {"total_current_paise": sum(e.amount_paise for e in events), "open_count": len(events)},
    }


# ── entry point ──────────────────────────────────────────────────────────────

def compute_drivers(session: Session, metric_id: str | None, week: str) -> dict | None:
    """Breakdown for one metric, or None when there is none to give (custom metrics,
    metrics with no contributing records). Never raises: a failure here must not
    cost the CFO the insight itself."""
    if not metric_id:
        return None
    try:
        if metric_id in RECEIVABLE_METRICS:
            return _receivable_drivers(session, week, RECEIVABLE_METRICS[metric_id])
        if metric_id in PAYABLE_METRICS:
            return _payable_drivers(session, week, PAYABLE_METRICS[metric_id])
        if metric_id in CASH_METRICS:
            return _cash_drivers(session, week)
        if metric_id in COST_METRICS:
            return _cost_drivers(session, week)
        if metric_id in SEGMENT_METRICS:
            return _segment_drivers(session, week)
        if metric_id == "collections_coverage_pct":
            return _collection_drivers(session, week)
        if metric_id in GST_METRICS:
            return _gst_drivers(session, week, GST_METRICS[metric_id])
    except Exception:  # noqa: BLE001
        logger.exception("Driver breakdown failed for %s %s", metric_id, week)
    return None


def driver_citations(drivers: dict | None) -> list[str]:
    return [r["citation"] for r in (drivers or {}).get("rows", []) if r.get("citation")]
