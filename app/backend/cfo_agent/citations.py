"""Citation scheme, resolver, cite-or-suppress gate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from cfo_agent.models import (
    ErpBankAccount,
    ErpCashDaily,
    ErpCreditNote,
    ErpEntity,
    ErpFxRate,
    ErpDebtBalance,
    ErpGlEntry,
    ErpInventoryBalance,
    ErpPlanMetric,
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
    "erp_credit_notes": ErpCreditNote,
    "erp_gl_entries": ErpGlEntry,
    "erp_inventory": ErpInventoryBalance,
    "erp_debt": ErpDebtBalance,
    "erp_bank_accounts": ErpBankAccount,
    "erp_fx_rates": ErpFxRate,
    "erp_entities": ErpEntity,
    "erp_plan_metrics": ErpPlanMetric,
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
        from cfo_agent.signals import metric_meta

        label = metric_meta(metric_id)["label"]
        delta = "" if sig.delta_wow is None else f" (Δ {sig.delta_wow:+.2f})"
        summary = f"{label} · {week} = {sig.value:,.2f}{delta}"
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
    elif table == "erp_credit_notes":
        summary = f"{row.business_key} · credit note {format_inr(row.amount_paise)} · {row.note_date}"
    elif table == "erp_gl_entries":
        summary = (f"{row.business_key} · {row.account_code} {row.account_name} · "
                   f"{format_inr(row.amount_paise)} · posted {row.posting_date} for {row.period_end}")
    elif table == "erp_inventory":
        summary = f"{row.business_key} · {row.category} inventory {format_inr(row.value_paise)} · {row.as_of_date}"
    elif table == "erp_debt":
        summary = (f"{row.business_key} · {row.facility} ({row.lender}) outstanding "
                   f"{format_inr(row.outstanding_paise)} at {row.interest_rate_pct:g}% · {row.as_of_date}")
    elif table == "erp_plan_metrics":
        summary = f"{row.business_key} · plan {row.metric_id} {row.value:g} · {row.week}"
    elif table == "erp_covenants":
        summary = f"{row.business_key} · {row.name} · threshold {format_inr(row.threshold_paise)}"
    elif table == "erp_purchase_invoices":
        summary = f"{row.business_key} · {row.vendor_name} · {format_inr(row.amount_paise)} · due {row.due_date}"
    else:
        summary = f"{row.business_key}"
    if getattr(row, "currency", "INR") not in (None, "INR"):
        summary += f" · {row.currency} @ {row.fx_rate:g}"
    return summary, data


def gate_citations(
    session: Session,
    citations: list[str],
    allowed: set[str] | list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Return (ok_ids, bad_ids). With `allowed`, IDs outside the set are bad even if they resolve."""
    ok, bad = [], []
    allowed_set = set(allowed) if allowed is not None else None
    for cid in citations:
        if allowed_set is not None and cid not in allowed_set:
            bad.append(cid)
            continue
        r = resolve_citation(session, cid)
        (ok if r.ok else bad).append(cid)
    return ok, bad


def citation_error(session: Session, cid: str, allowed: set[str] | list[str] | None = None) -> str:
    if allowed is not None and cid not in set(allowed):
        return "Not in ALLOWED_CITATIONS for this item"
    return resolve_citation(session, cid).error or "unresolvable"


# ── Figure verification ──────────────────────────────────────────────────────
import re  # noqa: E402

_NUM_RE = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")
_SKIP_RE = re.compile(r"\b\d{4}-W\d{1,2}\b|\bW\d{1,2}\b|\b\d{4}-\d{2}-\d{2}\b|\b[A-Z]{2,}-\d+\b")


def _numeric_values(obj: Any, out: list[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.append(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _numeric_values(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _numeric_values(v, out)


def known_figures(values: list[float]) -> list[float]:
    """Expand raw values into the forms a narrative may use (paise→Cr/L/₹, abs)."""
    out: list[float] = []
    for v in values:
        out.extend([v, abs(v)])
        if abs(v) >= 10_000:  # plausibly paise
            out.extend([abs(v) / 1e9, abs(v) / 1e7, abs(v) / 100])
    return out


def unverified_figures(texts: list[str], known: list[float], *, rel_tol: float = 0.02) -> list[str]:
    """Numbers in `texts` that match no known figure (within rounding). Years, dates,
    week IDs, document keys and integers ≤ 10 are ignored."""
    flagged: list[str] = []
    for text in texts:
        cleaned = _SKIP_RE.sub(" ", text or "")
        for m in _NUM_RE.finditer(cleaned):
            raw = m.group(0)
            try:
                n = abs(float(raw.replace(",", "")))
            except ValueError:
                continue
            if "." not in raw and (n <= 10 or 1990 <= n <= 2100):
                continue
            decimals = len(raw.split(".")[1]) if "." in raw else 0
            round_tol = 0.5 * 10 ** (-decimals)
            if not any(abs(n - k) <= max(round_tol, rel_tol * abs(k)) for k in known):
                flagged.append(raw)
    return list(dict.fromkeys(flagged))


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
