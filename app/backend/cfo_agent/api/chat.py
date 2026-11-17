from __future__ import annotations

import json
import logging
import queue
import threading

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from cfo_agent.chat import get_chat_history, message_to_dict, run_chat
from cfo_agent.db import get_session
from cfo_agent.models import DigestItem
from cfo_agent.scheduler import note_activity

router = APIRouter()
logger = logging.getLogger(__name__)


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


@router.post("/chat/{item_id}")
def post_chat(item_id: int, body: ChatBody):
    session = get_session()
    try:
        msg = run_chat(session, item_id, body.message)
        session.commit()
        note_activity()
        return message_to_dict(msg)
    except ValueError as e:
        session.rollback()
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        session.rollback()
        logger.exception("Chat failed for item %s", item_id)
        raise HTTPException(500, "The assistant could not answer just now. Please try again.") from e
    finally:
        session.close()


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def stream_response(run, what: str) -> StreamingResponse:
    """Run `run(emit)` on a worker thread and relay its events as server-sent events.
    An exception becomes an `error` event; the stream always ends."""
    q: queue.Queue[tuple[str, dict] | None] = queue.Queue()

    def worker() -> None:
        try:
            run(lambda e, d: q.put((e, d)))
        except Exception:  # noqa: BLE001
            logger.exception("Streaming %s failed", what)
            q.put(("error", {"detail": "The assistant could not answer just now. Please try again."}))
        finally:
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def events():
        while True:
            item = q.get()
            if item is None:
                break
            yield _sse(*item)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/chat/{item_id}/stream")
def stream_chat(item_id: int, body: ChatBody):
    """Server-sent events: tool / reset / delta / done / error."""
    check = get_session()
    try:
        if not check.get(DigestItem, item_id):
            raise HTTPException(404, f"Unknown digest item {item_id}")
    finally:
        check.close()

    def run(emit) -> None:
        session = get_session()
        try:
            run_chat(session, item_id, body.message, emit=emit)
            session.commit()
            note_activity()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return stream_response(run, f"chat for item {item_id}")


@router.get("/chat/{item_id}")
def get_chat(item_id: int):
    session = get_session()
    try:
        return [message_to_dict(m) for m in get_chat_history(session, item_id) if m.role in ("user", "assistant")]
    finally:
        session.close()
