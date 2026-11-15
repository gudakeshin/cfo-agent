from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from cfo_agent.db import get_session
from cfo_agent.models import AuditEvent, Digest, DigestItem, Feedback
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


@router.get("/feedback/scorecard")
def pilot_scorecard(weeks: int = Query(13, ge=1, le=52)):
    """Is the pilot working? Per week: what was surfaced, whether the briefing was
    opened, and what the CFO did with each insight. The success test is at least one
    insight in the period that was acted on and not already known."""
    from cfo_agent.settings import get_settings

    session = get_session()
    try:
        # Latest digest per week (a week can be re-run under another weighting)
        latest: dict[str, Digest] = {}
        for d in session.query(Digest).order_by(Digest.created_at.asc(), Digest.id.asc()):
            latest[d.week] = d
        digests = sorted(latest.values(), key=lambda d: d.week)[-weeks:]
        if not digests:
            return {"weeks": [], "totals": None}

        ids = [d.id for d in digests]
        items = session.query(DigestItem).filter(DigestItem.digest_id.in_(ids)).all()
        fb = {
            f.digest_item_id: f
            for f in session.query(Feedback).filter(Feedback.digest_item_id.in_([i.id for i in items] or [0]))
        }

        # "Opened" = the digest (or the briefing while it was the latest) was viewed.
        # With sign-in on, only views by someone in the CFO role count.
        q = session.query(AuditEvent.at, AuditEvent.path).filter(
            AuditEvent.action == "view", AuditEvent.at >= digests[0].created_at
        )
        if get_settings().auth_mode != "off":
            q = q.filter(AuditEvent.role == "cfo")
        views = q.all()

        rows = []
        for idx, d in enumerate(digests):
            mine = [i for i in items if i.digest_id == d.id]
            verdicts = [fb[i.id] for i in mine if i.id in fb]
            until = digests[idx + 1].created_at if idx + 1 < len(digests) else None
            opens = sum(
                1
                for at, path in views
                if path == f"/api/digests/{d.id}"
                or (path == "/api/briefing" and at >= d.created_at and (until is None or at < until))
            )
            rows.append({
                "week": d.week,
                "digest_id": d.id,
                "surfaced": len(mine),
                "red_lines": sum(1 for i in mine if i.is_red_line),
                "reviewed": len(verdicts),
                "acted_on": sum(1 for f in verdicts if f.verdict == "acted_on"),
                "useful": sum(1 for f in verdicts if f.verdict == "useful"),
                "set_aside": sum(1 for f in verdicts if f.verdict == "set_aside"),
                "already_knew": sum(1 for f in verdicts if f.already_knew),
                "novel_acted_on": sum(1 for f in verdicts if f.verdict == "acted_on" and not f.already_knew),
                "opens": opens,
            })

        def total(key: str) -> int:
            return sum(r[key] for r in rows)

        surfaced, reviewed = total("surfaced"), total("reviewed")
        return {
            "weeks": rows,
            "totals": {
                "digests": len(rows),
                "weeks_opened": sum(1 for r in rows if r["opens"]),
                "surfaced": surfaced,
                "reviewed": reviewed,
                "review_rate_pct": round(100.0 * reviewed / surfaced, 1) if surfaced else None,
                "acted_on": total("acted_on"),
                "useful": total("useful"),
                "set_aside": total("set_aside"),
                "already_knew": total("already_knew"),
                "novel_acted_on": total("novel_acted_on"),
                # Share of reviewed insights the CFO found worth having
                "useful_rate_pct": round(100.0 * (total("acted_on") + total("useful")) / reviewed, 1) if reviewed else None,
                "success_test_met": total("novel_acted_on") >= 1,
            },
        }
    finally:
        session.close()
