from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from cfo_agent.db import get_session
from cfo_agent.models import Alert
from cfo_agent.scheduler import job_redline_sweep
from cfo_agent.timeutil import week_monday, week_sunday

router = APIRouter()


class CheckBody(BaseModel):
    as_of: str | None = None  # ISO date or week id like 2026-W28 or "2026-W28 Wednesday"


def _parse_as_of(s: str | None) -> date | None:
    if not s:
        return None
    if "-W" in s and " " in s:
        # "2026-W28 Wednesday"
        week, dayname = s.split(" ", 1)
        monday = week_monday(week)
        offset = {
            "monday": 0,
            "tuesday": 1,
            "wednesday": 2,
            "thursday": 3,
            "friday": 4,
            "saturday": 5,
            "sunday": 6,
        }.get(dayname.strip().lower(), 2)
        from datetime import timedelta

        return monday + timedelta(days=offset)
    if "-W" in s:
        return week_sunday(s)
    return date.fromisoformat(s)


@router.get("/alerts")
def list_alerts(active: bool = False):
    session = get_session()
    try:
        q = session.query(Alert).order_by(Alert.created_at.desc())
        if active:
            q = q.filter(Alert.status.in_(["active", "acked"]))
        rows = q.all()
        return [
            {
                "id": a.id,
                "rule_id": a.rule_id,
                "title": a.title,
                "description": a.description,
                "week": a.week,
                "as_of_date": a.as_of_date.isoformat(),
                "status": a.status,
                "evidence": a.evidence_json,
                "synthesis": a.synthesis_json,
                "email_status": a.email_status,
                "email_path": a.email_path,
                "created_at": a.created_at,
            }
            for a in rows
        ]
    finally:
        session.close()


@router.post("/alerts/check")
def check_alerts(body: CheckBody | None = None):
    as_of = _parse_as_of(body.as_of if body else None)
    ids = job_redline_sweep(as_of=as_of)
    return {"fired_alert_ids": ids}


@router.post("/alerts/{alert_id}/ack")
def ack_alert(alert_id: int):
    session = get_session()
    try:
        a = session.get(Alert, alert_id)
        if not a:
            raise HTTPException(404)
        a.status = "acked"
        session.commit()
        return {"id": a.id, "status": a.status}
    finally:
        session.close()
