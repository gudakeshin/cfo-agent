"""Read-only data tools for the drill-down chat.

Every tool:
- only reads (no session writes beyond the derived Signal table the pipeline also maintains);
- is capped at an as-of week, so in replay mode the chat cannot see the synthetic
  quarter's "future";
- returns the citation IDs it used, which become the only IDs (besides the item's own
  evidence) the answer may cite.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from cfo_agent.models import (
    ErpCashDaily,
    ErpCovenant,
    ErpCustomer,
    ErpExpense,
    ErpGstEvent,
    ErpReceipt,
    ErpSalesInvoice,
    Signal,
)
from cfo_agent.signals import COMPUTE_FNS, as_of_end, compute_all_signals, metric_meta
from cfo_agent.timeutil import format_inr, iso_week_id, paise_to_crore, quarter_weeks_ending, week_sunday

MAX_ROWS = 25

DATA_TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_metric",
        "description": (
            "Get one finance metric for a week (default: the digest week): value, unit and change "
            "on the prior week. Metrics: " + ", ".join(sorted(COMPUTE_FNS))
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric_id": {"type": "string", "enum": sorted(COMPUTE_FNS)},
                "week": {"type": "string", "description": "ISO week like 2026-W27; optional"},
            },
            "required": ["metric_id"],
        },
    },
    {
        "name": "metric_trend",
        "description": "Weekly values of a metric over the last N weeks (max 13) up to the digest week.",
        "input_schema": {
            "type": "object",
            "properties": {
                "metric_id": {"type": "string", "enum": sorted(COMPUTE_FNS)},
                "weeks": {"type": "integer", "minimum": 2, "maximum": 13},
            },
            "required": ["metric_id"],
        },
    },
    {
        "name": "receivables_ageing",
        "description": (
            "Open receivables as of the digest week, bucketed by days overdue (current, 1-30, "
            "31-60, 61-90, 90+), with the largest overdue invoices and customers."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_records",
        "description": (
            "List ERP records (read-only, max 25) with optional filters. Tables: sales_invoices, "
            "receipts, gst_events, cash_daily, expenses, covenants."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "enum": ["sales_invoices", "receipts", "gst_events", "cash_daily", "expenses", "covenants"],
                },
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                "customer": {"type": "string", "description": "Customer name contains (sales_invoices)"},
                "status": {"type": "string"},
                "event_type": {"type": "string", "description": "gst_events only"},
                "cost_center": {"type": "string", "description": "expenses only"},
                "min_amount_cr": {"type": "number"},
                "order_by": {"type": "string", "enum": ["date_desc", "date_asc", "amount_desc"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
            },
            "required": ["table"],
        },
    },
    {
        "name": "what_if",
        "description": (
            "Deterministic scenario on the digest week's position. Scenarios: "
            "'cash_outflow' (amount_cr: one-off outflow; shows cash, covenant headroom and red-line "
            "status after it); 'dso_change' (days: +/- change in DSO; shows the cash tied up or "
            "released); 'collect_overdue' (pct: share of overdue receivables collected; shows cash "
            "after collection)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scenario": {"type": "string", "enum": ["cash_outflow", "dso_change", "collect_overdue"]},
                "amount_cr": {"type": "number"},
                "days": {"type": "number"},
                "pct": {"type": "number", "minimum": 0, "maximum": 100},
            },
            "required": ["scenario"],
        },
    },
]


def _cid(table: str, key: str) -> str:
    return f"erp:{table}:{key}"


def _cr(paise: int) -> float:
    return round(paise_to_crore(paise), 2)


class ChatDataContext:
    """Per-conversation state: the as-of week cap and every citation a tool returned."""

    def __init__(self, session: Session, as_of_week: str):
        self.session = session
        self.as_of_week = as_of_week
        self.citations: set[str] = set()
        self.tools_used: list[str] = []
        self.results: list[dict] = []

    # ── helpers ──────────────────────────────────────────────────────────────
    def _week(self, week: str | None) -> str:
        if not week or week > self.as_of_week:
            return self.as_of_week
        return week

    def _end(self) -> date:
        return as_of_end(self.as_of_week)

    def _metric_signal(self, metric_id: str, week: str) -> Signal | None:
        sid = f"metric:{metric_id}:{week}"
        sig = self.session.query(Signal).filter_by(signal_id=sid).one_or_none()
        if sig is None:
            # Same derived table the pipeline maintains; makes the metric citable.
            compute_all_signals(self.session, week, persist=True)
            sig = self.session.query(Signal).filter_by(signal_id=sid).one_or_none()
        return sig

    def _open_invoices(self) -> list[tuple[ErpSalesInvoice, int]]:
        end = self._end()
        paid = dict(
            self.session.query(ErpReceipt.invoice_id, func.coalesce(func.sum(ErpReceipt.amount_paise), 0))
            .filter(ErpReceipt.receipt_date <= end)
            .group_by(ErpReceipt.invoice_id)
            .all()
        )
        out = []
        for inv in self.session.query(ErpSalesInvoice).filter(ErpSalesInvoice.invoice_date <= end).all():
            rem = inv.amount_paise - int(paid.get(inv.id, 0))
            if rem > 0:
                out.append((inv, rem))
        return out

    # ── tools ────────────────────────────────────────────────────────────────
    def get_metric(self, metric_id: str, week: str | None = None) -> dict:
        if metric_id not in COMPUTE_FNS:
            return {"error": f"Unknown metric {metric_id}"}
        week = self._week(week)
        sig = self._metric_signal(metric_id, week)
        if sig is None:
            return {"error": f"{metric_id} is not available for {week}"}
        meta = metric_meta(metric_id)
        cid = sig.signal_id
        return {
            **meta,
            "week": week,
            "value": sig.value,
            "change_on_prior_week": sig.delta_wow,
            "citations": [cid],
        }

    def metric_trend(self, metric_id: str, weeks: int = 8) -> dict:
        if metric_id not in COMPUTE_FNS:
            return {"error": f"Unknown metric {metric_id}"}
        n = max(2, min(13, int(weeks or 8)))
        week_ids = quarter_weeks_ending(week_sunday(self.as_of_week), n)
        fn = COMPUTE_FNS[metric_id]
        points = []
        for w in week_ids:
            mr = fn(self.session, w)
            points.append({"week": w, "value": mr.value if mr.available else None})
        latest = self._metric_signal(metric_id, self.as_of_week)
        return {
            **metric_meta(metric_id),
            "points": points,
            "citations": [latest.signal_id] if latest else [],
        }

    def receivables_ageing(self) -> dict:
        end = self._end()
        buckets = {"current": 0, "1-30": 0, "31-60": 0, "61-90": 0, "90+": 0}
        overdue_rows = []
        customers = {c.id: c for c in self.session.query(ErpCustomer).all()}
        by_customer: dict[str, int] = {}
        for inv, rem in self._open_invoices():
            days = (end - inv.due_date).days
            if days <= 0:
                buckets["current"] += rem
                continue
            key = "1-30" if days <= 30 else "31-60" if days <= 60 else "61-90" if days <= 90 else "90+"
            buckets[key] += rem
            name = customers[inv.customer_id].name if inv.customer_id in customers else "?"
            by_customer[name] = by_customer.get(name, 0) + rem
            overdue_rows.append((rem, days, inv, name))
        overdue_rows.sort(key=lambda r: -r[0])
        top = overdue_rows[:8]
        overdue_metric = self._metric_signal("ar_overdue_pct", self.as_of_week)
        total = sum(buckets.values())
        overdue_total = total - buckets["current"]
        return {
            "as_of": end.isoformat(),
            "total_open_cr": _cr(total),
            "overdue_cr": _cr(overdue_total),
            "overdue_pct": round(100 * overdue_total / total, 1) if total else 0.0,
            "buckets_cr": {k: _cr(v) for k, v in buckets.items()},
            "bucket_bounds_days": [1, 30, 31, 60, 61, 90],
            "top_overdue_invoices": [
                {
                    "invoice": inv.business_key,
                    "customer": name,
                    "open_inr": format_inr(rem),
                    "open_cr": _cr(rem),
                    "days_overdue": days,
                    "citation": _cid("erp_sales_invoices", inv.business_key),
                }
                for rem, days, inv, name in top
            ],
            "top_overdue_customers": [
                {"customer": n, "overdue_cr": _cr(v)}
                for n, v in sorted(by_customer.items(), key=lambda kv: -kv[1])[:5]
            ],
            # Totals cite the receivables-overdue metric; per-invoice lines cite the invoice.
            "totals_citation": overdue_metric.signal_id if overdue_metric else None,
            "citations": ([overdue_metric.signal_id] if overdue_metric else [])
            + [_cid("erp_sales_invoices", r[2].business_key) for r in top],
        }

    def list_records(self, table: str, **f: Any) -> dict:
        end = self._end()
        limit = max(1, min(MAX_ROWS, int(f.get("limit") or 10)))
        date_to = min(date.fromisoformat(f["date_to"]), end) if f.get("date_to") else end
        date_from = date.fromisoformat(f["date_from"]) if f.get("date_from") else None
        min_paise = int(float(f["min_amount_cr"]) * 1e9) if f.get("min_amount_cr") is not None else None
        order = f.get("order_by") or "date_desc"

        spec = {
            "sales_invoices": (ErpSalesInvoice, ErpSalesInvoice.invoice_date, ErpSalesInvoice.amount_paise, "erp_sales_invoices"),
            "receipts": (ErpReceipt, ErpReceipt.receipt_date, ErpReceipt.amount_paise, "erp_receipts"),
            "gst_events": (ErpGstEvent, ErpGstEvent.event_date, ErpGstEvent.amount_paise, "erp_gst_events"),
            "cash_daily": (ErpCashDaily, ErpCashDaily.as_of_date, ErpCashDaily.balance_paise, "erp_cash_daily"),
            "expenses": (ErpExpense, ErpExpense.expense_date, ErpExpense.amount_paise, "erp_expenses"),
            "covenants": (ErpCovenant, None, ErpCovenant.threshold_paise, "erp_covenants"),
        }.get(table)
        if not spec:
            return {"error": f"Unknown table {table}"}
        model, date_col, amount_col, table_name = spec
        q = self.session.query(model)
        if date_col is not None:
            q = q.filter(date_col <= date_to)
            if date_from:
                q = q.filter(date_col >= date_from)
        if min_paise is not None:
            q = q.filter(amount_col >= min_paise)
        if f.get("status") and hasattr(model, "status"):
            q = q.filter(model.status == f["status"])
        if f.get("event_type") and model is ErpGstEvent:
            q = q.filter(ErpGstEvent.event_type == f["event_type"])
        if f.get("cost_center") and model is ErpExpense:
            q = q.filter(ErpExpense.cost_center == f["cost_center"])
        customers = {c.id: c.name for c in self.session.query(ErpCustomer).all()}
        if f.get("customer") and model is ErpSalesInvoice:
            needle = str(f["customer"]).lower()
            ids = [cid for cid, name in customers.items() if needle in name.lower()]
            q = q.filter(ErpSalesInvoice.customer_id.in_(ids or [-1]))
        if order == "amount_desc":
            q = q.order_by(amount_col.desc())
        elif date_col is not None:
            q = q.order_by(date_col.asc() if order == "date_asc" else date_col.desc())
        rows = q.limit(limit).all()

        out = []
        for r in rows:
            d: dict[str, Any] = {"key": r.business_key}
            for col in r.__table__.columns:
                if col.name in ("id", "business_key"):
                    continue
                v = getattr(r, col.name)
                if col.name.endswith("_paise"):
                    d[col.name.removesuffix("_paise") + "_inr"] = format_inr(int(v))
                    d[col.name.removesuffix("_paise") + "_cr"] = _cr(int(v))
                elif col.name == "customer_id":
                    d["customer"] = customers.get(v, "?")
                elif hasattr(v, "isoformat"):
                    d[col.name] = v.isoformat()
                else:
                    d[col.name] = v
            d["citation"] = _cid(table_name, r.business_key)
            out.append(d)
        return {"table": table, "count": len(out), "rows": out, "citations": [d["citation"] for d in out]}

    def what_if(self, scenario: str, amount_cr: float | None = None, days: float | None = None, pct: float | None = None) -> dict:
        end = self._end()
        cash_row = (
            self.session.query(ErpCashDaily)
            .filter(ErpCashDaily.as_of_date <= end)
            .order_by(ErpCashDaily.as_of_date.desc())
            .first()
        )
        if not cash_row:
            return {"error": "No cash data"}
        cov = self.session.query(ErpCovenant).filter_by(metric="cash_balance_cr").first()
        cash = _cr(cash_row.balance_paise)
        cov_min = _cr(cov.threshold_paise) if cov else 20.0
        buffer_min = _cr(cov.buffered_threshold_paise) if cov else 22.0
        cites = [_cid("erp_cash_daily", cash_row.business_key)] + ([_cid("erp_covenants", cov.business_key)] if cov else [])

        def position(new_cash: float) -> dict:
            return {
                "cash_after_cr": round(new_cash, 2),
                "covenant_minimum_cr": cov_min,
                "headroom_after_cr": round(new_cash - cov_min, 2),
                "below_buffered_red_line": new_cash < buffer_min,
                "breaches_covenant": new_cash < cov_min,
            }

        base = {"scenario": scenario, "as_of": cash_row.as_of_date.isoformat(), "cash_now_cr": cash}
        if scenario == "cash_outflow":
            amt = abs(float(amount_cr or 0))
            return {**base, "outflow_cr": amt, **position(cash - amt), "citations": cites}

        if scenario in ("dso_change", "collect_overdue"):
            start = end - timedelta(days=29)
            credit_sales = (
                self.session.query(func.coalesce(func.sum(ErpSalesInvoice.amount_paise), 0))
                .filter(ErpSalesInvoice.invoice_date >= start, ErpSalesInvoice.invoice_date <= end)
                .scalar()
            )
            dso_sig = self._metric_signal("dso_days", self.as_of_week)
            if dso_sig:
                cites.append(dso_sig.signal_id)
            if scenario == "dso_change":
                if not credit_sales:
                    return {"error": "No credit sales in the last 30 days; DSO effect is undefined"}
                daily_cr = _cr(int(credit_sales)) / 30.0
                d = float(days or 0)
                tied_up = round(daily_cr * d, 2)  # +days → more cash tied up in receivables
                return {
                    **base,
                    "dso_change_days": d,
                    "avg_daily_credit_sales_cr": round(daily_cr, 3),
                    "cash_tied_up_cr": tied_up,
                    **position(cash - tied_up),
                    "citations": cites,
                }
            share = max(0.0, min(100.0, float(pct or 0))) / 100.0
            overdue = sum(rem for inv, rem in self._open_invoices() if inv.due_date < end)
            collected = round(_cr(overdue) * share, 2)
            return {
                **base,
                "overdue_receivables_cr": _cr(overdue),
                "collected_pct": round(share * 100, 1),
                "cash_collected_cr": collected,
                **position(cash + collected),
                "citations": cites,
            }
        return {"error": f"Unknown scenario {scenario}"}

    # ── dispatch ─────────────────────────────────────────────────────────────
    def dispatch(self, name: str, args: dict[str, Any]) -> str:
        try:
            fn = {
                "get_metric": lambda a: self.get_metric(a["metric_id"], a.get("week")),
                "metric_trend": lambda a: self.metric_trend(a["metric_id"], a.get("weeks") or 8),
                "receivables_ageing": lambda a: self.receivables_ageing(),
                "list_records": lambda a: self.list_records(a.pop("table"), **a),
                "what_if": lambda a: self.what_if(a["scenario"], a.get("amount_cr"), a.get("days"), a.get("pct")),
            }.get(name)
            if fn is None:
                return json.dumps({"error": f"Unknown tool {name}"})
            result = fn(dict(args or {}))
        except Exception as e:  # noqa: BLE001 — tool errors go back to the model
            result = {"error": str(e)}
        self.tools_used.append(name)
        if isinstance(result, dict):
            self.citations.update(result.get("citations") or [])
            self.results.append(result)
        return json.dumps(result, default=str)


def chat_as_of_week(session: Session, digest_week: str) -> str:
    """Replay mode caps the chat at the digest's week; live mode at the latest data."""
    from cfo_agent.settings import get_settings

    if get_settings().data_mode != "live":
        return digest_week
    latest = session.query(func.max(ErpCashDaily.as_of_date)).scalar()
    return iso_week_id(latest) if latest else digest_week
