from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from cfo_agent.citations import (
    citation_error,
    gate_citations,
    known_figures,
    resolve_citation,
    unverified_figures,
    _numeric_values,
)
from cfo_agent.pipeline.fallback import allowed_citations_for, deterministic_synthesis
from cfo_agent.pipeline.schemas import SynthesizeItemOutput
from cfo_agent.pipeline.step4_filter import FilterItem

# Sections that state facts and therefore must carry ≥1 citation.
FACTUAL_SECTIONS = ("what", "why")


@dataclass
class SurfacedItem:
    filter_item: FilterItem
    synthesis: SynthesizeItemOutput
    citations: dict[str, list[str]]
    all_citation_ids: list[str]
    # citation_id -> {"summary", "record"} frozen at generation time
    evidence_snapshot: dict[str, dict] = field(default_factory=dict)
    # {"unverified_figures": [...], "fallback": bool, "notes": [...]}
    verification: dict = field(default_factory=dict)


@dataclass
class SuppressedItem:
    reason: str
    title: str
    bad_citations: list[str]
    detail: str


def _section_cites(syn: SynthesizeItemOutput) -> dict[str, list[str]]:
    return {
        "what": list(syn.what.citations or []),
        "why": list(syn.why.citations or []),
        "connects": list(syn.connects.citations or []),
        "action": list(syn.action.citations or []),
    }


def _gate(session: Session, syn: SynthesizeItemOutput, allowed: list[str]):
    cites = _section_cites(syn)
    all_ids = list(dict.fromkeys(c for v in cites.values() for c in v))
    ok, bad = gate_citations(session, all_ids, allowed=allowed)
    missing = [s for s in FACTUAL_SECTIONS if not cites[s]]
    errors = {cid: citation_error(session, cid, allowed) for cid in bad}
    for s in missing:
        errors[f"<section:{s}>"] = "Section has no citation — every factual section must cite"
    return cites, ok, bad, missing, errors


def _known_values(item: FilterItem, snapshot: dict[str, dict]) -> list[float]:
    vals: list[float] = []
    if item.weighted:
        s = item.weighted.mapped.signal
        vals.extend(v for v in (s.value, s.delta_wow, s.baseline) if v is not None)
        if s.baseline:
            if s.delta_wow is not None:
                vals.append(100.0 * s.delta_wow / s.baseline)
    if item.rule_fire:
        rf = item.rule_fire
        vals.extend(v for v in (rf.value, rf.threshold) if v is not None)
        if rf.value is not None and rf.threshold is not None:
            vals.append(rf.value - rf.threshold)
        _numeric_values(rf.evidence, vals)
    _numeric_values(item.score_decomp, vals)
    for ent in snapshot.values():
        _numeric_values(ent.get("record"), vals)
    return known_figures(vals)


def _snapshot(session: Session, ids: list[str]) -> dict[str, dict]:
    snap: dict[str, dict] = {}
    for cid in ids:
        r = resolve_citation(session, cid)
        if r.ok:
            snap[cid] = {"summary": r.summary, "record": r.record}
    return snap


def step6_surface(
    session: Session,
    pairs: list[tuple[FilterItem, SynthesizeItemOutput]],
    *,
    repair_fn=None,
) -> tuple[list[SurfacedItem], list[SuppressedItem]]:
    """Citation gate.

    An item surfaces only if every citation resolves AND is in the item's
    allowed set, and each factual section (what/why) cites something. One repair
    retry via repair_fn. Red lines are never suppressed: if they still fail, a
    deterministic, fully-cited synthesis replaces the LLM text.
    Figures in the text that match no cited value are flagged (not suppressed)."""
    surfaced: list[SurfacedItem] = []
    suppressed: list[SuppressedItem] = []

    for item, syn in pairs:
        allowed = allowed_citations_for(item)
        cites, ok, bad, missing, errors = _gate(session, syn, allowed)
        if (bad or missing) and repair_fn is not None:
            syn = repair_fn(item, syn, errors)
            cites, ok, bad, missing, errors = _gate(session, syn, allowed)

        notes: list[str] = []
        fallback = False
        if bad or missing or not ok:
            if item.is_red_line:
                notes.append(f"LLM synthesis failed the gate ({errors}); deterministic text used")
                syn = deterministic_synthesis(item)
                cites, ok, bad, missing, errors = _gate(session, syn, allowed)
                fallback = True
            # A red line with no resolvable evidence at all cannot be cited; it is
            # still delivered through the alert channel.
            if bad or missing or not ok:
                suppressed.append(
                    SuppressedItem(
                        reason="uncitable" if bad else "no_citations",
                        title=syn.title,
                        bad_citations=bad,
                        detail="; ".join(f"{k}: {v}" for k, v in errors.items()) or "No citations provided",
                    )
                )
                continue

        snapshot = _snapshot(session, ok)
        texts = [syn.title, syn.what.text, syn.why.text, syn.connects.text, syn.action.text]
        flagged = unverified_figures(texts, _known_values(item, snapshot))
        surfaced.append(
            SurfacedItem(
                filter_item=item,
                synthesis=syn,
                citations=cites,
                all_citation_ids=ok,
                evidence_snapshot=snapshot,
                verification={
                    "unverified_figures": flagged,
                    "fallback": fallback,
                    "notes": notes,
                },
            )
        )

    surfaced.sort(
        key=lambda s: (
            0 if s.filter_item.is_red_line else 1,
            -s.filter_item.priority_score,
        )
    )
    return surfaced, suppressed
