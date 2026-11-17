from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from cfo_agent.db import get_session
from cfo_agent.memory.archival import archival_search, check_archival_consistency
from cfo_agent.memory.blocks import (
    AGENT_EDITABLE,
    HUMAN_ONLY,
    MANAGED,
    MemoryError as BlockError,
    provenance,
    get_block,
    list_blocks,
    revision_history,
    set_block_content,
)
from cfo_agent.memory.consolidation import run_consolidation
from cfo_agent.memory.recall import recall_search
from cfo_agent.models import ConsolidationRun

router = APIRouter()
logger = logging.getLogger(__name__)


class SearchBody(BaseModel):
    query: str
    k: int = 5
    scope: str | None = "global"


class BlockUpdate(BaseModel):
    content: str
    reason: str = "Human override"


@router.get("/memory/blocks")
def memory_blocks():
    session = get_session()
    try:
        rows = list_blocks(session)
        return [
            {
                "name": b.name,
                "content": b.content,
                "char_limit": b.char_limit,
                "updated_at": b.updated_at,
                # Human-only blocks are managed by KRA/Weighting editors — not editable here
                "editable": b.name not in MANAGED,
                "agent_editable": b.name in AGENT_EDITABLE,
                "provenance": provenance(b.name),
                "managed_by": (
                    "kra" if b.name == "kra_framework"
                    else "weighting" if b.name == "priority_weighting"
                    else None
                ),
            }
            for b in rows
        ]
    finally:
        session.close()


@router.get("/memory/blocks/{name}/history")
def block_history(name: str):
    session = get_session()
    try:
        get_block(session, name)
        revs = revision_history(session, name)
        return [
            {
                "version": r.version,
                "content": r.content,
                "edited_by": r.edited_by,
                "reason": r.reason,
                "created_at": r.created_at,
            }
            for r in revs
        ]
    except BlockError as e:
        raise HTTPException(404, str(e)) from e
    finally:
        session.close()


@router.put("/memory/blocks/{name}")
def update_block(name: str, body: BlockUpdate):
    if name in MANAGED:
        raise HTTPException(
            409,
            detail=(
                f"Block '{name}' is managed by the "
                f"{'KRA' if name == 'kra_framework' else 'Weighting'} editor. "
                "Edit it there instead."
            ),
        )
    session = get_session()
    try:
        row = set_block_content(
            session,
            name,
            body.content,
            edited_by="human",
            reason=body.reason,
            enforce_agent_rules=False,
        )
        session.commit()
        return {"name": row.name, "content": row.content, "updated_at": row.updated_at}
    except BlockError as e:
        session.rollback()
        raise HTTPException(422, str(e)) from e
    finally:
        session.close()


@router.post("/memory/search")
def memory_search(body: SearchBody):
    session = get_session()
    try:
        archival = archival_search(session, body.query, k=body.k, scope=body.scope)
        recall = recall_search(session, body.query, limit=body.k, scope=body.scope)
        return {"archival": archival, "recall": recall}
    finally:
        session.close()


@router.post("/memory/consolidate")
def consolidate():
    session = get_session()
    try:
        run = run_consolidation(session)
        session.commit()
        if not run:
            return {"ok": True, "message": "Nothing to consolidate"}
        return {
            "ok": True,
            "id": run.id,
            "summary": run.summary,
            "edits": run.edits_json,
            "token_usage": run.token_usage_json,
        }
    except Exception as e:
        session.rollback()
        logger.exception("Memory operation failed")
        raise HTTPException(500, "Memory operation failed; see server logs") from e
    finally:
        session.close()


@router.post("/memory/maintenance")
def memory_maintenance():
    session = get_session()
    try:
        stats = check_archival_consistency(session)
        session.commit()
        return {"ok": True, "stats": stats}
    except Exception as e:
        session.rollback()
        logger.exception("Memory operation failed")
        raise HTTPException(500, "Memory operation failed; see server logs") from e
    finally:
        session.close()


@router.get("/memory/consolidations")
def list_consolidations(limit: int = 20):
    session = get_session()
    try:
        rows = (
            session.query(ConsolidationRun)
            .order_by(ConsolidationRun.id.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": r.id,
                "window_start": r.window_start,
                "window_end": r.window_end,
                "summary": r.summary,
                "token_usage": r.token_usage_json,
                "edits_count": len(r.edits_json or []),
                "created_at": r.created_at,
            }
            for r in rows
        ]
    finally:
        session.close()
