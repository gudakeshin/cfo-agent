from __future__ import annotations

from fastapi import APIRouter, Query

from cfo_agent.db import get_session
from cfo_agent.models import AuditEvent

router = APIRouter()


@router.get("/audit")
def list_audit(
    action: str | None = None,
    user: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
):
    session = get_session()
    try:
        q = session.query(AuditEvent)
        if action:
            q = q.filter(AuditEvent.action == action)
        if user:
            q = q.filter(AuditEvent.user_email.contains(user.lower()))
        rows = q.order_by(AuditEvent.id.desc()).limit(limit).all()
        return [
            {
                "id": r.id,
                "at": r.at,
                "user": r.user_email,
                "role": r.role,
                "action": r.action,
                "method": r.method,
                "path": r.path,
                "status": r.status,
                "ip": r.ip,
                "request_id": r.request_id,
            }
            for r in rows
        ]
    finally:
        session.close()
