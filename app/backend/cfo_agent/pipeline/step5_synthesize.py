from __future__ import annotations

import json

from sqlalchemy.orm import Session

from cfo_agent.kra import KraFramework, framework_to_prompt_block
from cfo_agent.llm import LlmBudget, cached_system_block, parse
from cfo_agent.memory.archival import archival_search
from cfo_agent.memory.blocks import render_blocks_for_prompt
from cfo_agent.pipeline.schemas import HeadlineOutput, SynthesizeItemOutput
from cfo_agent.pipeline.step4_filter import FilterItem
from cfo_agent.weighting import weighting_to_prompt_block


def _with_display_amounts(obj):
    """Add a pre-formatted ₹ string next to every *_paise field so the model never
    converts paise itself (it got a ₹3.2 Cr notice wrong by 10× doing so)."""
    from cfo_agent.timeutil import format_inr

    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            out[k] = _with_display_amounts(v)
            if k.endswith("_paise") and isinstance(v, (int, float)):
                out[k.removesuffix("_paise") + "_inr"] = format_inr(int(v))
        return out
    if isinstance(obj, list):
        return [_with_display_amounts(v) for v in obj]
    return obj


def step5_synthesize_item(
    session: Session,
    item: FilterItem,
    framework: KraFramework,
    weighting: dict,
    week: str,
    budget: LlmBudget,
    repair_errors: dict[str, str] | None = None,
) -> SynthesizeItemOutput:
    from cfo_agent.pipeline.fallback import allowed_citations_for

    allowed_cites = allowed_citations_for(item)
    payload: dict = {"week": week, "is_red_line": item.is_red_line}

    if item.weighted:
        s = item.weighted.mapped.signal
        payload["signal"] = {
            "signal_id": s.signal_id,
            "metric_id": s.metric_id,
            "value": s.value,
            "delta_wow": s.delta_wow,
            "kra_ids": item.kra_ids,
            "lineage": s.lineage_json,
            "score_decomp": {k: v for k, v in item.score_decomp.items() if k != "drivers"},
        }
    if item.rule_fire:
        payload["red_line"] = {
            "rule_id": item.rule_fire.rule_id,
            "description": item.rule_fire.description,
            "value": item.rule_fire.value,
            "threshold": item.rule_fire.threshold,
            "evidence": _with_display_amounts(item.rule_fire.evidence),
        }

    drivers = (item.score_decomp or {}).get("drivers")
    if drivers:
        payload["drivers"] = _with_display_amounts(drivers)

    if item.connections:
        payload["connections"] = [c.model_dump() for c in item.connections]

    # Archival retrieval as-of week
    query = payload.get("signal", {}).get("metric_id") or (
        item.rule_fire.description if item.rule_fire else "finance"
    )
    past = archival_search(session, str(query), k=3, as_of_week=week)
    payload["past_insights"] = past

    system = [
        cached_system_block(
            framework_to_prompt_block(framework)
            + "\n\n"
            + weighting_to_prompt_block(weighting)
        ),
        {"type": "text", "text": render_blocks_for_prompt(session, skip_managed=True)},
    ]
    repair_note = ""
    if repair_errors:
        repair_note = (
            "\n\nREPAIR: your previous attempt cited IDs that do not resolve. "
            "Cite ONLY from ALLOWED_CITATIONS this time. Failed citations:\n"
            + json.dumps(repair_errors, indent=2)
        )
    user = (
        "Synthesize one digest item for the CFO. "
        "Every factual claim must cite only from ALLOWED_CITATIONS. "
        "Structure: title, what, why, connects, action — each with citations[]; "
        "'what' and 'why' MUST each carry at least one citation. "
        "Only state figures that appear in ITEM (or in the cited records); do not derive new numbers. "
        "For money, quote the *_inr fields verbatim; never convert *_paise values yourself. "
        "If ITEM.drivers is present it lists the records that actually moved this metric: in 'what', name the "
        "one or two largest by label and amount and cite each row's `citation`; base 'action' on them. "
        "Do not say they explain the whole move unless drivers.reconciliation shows it, and if "
        "`not_in_feed_inr` is material say that part is unexplained by the records. "
        "If there are no drivers, do not speculate about causes. "
        "If past_insights exist, reference continuity briefly.\n\n"
        f"ALLOWED_CITATIONS: {json.dumps(allowed_cites)}\n\n"
        f"ITEM:\n{json.dumps(payload, indent=2, default=str)}"
        f"{repair_note}"
    )
    return parse(
        SynthesizeItemOutput,
        system=system,
        user=user,
        purpose="synthesize_item",
        budget=budget,
        thinking=True,
    )


def step5_headline(
    session: Session,
    titles: list[str],
    weighting: dict,
    week: str,
    budget: LlmBudget,
    empty_bar: bool,
) -> str:
    if empty_bar:
        return f"{week}: Nothing met the surfacing bar under current risk appetite."
    system = weighting_to_prompt_block(weighting)
    user = (
        f"Write one sharp headline (≤160 chars) for the {week} CFO digest summarizing these items:\n"
        + "\n".join(f"- {t}" for t in titles)
    )
    out = parse(
        HeadlineOutput,
        system=system,
        user=user,
        purpose="headline",
        budget=budget,
        thinking=False,
        max_tokens=512,
    )
    return out.headline
