from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from cfo_agent.chat import chat, get_chat_history
from cfo_agent.db import get_session
from cfo_agent.scheduler import note_activity

router = APIRouter()


class ChatBody(BaseModel):
    message: str


@router.post("/chat/{item_id}")
def post_chat(item_id: int, body: ChatBody):
    session = get_session()
    try:
        msg = chat(session, item_id, body.message)
        session.commit()
        note_activity()
        return {
            "id": msg.id,
            "role": msg.role,
            "content": msg.content,
            "created_at": msg.created_at,
        }
    except ValueError as e:
        session.rollback()
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        session.rollback()
        raise HTTPException(500, str(e)) from e
    finally:
        session.close()


@router.get("/chat/{item_id}")
def get_chat(item_id: int):
    session = get_session()
    try:
        rows = get_chat_history(session, item_id)
        return [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at,
            }
            for m in rows
            if m.role in ("user", "assistant")
        ]
    finally:
        session.close()
