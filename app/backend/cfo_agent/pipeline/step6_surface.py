from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from cfo_agent.citations import gate_citations, resolve_citation
from cfo_agent.pipeline.schemas import SynthesizeItemOutput
from cfo_agent.pipeline.step4_filter import FilterItem


@dataclass
class SurfacedItem:
    filter_item: FilterItem
    synthesis: SynthesizeItemOutput
    citations: dict[str, list[str]]
    all_citation_ids: list[str]


@dataclass
class SuppressedItem:
    reason: str
    title: str
    bad_citations: list[str]
    detail: str


def _section_cites(block) -> list[str]:
    return list(block.citations or [])


def step6_surface(
    session: Session,
    pairs: list[tuple[FilterItem, SynthesizeItemOutput]],
    *,
    repair_fn=None,
) -> tuple[list[SurfacedItem], list[SuppressedItem]]:
    """Citation gate — suppress uncitable items. Optional one repair retry via repair_fn."""
    surfaced: list[SurfacedItem] = []
    suppressed: list[SuppressedItem] = []

    for item, syn in pairs:
        cites = {
            "what": _section_cites(syn.what),
            "why": _section_cites(syn.why),
            "connects": _section_cites(syn.connects),
            "action": _section_cites(syn.action),
        }
        all_ids = []
        for v in cites.values():
            all_ids.extend(v)
        all_ids = list(dict.fromkeys(all_ids))

        ok, bad = gate_citations(session, all_ids)
        if bad and repair_fn is not None:
            errors = {cid: resolve_citation(session, cid).error for cid in bad}
            syn = repair_fn(item, syn, errors)
            cites = {
                "what": _section_cites(syn.what),
                "why": _section_cites(syn.why),
                "connects": _section_cites(syn.connects),
                "action": _section_cites(syn.action),
            }
            all_ids = []
            for v in cites.values():
                all_ids.extend(v)
            all_ids = list(dict.fromkeys(all_ids))
            ok, bad = gate_citations(session, all_ids)

        if bad or not all_ids:
            suppressed.append(
                SuppressedItem(
                    reason="uncitable" if bad else "no_citations",
                    title=syn.title,
                    bad_citations=bad,
                    detail=f"Failed citations: {bad}" if bad else "No citations provided",
                )
            )
            continue

        surfaced.append(
            SurfacedItem(
                filter_item=item,
                synthesis=syn,
                citations=cites,
                all_citation_ids=ok,
            )
        )

    # Order: red-lines first then score
    surfaced.sort(
        key=lambda s: (
            0 if s.filter_item.is_red_line else 1,
            -s.filter_item.priority_score,
        )
    )
    return surfaced, suppressed
