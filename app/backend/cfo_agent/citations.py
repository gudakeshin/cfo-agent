"""Citation scheme, resolver, cite-or-suppress gate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from cfo_agent.models import (
    ErpCashDaily,
    ErpCovenant,
    ErpCustomer,
    ErpExpense,
    ErpGstEvent,
    ErpPayment,
    ErpPurchaseInvoice,
    ErpReceipt,
    ErpRevenueDaily,
    ErpSalesInvoice,
    Signal,
)
from cfo_agent.timeutil import format_inr

TABLE_MODEL = {
    "erp_customers": ErpCustomer,
    "erp_sales_invoices": ErpSalesInvoice,
    "erp_receipts": ErpReceipt,
    "erp_purchase_invoices": ErpPurchaseInvoice,
    "erp_payments": ErpPayment,
    "erp_expenses": ErpExpense,
    "erp_cash_daily": ErpCashDaily,
    "erp_revenue_daily": ErpRevenueDaily,
    "erp_gst_events": ErpGstEvent,
    "erp_covenants": ErpCovenant,
}


@dataclass
class ResolvedCitation:
    citation_id: str
    ok: bool
    summary: str
    record: dict[str, Any] | None = None
    error: str | None = None


def parse_citation(cid: str) -> tuple[str, ...]:
    parts = cid.split(":")
    return tuple(parts)


def resolve_citation(session: Session, cid: str) -> ResolvedCitation:
    parts = parse_citation(cid)
    if len(parts) < 2:
        return ResolvedCitation(cid, False, "", error="Malformed citation")

    kind = parts[0]
    if kind == "erp":
        if len(parts) != 3:
            return ResolvedCitation(cid, False, "", error="erp citation needs table:key")
        _, table, key = parts
        model = TABLE_MODEL.get(table)
        if not model:
            return ResolvedCitation(cid, False, "", error=f"Unknown table {table}")
        row = session.query(model).filter_by(business_key=key).one_or_none()
        if not row:
            return ResolvedCitation(cid, False, "", error=f"No row for {cid}")
        summary, record = _summarize_erp(session, table, row)
        return ResolvedCitation(cid, True, summary, record=record)

    if kind == "metric":
        if len(parts) != 3:
            return ResolvedCitation(cid, False, "", error="metric citation needs id:week")
        _, metric_id, week = parts
        sig = session.query(Signal).filter_by(signal_id=cid).one_or_none()
        if not sig:
            # try compose
            sig = (
                session.query(Signal)
                .filter_by(metric_id=metric_id, week=week)
                .one_or_none()
            )
        if not sig:
            return ResolvedCitation(cid, False, "", error=f"No signal for {cid}")
        lineage = list(sig.lineage_json or [])
        # Verify lineage resolves (≤2 hops)
        for lid in lineage[:5]:
            r = resolve_citation(session, lid)
            if not r.ok:
                return ResolvedCitation(
                    cid, False, "", error=f"Lineage broken: {lid} — {r.error}"
                )
        summary = f"{metric_id} @ {week} = {sig.value} (Δ {sig.delta_wow})"
        return ResolvedCitation(
            cid,
            True,
            summary,
            record={
                "metric_id": metric_id,
                "week": week,
                "value": sig.value,
                "delta_wow": sig.delta_wow,
                "lineage": lineage,
            },
        )

    return ResolvedCitation(cid, False, "", error=f"Unknown citation kind {kind}")


def _summarize_erp(session: Session, table: str, row: Any) -> tuple[str, dict]:
    data = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    # stringify dates
    for k, v in list(data.items()):
        if hasattr(v, "isoformat"):
            data[k] = v.isoformat()

    if table == "erp_sales_invoices":
        cust = session.get(ErpCustomer, row.customer_id)
        name = cust.name if cust else "?"
        summary = (
            f"{row.business_key} · {name} · {format_inr(row.amount_paise)} · due {row.due_date}"
        )
    elif table == "erp_receipts":
        summary = f"{row.business_key} · receipt {format_inr(row.amount_paise)} · {row.receipt_date}"
    elif table == "erp_cash_daily":
        summary = f"{row.business_key} · cash {format_inr(row.balance_paise)} · {row.as_of_date}"
    elif table == "erp_gst_events":
        summary = (
            f"{row.business_key} · {row.event_type} · {format_inr(row.amount_paise)} · {row.event_date}"
        )
    elif table == "erp_expenses":
        summary = (
            f"{row.business_key} · {row.cost_center} · {format_inr(row.amount_paise)} · {row.expense_date}"
        )
    else:
        summary = f"{row.business_key}"
    return summary, data


def gate_citations(
    session: Session,
    citations: list[str],
) -> tuple[list[str], list[str]]:
    """Return (ok_ids, bad_ids)."""
    ok, bad = [], []
    for cid in citations:
        r = resolve_citation(session, cid)
        (ok if r.ok else bad).append(cid)
    return ok, bad


def collect_item_citations(sections: dict[str, Any]) -> list[str]:
    """Flatten citation lists from synthesize output sections."""
    found: list[str] = []
    for key in ("citations", "what_citations", "why_citations", "connects_citations", "action_citations"):
        val = sections.get(key)
        if isinstance(val, list):
            found.extend(val)
    # nested per-section
    for section in ("what", "why", "connects", "action"):
        block = sections.get(f"{section}_block") or sections.get(section)
        if isinstance(block, dict) and "citations" in block:
            found.extend(block["citations"])
    # dedupe preserve order
    seen = set()
    out = []
    for c in found:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out
