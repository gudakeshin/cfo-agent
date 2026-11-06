from __future__ import annotations

from fastapi import APIRouter, HTTPException

from cfo_agent.citations import resolve_citation
from cfo_agent.db import get_session
from cfo_agent.models import DigestItem

router = APIRouter()


@router.get("/citations/{cid:path}")
def get_citation(cid: str, digest_item_id: int | None = None):
    """Resolve a citation. With digest_item_id, return the record as it was when
    that digest was generated (frozen snapshot), so audits are reproducible."""
    session = get_session()
    try:
        if digest_item_id is not None:
            item = session.get(DigestItem, digest_item_id)
            snap = (item.evidence_snapshot_json or {}).get(cid) if item else None
            if snap:
                return {
                    "citation_id": cid,
                    "summary": snap.get("summary", ""),
                    "record": snap.get("record"),
                    "snapshot": True,
                }
        r = resolve_citation(session, cid)
        if not r.ok:
            raise HTTPException(404, detail=r.error)
        return {
            "citation_id": r.citation_id,
            "summary": r.summary,
            "record": r.record,
            "snapshot": False,
        }
    finally:
        session.close()
