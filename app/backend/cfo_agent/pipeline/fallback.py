"""Deterministic (no-LLM) synthesis used when the LLM budget runs out or when a
red-line item fails the citation gate. Every sentence here is built only from
verified pipeline data, so it always passes the gate."""

from __future__ import annotations

from cfo_agent.pipeline.schemas import SectionBlock, SynthesizeItemOutput
from cfo_agent.pipeline.step4_filter import FilterItem


def allowed_citations_for(item: FilterItem) -> list[str]:
    """The only citation IDs an item's synthesis may use."""
    allowed: list[str] = []
    if item.weighted:
        s = item.weighted.mapped.signal
        allowed.append(s.signal_id)
        allowed.extend(s.lineage_json or [])
    if item.rule_fire:
        evidence = item.rule_fire.evidence or {}
        for ev in evidence.get("events") or []:
            if "citation" in ev:
                allowed.append(ev["citation"])
        allowed.extend(evidence.get("lineage") or [])
    from cfo_agent.drivers import driver_citations

    allowed.extend(driver_citations((item.score_decomp or {}).get("drivers")))
    return list(dict.fromkeys(allowed))


def _driver_sentence(item: FilterItem) -> tuple[str, list[str]]:
    """One factual sentence naming the largest contributor, with its citation."""
    from cfo_agent.timeutil import format_inr

    drivers = (item.score_decomp or {}).get("drivers") or {}
    rows = drivers.get("rows") or []
    if not rows:
        return "", []
    r = rows[0]
    recon = drivers.get("reconciliation") or {}
    if recon.get("not_in_feed_paise") is not None:
        # Cash: the largest single flow is not "the cause"; say what the records
        # net to and how much of the balance change they leave unexplained.
        text = (f" Recorded receipts, payments and expenses net to {format_inr(recon['net_recorded_flow_paise'])}; "
                f"{format_inr(recon['not_in_feed_paise'])} of the {format_inr(recon['balance_change_paise'])} "
                "balance change is not in the feed.")
        return text, [r["citation"]] if r.get("citation") else []
    if r.get("change_paise") is not None:
        text = (f" Largest contributor: {r['label']} ({r['group']}), {format_inr(r['current_paise'])} "
                f"against {format_inr(r['previous_paise'])} before.")
    else:
        text = f" Largest contributor: {r['label']} ({r['group']}), {format_inr(r['current_paise'])}."
    return text, [r["citation"]] if r.get("citation") else []


def _label(metric_id: str | None) -> str:
    """The metric's display name, as the CFO sees it in the app."""
    if not metric_id:
        return "metric"
    from cfo_agent.signals import metric_meta

    return metric_meta(metric_id)["label"]


def _val(metric_id: str | None, v: float | None) -> str:
    """A value with its unit: ₹21.4 Cr, 73.1 days, 35.7%."""
    from cfo_agent.signals import metric_meta

    unit = metric_meta(metric_id)["unit"] if metric_id else ""
    n = _fmt(v)
    if v is None or unit in ("", "count", "score"):
        return n
    if unit == "₹ Cr":
        return f"{'-' if v < 0 else ''}₹{_fmt(abs(v))} Cr"
    if unit == "%":
        return f"{n}%"
    return f"{n} {unit}"


def _signed(metric_id: str | None, v: float) -> str:
    sign = "+" if v >= 0 else "-"
    return f"{sign}{_val(metric_id, abs(v))}"


def _metric_of(item: FilterItem) -> str | None:
    if item.weighted:
        return item.weighted.mapped.signal.metric_id
    return item.rule_fire.metric if item.rule_fire else None


def period_display(item: FilterItem) -> list[dict]:
    """The item's period views with every figure pre-formatted, as the model quotes them."""
    metric = _metric_of(item)
    periods = (item.score_decomp or {}).get("periods") or {}
    flow = periods.get("aggregation") in ("sum", "avg")
    out = []
    for v in periods.get("views") or []:
        row = {
            "period": v["label"],
            "since": v["since"],
            "change_display": _signed(metric, v["change"]),
            "basis": v["basis"],
        }
        if flow and v.get("value") is not None:
            row["value_display"] = _val(metric, v["value"])
        if v.get("change_pct") is not None:
            row["change_pct_display"] = f"{v['change_pct']:+.1f}%"
        out.append(row)
    return out


_SPAN = {
    "Month to date": "the month",
    "Quarter to date": "the quarter",
    "Year to date": "the financial year",
    "Month and quarter to date": "the month and quarter",
}


def _period_sentence(item: FilterItem) -> str:
    """One factual sentence on the longest period that has a comparison."""
    periods = (item.score_decomp or {}).get("periods") or {}
    views = periods.get("views") or []
    if not views:
        return ""
    metric = _metric_of(item)
    pick = next((v for v in reversed(views) if v["label"] != "Vs a year ago"), views[-1])
    change = pick["change"]
    moved = "risen" if change > 0 else "fallen"
    amount = _val(metric, abs(change))
    if pick["label"] == "Vs a year ago":
        return f" It has {moved} {amount} over the year."
    span = _SPAN.get(pick["label"], pick["label"].lower())
    if periods.get("aggregation") == "sum":
        return f" Over {span} so far it totals {_val(metric, pick['value'])}."
    if periods.get("aggregation") == "avg":
        return f" Over {span} so far it averages {_val(metric, pick['value'])}."
    return f" Since the start of {span} it has {moved} {amount}."


def _fmt(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def deterministic_synthesis(item: FilterItem) -> SynthesizeItemOutput:
    allowed = allowed_citations_for(item)
    primary = allowed[:1]
    evidence = allowed[:5]

    if item.rule_fire:
        rf = item.rule_fire
        title = f"Red line: {rf.description}"
        if rf.metric:
            what = (
                f"{_label(rf.metric)} is {_val(rf.metric, rf.value)} against a red-line threshold of "
                f"{_val(rf.metric, rf.threshold)} for {rf.week}."
            )
        else:
            what = f"{int(rf.value or 0)} open event(s) match the red line '{rf.description}' as of {rf.as_of_date}."
        why = "This condition is a declared red line, so it is surfaced regardless of weighting."
        action = "Review the cited evidence and confirm the mitigation owner this week."
    else:
        s = item.weighted.mapped.signal  # type: ignore[union-attr]
        title = f"{_label(s.metric_id)} moved to {_val(s.metric_id, s.value)}"
        delta = f" ({'+' if (s.delta_wow or 0) >= 0 else ''}{_fmt(s.delta_wow)} week on week)" if s.delta_wow is not None else ""
        what = f"{_label(s.metric_id)} is {_val(s.metric_id, s.value)} for {s.week}{delta}."
        why = f"It maps to {', '.join(item.kra_ids) or 'a ranked KRA'} and cleared the surfacing bar under the current weighting."
        action = "Ask the KRA owner to explain the movement using the cited records."

    connects_text = "; ".join(c.narrative for c in item.connections) or "No cross-KRA connection identified."
    driver_text, driver_cites = _driver_sentence(item)
    return SynthesizeItemOutput(
        title=title,
        what=SectionBlock(text=what + driver_text + _period_sentence(item), citations=(primary or evidence) + driver_cites),
        why=SectionBlock(text=why, citations=primary or evidence),
        connects=SectionBlock(text=connects_text, citations=[]),
        action=SectionBlock(text=action, citations=evidence),
    )


def deterministic_headline(week: str, titles: list[str]) -> str:
    if not titles:
        return f"{week}: Nothing met the surfacing bar under current risk appetite."
    lead = titles[0]
    more = f" (+{len(titles) - 1} more)" if len(titles) > 1 else ""
    return f"{week}: {lead}{more}"[:160]
