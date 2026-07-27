from __future__ import annotations

from fastapi import APIRouter, HTTPException

from cfo_agent.citations import resolve_citation
from cfo_agent.db import get_session

router = APIRouter()


@router.get("/citations/{cid:path}")
def get_citation(cid: str):
    session = get_session()
    try:
        r = resolve_citation(session, cid)
        if not r.ok:
            raise HTTPException(404, detail=r.error)
        return {
            "citation_id": r.citation_id,
            "summary": r.summary,
            "record": r.record,
        }
    finally:
        session.close()
