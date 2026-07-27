from __future__ import annotations

from fastapi import APIRouter, HTTPException

from cfo_agent.db import get_session
from cfo_agent.models import PipelineRun

router = APIRouter()


@router.get("/runs")
def list_runs():
    session = get_session()
    try:
        rows = session.query(PipelineRun).order_by(PipelineRun.id.desc()).limit(50).all()
        return [
            {
                "id": r.id,
                "week": r.week,
                "status": r.status,
                "current_step": r.current_step,
                "cost_usd": r.cost_usd,
                "digest_id": r.digest_id,
                "error": r.error,
                "created_at": r.created_at,
                "completed_at": r.completed_at,
            }
            for r in rows
        ]
    finally:
        session.close()


@router.get("/runs/{run_id}")
def get_run(run_id: int):
    session = get_session()
    try:
        r = session.get(PipelineRun, run_id)
        if not r:
            raise HTTPException(404)
        return {
            "id": r.id,
            "week": r.week,
            "status": r.status,
            "current_step": r.current_step,
            "step_timings": r.step_timings_json,
            "token_usage": r.token_usage_json,
            "cost_usd": r.cost_usd,
            "suppressed": r.suppressed_json,
            "data_health": r.data_health_json,
            "digest_id": r.digest_id,
            "error": r.error,
            "created_at": r.created_at,
            "completed_at": r.completed_at,
        }
    finally:
        session.close()
