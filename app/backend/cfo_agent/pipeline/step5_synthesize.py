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


def step5_synthesize_item(
    session: Session,
    item: FilterItem,
    framework: KraFramework,
    weighting: dict,
    week: str,
    budget: LlmBudget,
    repair_errors: dict[str, str] | None = None,
) -> SynthesizeItemOutput:
    allowed_cites: list[str] = []
    payload: dict = {"week": week, "is_red_line": item.is_red_line}

    if item.weighted:
        s = item.weighted.mapped.signal
        allowed_cites.append(s.signal_id)
        allowed_cites.extend(s.lineage_json or [])
        payload["signal"] = {
            "signal_id": s.signal_id,
            "metric_id": s.metric_id,
            "value": s.value,
            "delta_wow": s.delta_wow,
            "kra_ids": item.kra_ids,
            "lineage": s.lineage_json,
            "score_decomp": item.score_decomp,
        }
    if item.rule_fire:
        payload["red_line"] = {
            "rule_id": item.rule_fire.rule_id,
            "description": item.rule_fire.description,
            "value": item.rule_fire.value,
            "threshold": item.rule_fire.threshold,
            "evidence": item.rule_fire.evidence,
        }
        for ev in (item.rule_fire.evidence or {}).get("events") or []:
            if "citation" in ev:
                allowed_cites.append(ev["citation"])
        for lid in (item.rule_fire.evidence or {}).get("lineage") or []:
            allowed_cites.append(lid)

    if item.connections:
        payload["connections"] = [c.model_dump() for c in item.connections]

    # Archival retrieval as-of week
    query = payload.get("signal", {}).get("metric_id") or (
        item.rule_fire.description if item.rule_fire else "finance"
    )
    past = archival_search(session, str(query), k=3, as_of_week=week)
    payload["past_insights"] = past

    allowed_cites = list(dict.fromkeys(allowed_cites))
    system = [
        cached_system_block(
            framework_to_prompt_block(framework)
            + "\n\n"
            + weighting_to_prompt_block(weighting)
        ),
        {"type": "text", "text": render_blocks_for_prompt(session)},
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
        "Structure: title, what, why, connects, action — each with citations[]. "
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
