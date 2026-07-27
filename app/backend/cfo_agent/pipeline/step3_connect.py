from __future__ import annotations

import json

from sqlalchemy.orm import Session

from cfo_agent.kra import KraFramework, framework_to_prompt_block
from cfo_agent.llm import LlmBudget, cached_system_block, parse
from cfo_agent.memory.blocks import render_blocks_for_prompt
from cfo_agent.pipeline.schemas import ConnectOutput
from cfo_agent.pipeline.step2_weight import WeightedSignal
from cfo_agent.weighting import weighting_to_prompt_block


def step3_connect(
    session: Session,
    top: list[WeightedSignal],
    framework: KraFramework,
    weighting: dict,
    budget: LlmBudget,
) -> ConnectOutput:
    signal_payload = []
    allowed_ids = set()
    for w in top:
        s = w.mapped.signal
        allowed_ids.add(s.signal_id)
        signal_payload.append(
            {
                "signal_id": s.signal_id,
                "metric_id": s.metric_id,
                "value": s.value,
                "delta_wow": s.delta_wow,
                "severity": s.severity,
                "kra_ids": w.mapped.kra_ids,
                "priority_score": w.priority_score,
                "lineage": s.lineage_json[:10],
            }
        )
    rels = [
        {"id": r.id, "from": r.from_kra, "to": r.to_kra, "description": r.description}
        for r in framework.relationships
    ]
    allowed_rels = {r["id"] for r in rels}

    system = [
        cached_system_block(
            framework_to_prompt_block(framework)
            + "\n\n"
            + weighting_to_prompt_block(weighting)
        ),
        {
            "type": "text",
            "text": render_blocks_for_prompt(session),
        },
    ]
    user = (
        "Connect cross-KRA threads among these top signals. "
        "You may ONLY reference the provided signal_ids and declared relationship ids. "
        "Prefer declared relationships when they apply (especially REL-1 for DSO→acquisition).\n\n"
        f"Signals:\n{json.dumps(signal_payload, indent=2)}\n\n"
        f"Relationships:\n{json.dumps(rels, indent=2)}"
    )
    result = parse(
        ConnectOutput,
        system=system,
        user=user,
        purpose="connect",
        budget=budget,
        thinking=True,
    )
    # Post-hoc validation
    cleaned = []
    for c in result.connections:
        sids = [s for s in c.signal_ids if s in allowed_ids]
        rids = [r for r in c.relationship_ids if r in allowed_rels]
        if sids:
            c.signal_ids = sids
            c.relationship_ids = rids
            cleaned.append(c)
    return ConnectOutput(connections=cleaned)
