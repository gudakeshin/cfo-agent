"""What acting on an insight is worth, in rupees, and by when.

Only where the records support a figure: past-due receivables and payables, the
shortfall to a cash-covenant buffer, open GST notices and input credit at risk, and
what one day of DSO / DPO / inventory is worth. Everything else gets no impact line,
never an estimate. Each figure is a sum of records (cited), and each deadline states
where it comes from, so a number here can always be traced and challenged."""

from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from cfo_agent.models import (
    ErpCovenant,
    ErpCustomer,
    ErpGstEvent,
    ErpPurchaseInvoice,
    ErpSalesInvoice,
)
from cfo_agent.signals import (
    _cid,
    _closed_months,
    _days_in_periods,
    _entity_conds,
    _gl_totals,
    as_of_end,
    cash_positions,
    open_payables,
    open_receivables,
)
from cfo_agent.timeutil import format_inr

logger = logging.getLogger(__name__)

TOP_N = 3
NOTICE_REPLY_DAYS = 30  # typical window to answer a GST show-cause notice; the notice itself governs

RECEIVABLES = {"ar_overdue_pct", "dso_days"}
PAYABLES = {"ap_overdue_pct", "dpo_days"}
CASH = {"cash_balance_cr", "covenant_headroom_cr"}
GST = {"gst_open_notices": "show_cause_notice", "gst_notice_exposure_cr": "show_cause_notice",
       "itc_at_risk_cr": "itc_mismatch"}


def _names(pairs: list[tuple[str, int]]) -> str:
    return ", ".join(f"{n} ({format_inr(a)})" for n, a in pairs)


def _result(label: str, amount: int | None, sentence: str, *, basis: str, citations: list[str],
            per_day: int | None = None, deadline: date | None = None, deadline_basis: str = "") -> dict:
    out = {
        "label": label,
        "amount_paise": None if amount is None else int(amount),
        "amount_inr": None if amount is None else format_inr(int(amount)),
        "basis": basis,
        "deadline": deadline.isoformat() if deadline else None,
        "deadline_basis": deadline_basis if deadline else "",
        "citations": citations,
    }
    if per_day is not None:
        out["per_day_paise"] = int(per_day)
        out["per_day_inr"] = format_inr(int(per_day))
    out["sentence"] = sentence + (f" Aim to finish by {deadline.isoformat()}." if deadline else "")
    return out


def _overdue_by_party(pairs, end: date, key_fn, doc_cid):
    groups: dict[str, list] = {}
    for doc, rem in pairs:
        if doc.due_date < end:
            g = groups.setdefault(key_fn(doc), [0, None, 0])
            g[0] += rem
            if rem > g[2]:
                g[1], g[2] = doc, rem
    ranked = sorted(groups.items(), key=lambda kv: (-kv[1][0], kv[0]))
    total = sum(v[0] for v in groups.values())
    top = ranked[:TOP_N]
    return total, [(k, v[0]) for k, v in top], [doc_cid(v[1]) for _, v in top]


def _receivables_impact(session: Session, metric_id: str, week: str) -> dict | None:
    end = as_of_end(week)
    pairs = open_receivables(session, end)
    names = dict(session.query(ErpCustomer.id, ErpCustomer.name).all())
    total, top, cites = _overdue_by_party(
        pairs, end, lambda inv: names.get(inv.customer_id, "Unknown customer"),
        lambda inv: _cid("erp_sales_invoices", inv.business_key))
    per_day = None
    if metric_id == "dso_days":
        start = end - timedelta(days=29)
        sales = (
            session.query(func.coalesce(func.sum(ErpSalesInvoice.amount_paise), 0))
            .join(ErpCustomer, ErpCustomer.id == ErpSalesInvoice.customer_id)
            .filter(ErpSalesInvoice.invoice_date >= start, ErpSalesInvoice.invoice_date <= end,
                    ErpCustomer.is_intercompany.is_(False), *_entity_conds(session, ErpSalesInvoice))
            .scalar()
        )
        per_day = int(sales) // 30 or None
    if total <= 0 and per_day is None:
        return None
    parts = []
    if total > 0:
        parts.append(f"Collect the {format_inr(total)} that is past due, starting with {_names(top)}.")
    if per_day:
        parts.append(f"Each day taken off DSO releases about {format_inr(per_day)}.")
    return _result(
        "past due from customers", total or None, " ".join(parts),
        basis="Open invoices past their due date at week-end, summed by customer; the largest three are named. "
              + ("One day of DSO is the last 30 days' credit sales divided by 30." if per_day else ""),
        citations=cites, per_day=per_day,
    )


def _payables_impact(session: Session, metric_id: str, week: str) -> dict | None:
    end = as_of_end(week)
    pairs = open_payables(session, end)
    total, top, cites = _overdue_by_party(
        pairs, end, lambda p: p.vendor_name, lambda p: _cid("erp_purchase_invoices", p.business_key))
    per_day = None
    if metric_id == "dpo_days":
        buys = (
            session.query(func.coalesce(func.sum(ErpPurchaseInvoice.amount_paise), 0))
            .filter(ErpPurchaseInvoice.invoice_date >= end - timedelta(days=89), ErpPurchaseInvoice.invoice_date <= end,
                    ErpPurchaseInvoice.is_intercompany.is_(False), *_entity_conds(session, ErpPurchaseInvoice))
            .scalar()
        )
        per_day = int(buys) // 90 or None
    if total <= 0 and per_day is None:
        return None
    parts = []
    if total > 0:
        parts.append(f"Clear or agree revised dates on the {format_inr(total)} past due to vendors, starting with {_names(top)}.")
    if per_day:
        parts.append(f"Each day of DPO is worth about {format_inr(per_day)} of cash.")
    return _result(
        "past due to vendors", total or None, " ".join(parts),
        basis="Open purchase invoices past their due date at week-end, summed by vendor; the largest three are named."
              + (" One day of DPO is the last 90 days' purchases divided by 90." if per_day else ""),
        citations=cites, per_day=per_day,
    )


def _cash_impact(session: Session, week: str) -> dict | None:
    from cfo_agent.analytics import holt_forecast, value_history

    end = as_of_end(week)
    cov = session.query(ErpCovenant).filter_by(metric="cash_balance_cr").order_by(ErpCovenant.id).first()
    positions = cash_positions(session, end)
    if cov is None or not positions or (cov.operator or "gte") not in ("gte", "gt"):
        return None
    cash = sum(r.balance_paise for r, _ in positions)
    cites = [_cid("erp_covenants", cov.business_key)] + [_cid("erp_cash_daily", r.business_key) for r, _ in positions]
    if cash < cov.buffered_threshold_paise:
        short = cov.buffered_threshold_paise - cash
        breached = cash < cov.threshold_paise
        sentence = (f"Find {format_inr(short)} of cash to get back above the {format_inr(cov.buffered_threshold_paise)} "
                    f"buffer" + (f"; the {format_inr(cov.threshold_paise)} covenant itself is already breached." if breached else "."))
        return _result("to restore the covenant buffer", short, sentence,
                       basis=f"Cash of {format_inr(cash)} against the buffered covenant level of "
                             f"{format_inr(cov.buffered_threshold_paise)}.",
                       citations=cites)
    # Above the buffer: only matters if the trend says it will not stay there.
    hist = value_history(session, "cash_balance_cr", week)
    fc = holt_forecast(hist + [cash / 1e9]) if len(hist) >= 3 else []
    thr = cov.threshold_paise / 1e9
    h = next((f["h"] for f in fc if f["value"] < thr), None)
    if h is None:
        return None
    due = end + timedelta(days=7 * h)
    return _result("cushion that the trend uses up", cash - cov.threshold_paise,
                   f"Cash has {format_inr(cash - cov.threshold_paise)} of room above the {format_inr(cov.threshold_paise)} "
                   f"covenant, but the four-week trend crosses it in about {h} week{'s' if h != 1 else ''}.",
                   basis="A short-horizon trend forecast of cash against the covenant threshold.",
                   citations=cites, deadline=due, deadline_basis=f"when the trend crosses the covenant, about {h} weeks out")


def _gst_impact(session: Session, event_type: str, week: str, keys: list[str] | None = None) -> dict | None:
    end = as_of_end(week)
    q = session.query(ErpGstEvent).filter(ErpGstEvent.status == "open", ErpGstEvent.event_date <= end)
    q = q.filter(ErpGstEvent.business_key.in_(keys)) if keys else q.filter(ErpGstEvent.event_type == event_type)
    events = q.order_by(ErpGstEvent.event_date, ErpGstEvent.id).all()
    if not events:
        return None
    total = sum(e.amount_paise for e in events)
    cites = [_cid("erp_gst_events", e.business_key) for e in events[:6]]
    kind = events[0].event_type
    if kind == "itc_mismatch":
        from cfo_agent.statutory import upcoming

        nxt = next((d for d in upcoming(end + timedelta(days=1), 45) if d["id"] == "gstr3b"), None)
        due = date.fromisoformat(nxt["due_date"]) if nxt else None
        return _result("input credit at risk", total,
                       f"Reconcile the {format_inr(total)} of input credit at risk before it is claimed in the return.",
                       basis="Open input-tax-credit mismatches at week-end.", citations=cites,
                       deadline=due, deadline_basis="the next standard GSTR-3B due date")
    oldest = events[0].event_date
    due = oldest + timedelta(days=NOTICE_REPLY_DAYS)
    n = len(events)
    res = _result("under open GST notices", total,
                   f"Respond to the {n} open notice{'s' if n != 1 else ''}, {format_inr(total)} in total.",
                   basis="Open show-cause notices at week-end, summed.", citations=cites, deadline=due,
                   deadline_basis=f"{NOTICE_REPLY_DAYS} days after the oldest open notice ({oldest.isoformat()}), the usual "
                                  "reply window; the notice itself states the actual date")
    res["window_days"] = NOTICE_REPLY_DAYS
    return res


def _inventory_impact(session: Session, week: str) -> dict | None:
    end = as_of_end(week)
    months = _closed_months(session, end)
    if not months:
        return None
    cogs = _gl_totals(session, months).get("cogs", 0)
    if cogs <= 0:
        return None
    per_day = int(cogs / _days_in_periods(months))
    return _result("cash tied up per day of inventory", None,
                   f"Each day taken off inventory days frees about {format_inr(per_day)} of cash.",
                   basis="Daily cost of goods sold over the last three closed ledger months.", citations=[], per_day=per_day)


def compute_impact(session: Session, metric_id: str | None, week: str, *, event_keys: list[str] | None = None) -> dict | None:
    """Value and deadline of acting on the insight, or None where the records do not
    support one. Never raises: a failure here must not cost the CFO the insight."""
    try:
        if metric_id in RECEIVABLES:
            return _receivables_impact(session, metric_id, week)
        if metric_id in PAYABLES:
            return _payables_impact(session, metric_id, week)
        if metric_id in CASH:
            return _cash_impact(session, week)
        if metric_id in GST:
            return _gst_impact(session, GST[metric_id], week)
        if metric_id == "dio_days":
            return _inventory_impact(session, week)
        if metric_id is None and event_keys:
            return _gst_impact(session, "show_cause_notice", week, event_keys)
    except Exception:  # noqa: BLE001
        logger.exception("Impact failed for %s %s", metric_id, week)
    return None
