"""Learn KRA emphasis from the CFO's feedback, within guardrails, for a delegate
to approve.

Each feedback verdict scores the insight's primary KRA:
    acted_on +1   useful +0.5   set_aside −1   (already_knew −0.25 on top)
Over the last LOOKBACK_WEEKS of digests, a KRA with at least MIN_SAMPLES verdicts
gets a target multiplier 1 + 0.25 × mean score, clipped to [FLOOR, CEILING]. Each
proposal moves at most MAX_STEP from the multiplier in force, so emphasis drifts
gradually and stays explainable. Red lines are never affected (they bypass scoring).
Nothing changes until a delegate approves the proposal, which creates an ordinary
weighting version (so it is versioned, auditable and reversible).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from cfo_agent.models import Digest, DigestItem, Feedback, WeightingProposal
from cfo_agent.weighting import create_weighting_version, get_active_weighting

VERDICT_SCORE = {"acted_on": 1.0, "useful": 0.5, "set_aside": -1.0}
ALREADY_KNEW_PENALTY = -0.25
LOOKBACK_WEEKS = 12
MIN_SAMPLES = 3
FLOOR, CEILING = 0.8, 1.25
MAX_STEP = 0.1
MIN_CHANGE = 0.03


def feedback_evidence(session: Session) -> dict[str, dict[str, Any]]:
    weeks = [w for (w,) in session.query(Digest.week).distinct().order_by(Digest.week.desc()).limit(LOOKBACK_WEEKS)]
    if not weeks:
        return {}
    rows = (
        session.query(Feedback, DigestItem)
        .join(DigestItem, DigestItem.id == Feedback.digest_item_id)
        .join(Digest, Digest.id == DigestItem.digest_id)
        .filter(Digest.week.in_(weeks), DigestItem.is_red_line.is_(False))
        .all()
    )
    ev: dict[str, dict[str, Any]] = defaultdict(lambda: {"n": 0, "acted_on": 0, "useful": 0, "set_aside": 0,
                                                         "already_knew": 0, "score_sum": 0.0, "items": []})
    for fb, item in rows:
        kra = (item.score_decomp_json or {}).get("primary_kra") or (item.kra_ids_json or [None])[0]
        if not kra or fb.verdict not in VERDICT_SCORE:
            continue
        e = ev[kra]
        e["n"] += 1
        e[fb.verdict] += 1
        e["already_knew"] += int(bool(fb.already_knew))
        e["score_sum"] += VERDICT_SCORE[fb.verdict] + (ALREADY_KNEW_PENALTY if fb.already_knew else 0.0)
        if len(e["items"]) < 5:
            e["items"].append({"digest_item_id": item.id, "title": item.title, "verdict": fb.verdict})
    for e in ev.values():
        e["mean"] = round(e["score_sum"] / e["n"], 3) if e["n"] else 0.0
    return dict(ev)


def propose(session: Session) -> WeightingProposal | None:
    """Create a pending proposal if feedback justifies a change; else None."""
    active = get_active_weighting(session)
    current = {k: float(v) for k, v in (active.content_json.get("kra_multipliers") or {}).items()}
    ev = feedback_evidence(session)
    changes: dict[str, float] = {}
    for kra, e in ev.items():
        if e["n"] < MIN_SAMPLES:
            continue
        now = current.get(kra, 1.0)
        target = min(max(1.0 + 0.25 * e["mean"], FLOOR), CEILING)
        step = max(-MAX_STEP, min(MAX_STEP, target - now))
        new = round(now + step, 3)
        if abs(new - now) >= MIN_CHANGE:
            changes[kra] = new
            e["from"], e["to"], e["target"] = now, new, round(target, 3)
    if not changes:
        return None
    for old in session.query(WeightingProposal).filter_by(status="pending").all():
        old.status = "superseded"
    p = WeightingProposal(
        base_weighting_id=active.id,
        multipliers_json=changes,
        evidence_json={k: v for k, v in ev.items() if k in changes},
    )
    session.add(p)
    session.flush()
    return p


def approve(session: Session, proposal: WeightingProposal, *, user: str):
    if proposal.status != "pending":
        raise ValueError(f"Proposal is {proposal.status}")
    active = get_active_weighting(session)
    content = dict(active.content_json or {})
    mults = dict(content.get("kra_multipliers") or {})
    mults.update(proposal.multipliers_json)
    content["kra_multipliers"] = {k: v for k, v in mults.items() if abs(float(v) - 1.0) > 1e-9}
    base_label = str(content.get("label", "Weighting")).split(" (learned")[0]
    content["label"] = f"{base_label} (learned from feedback #{proposal.id})"
    row = create_weighting_version(session, content, activate=True)
    proposal.status = "approved"
    proposal.decided_by = user
    proposal.decided_at = datetime.utcnow()
    proposal.applied_weighting_id = row.id
    session.flush()
    try:
        from cfo_agent.memory.blocks import set_block_content
        from cfo_agent.weighting import weighting_to_prompt_block

        set_block_content(session, "priority_weighting", weighting_to_prompt_block(content), edited_by="human",
                          reason=f"Approved learned multipliers #{proposal.id}", enforce_agent_rules=False)
    except Exception:  # noqa: BLE001
        pass
    return row


def reject(session: Session, proposal: WeightingProposal, *, user: str) -> None:
    if proposal.status != "pending":
        raise ValueError(f"Proposal is {proposal.status}")
    proposal.status = "rejected"
    proposal.decided_by = user
    proposal.decided_at = datetime.utcnow()
    session.flush()
