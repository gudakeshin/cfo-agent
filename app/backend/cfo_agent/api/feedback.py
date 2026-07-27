from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from cfo_agent.db import get_session
from cfo_agent.models import DigestItem, Feedback
from cfo_agent.scheduler import note_activity

router = APIRouter()


class FeedbackBody(BaseModel):
    digest_item_id: int
    verdict: str  # acted_on|useful|set_aside
    note: str = ""
    already_knew: bool = False


@router.post("/feedback")
def upsert_feedback(body: FeedbackBody):
    if body.verdict not in {"acted_on", "useful", "set_aside"}:
        raise HTTPException(422, "Invalid verdict")
    session = get_session()
    try:
        item = session.get(DigestItem, body.digest_item_id)
        if not item:
            raise HTTPException(404, "Item not found")
        row = session.query(Feedback).filter_by(digest_item_id=body.digest_item_id).one_or_none()
        if row:
            row.verdict = body.verdict
            row.note = body.note
            row.already_knew = body.already_knew
            row.updated_at = datetime.utcnow()
        else:
            row = Feedback(
                digest_item_id=body.digest_item_id,
                verdict=body.verdict,
                note=body.note,
                already_knew=body.already_knew,
            )
            session.add(row)
        session.commit()
        note_activity()
        return {
            "id": row.id,
            "digest_item_id": row.digest_item_id,
            "verdict": row.verdict,
            "note": row.note,
            "already_knew": row.already_knew,
        }
    finally:
        session.close()


@router.get("/feedback")
def list_feedback():
    session = get_session()
    try:
        rows = session.query(Feedback).order_by(Feedback.updated_at.desc()).all()
        out = []
        counts = {"acted_on": 0, "useful": 0, "set_aside": 0}
        g1 = 0  # acted_on and not already_knew
        for r in rows:
            counts[r.verdict] = counts.get(r.verdict, 0) + 1
            if r.verdict == "acted_on" and not r.already_knew:
                g1 += 1
            item = session.get(DigestItem, r.digest_item_id)
            out.append(
                {
                    "id": r.id,
                    "digest_item_id": r.digest_item_id,
                    "title": item.title if item else None,
                    "verdict": r.verdict,
                    "note": r.note,
                    "already_knew": r.already_knew,
                    "updated_at": r.updated_at,
                }
            )
        total_scored = sum(counts.values()) or 1
        precision = 100.0 * (counts["acted_on"] + counts["useful"]) / total_scored
        return {
            "items": out,
            "counts": counts,
            "surfacing_precision_pct": round(precision, 1),
            "g1_novel_actionable": g1,
        }
    finally:
        session.close()
