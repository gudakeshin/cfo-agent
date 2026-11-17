from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from cfo_agent.db import get_session
from cfo_agent.models import (
    Alert,
    ArchivalMemory,
    ChatMessage,
    ConnectorConfig,
    Digest,
    DigestItem,
    Feedback,
    PipelineRun,
    Signal,
)
from cfo_agent.synthetic.generator import generate_erp
from cfo_agent.validation import check_data_health

router = APIRouter()


class GenerateBody(BaseModel):
    seed: int = 42
    stale_days: int = 0
    confirm: bool = False


DEPENDENT = [Feedback, ChatMessage, DigestItem, Digest, Alert, Signal, PipelineRun, ArchivalMemory]


def _set_erp_seed(session: Session, seed: int) -> None:
    from cfo_agent.models import AppState

    row = session.get(AppState, "erp_seed")
    if row is None:
        session.add(AppState(key="erp_seed", value_json=seed))
    else:
        row.value_json = seed


@router.post("/data/generate")
def data_generate(body: GenerateBody):
    from cfo_agent.settings import get_settings

    if not get_settings().demo_mode:
        raise HTTPException(403, detail="Synthetic data generation is disabled outside demo mode")
    session = get_session()
    try:
        digest_count = session.query(Digest).count()
        if digest_count > 0 and not body.confirm:
            raise HTTPException(
                409,
                detail="Digests exist — regeneration cascades wipe digests/alerts/signals/feedback/archival. Pass confirm=true.",
            )
        if body.confirm or digest_count > 0:
            for model in DEPENDENT:
                session.query(model).delete()
            session.flush()
        meta = generate_erp(session, seed=body.seed, stale_days=body.stale_days)
        _set_erp_seed(session, body.seed)
        # New ERP snapshot invalidates every connector's incremental watermark —
        # next sync for each must re-derive from week 1 (or full-resync, per mode).
        for cfg in session.query(ConnectorConfig).all():
            cfg.watermark_week = None
            if cfg.last_status != "running":
                cfg.last_status = "never_run"
        meta = {**meta, "seed": body.seed}
        session.commit()
        return {"ok": True, "meta": meta}
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(500, str(e)) from e
    finally:
        session.close()


@router.get("/data/health")
def data_health(week: str | None = None):
    session = get_session()
    try:
        h = check_data_health(session, week=week)
        return h.to_dict()
    finally:
        session.close()


@router.get("/data/weeks")
def data_weeks():
    """ISO weeks present in the seeded ERP quarter (as-of Sundays)."""
    session = get_session()
    try:
        from cfo_agent.models import ErpCashDaily
        from cfo_agent.timeutil import iso_week_id, quarter_weeks_ending

        end = (
            session.query(ErpCashDaily.as_of_date)
            .order_by(ErpCashDaily.as_of_date.desc())
            .limit(1)
            .scalar()
        )
        if not end:
            return {"weeks": []}
        # Snap to Sunday
        while end.weekday() != 6:
            from datetime import timedelta

            end = end - timedelta(days=1)
        weeks = quarter_weeks_ending(end, 13)
        return {"weeks": weeks, "end": end.isoformat()}
    finally:
        session.close()
