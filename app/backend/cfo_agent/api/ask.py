"""The Ask box API: question a whole briefing. Personal to the signed-in user."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from cfo_agent.api.chat import stream_response
from cfo_agent.ask import ask_history, clear_history, message_dict, resolve_digest, run_ask
from cfo_agent.db import get_session
from cfo_agent.scheduler import note_activity

router = APIRouter()
logger = logging.getLogger(__name__)


class AskBody(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    digest_id: int | None = None  # default: the latest briefing


class ClearBody(BaseModel):
    digest_id: int | None = None


def _email(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return getattr(user, "email", None) or "unknown"


@router.get("/ask")
def get_ask(request: Request, digest_id: int | None = None):
    session = get_session()
    try:
        digest = resolve_digest(session, digest_id)
        if digest is None:
            return {"digest_id": None, "week": None, "headline": None, "messages": []}
        return {
            "digest_id": digest.id,
            "week": digest.week,
            "headline": digest.headline,
            "messages": [message_dict(m) for m in ask_history(session, _email(request), digest.id)],
        }
    finally:
        session.close()


@router.post("/ask")
def post_ask(request: Request, body: AskBody):
    session = get_session()
    try:
        msg = run_ask(session, _email(request), body.digest_id, body.message)
        session.commit()
        note_activity()
        return message_dict(msg)
    except ValueError as e:
        session.rollback()
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        session.rollback()
        logger.exception("Ask failed")
        raise HTTPException(500, "The assistant could not answer just now. Please try again.") from e
    finally:
        session.close()


@router.post("/ask/stream")
def stream_ask(request: Request, body: AskBody):
    """Server-sent events: tool / reset / delta / done / error."""
    check = get_session()
    try:
        if resolve_digest(check, body.digest_id) is None:
            raise HTTPException(404, "There is no briefing to ask about yet.")
    finally:
        check.close()
    email = _email(request)

    def run(emit) -> None:
        session = get_session()
        try:
            run_ask(session, email, body.digest_id, body.message, emit=emit)
            session.commit()
            note_activity()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return stream_response(run, "Ask")


@router.post("/ask/clear")
def clear_ask(request: Request, body: ClearBody):
    """Start over: delete the signed-in user's own questions and answers for a briefing."""
    session = get_session()
    try:
        digest = resolve_digest(session, body.digest_id)
        n = clear_history(session, _email(request), digest.id if digest else None)
        session.commit()
        return {"cleared": n}
    finally:
        session.close()
