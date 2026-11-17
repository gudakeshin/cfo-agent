"""Audit trail writer. Failures here never break the request being audited."""

from __future__ import annotations

import logging
import re

from fastapi import Request

from cfo_agent.db import get_session
from cfo_agent.models import AuditEvent

logger = logging.getLogger(__name__)

# GETs worth recording as "views": a digest, the briefing, an insight conversation, evidence.
VIEW_PATHS = re.compile(r"^/api/(digests/\d+(/export\.(pdf|pptx))?|briefing|chat/\d+|citations/.+|audit|briefs/\d+)$")


def classify(method: str, path: str, status: int) -> str | None:
    if not path.startswith("/api/") or path.startswith("/api/auth/"):
        return None
    if status == 403:
        return "denied"  # signed in but not allowed (401s are just "not signed in yet")
    if status == 401:
        return None
    if method in ("POST", "PUT", "DELETE", "PATCH"):
        return "change"
    if method == "GET" and status < 400 and VIEW_PATHS.match(path):
        return "view"
    return None


def record_event(request: Request, *, action: str, status: int, user=None) -> None:
    u = user or getattr(request.state, "user", None)
    session = get_session()
    try:
        session.add(
            AuditEvent(
                user_email=getattr(u, "email", None) or "anonymous",
                role=getattr(u, "role", None) or "-",
                action=action,
                method=request.method,
                path=str(request.url.path)[:512],
                status=status,
                ip=request.client.host if request.client else None,
                request_id=getattr(request.state, "request_id", None),
            )
        )
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.exception("Could not write audit event")
    finally:
        session.close()
