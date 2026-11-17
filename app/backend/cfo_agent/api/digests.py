from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from cfo_agent.db import get_session
from cfo_agent.digest.email import send_digest_email
from cfo_agent.models import Digest, DigestItem, PipelineRun
from cfo_agent.pipeline.orchestrator import WeekBusy, reserve_week, run_pipeline
from cfo_agent.weighting import get_active_weighting

router = APIRouter()


class GenerateDigestBody(BaseModel):
    week: str
    weighting_version: int | None = None


@router.post("/digests/generate")
def generate_digest(body: GenerateDigestBody, background_tasks: BackgroundTasks):
    session = get_session()
    try:
        wv_id = body.weighting_version
        if wv_id is None:
            wv_id = get_active_weighting(session).id
        try:
            run = reserve_week(session, body.week, wv_id)
        except WeekBusy as e:
            raise HTTPException(409, detail=f"A digest run for {body.week} is already in progress") from e
        rid = run.id
        from cfo_agent.settings import get_settings

        if get_settings().job_mode == "queue":
            # Durable: survives a restart or deploy, retried if the worker dies mid-run.
            from cfo_agent.jobqueue import enqueue

            job_id, _ = enqueue("digest", {"week": body.week, "weighting_version_id": wv_id, "run_id": rid},
                                max_attempts=2, created_by="api")
            return {"run_id": rid, "week": body.week, "status": "pending", "job_id": job_id}
        background_tasks.add_task(run_pipeline, rid, body.week, wv_id)
        return {"run_id": rid, "week": body.week, "status": "pending"}
    finally:
        session.close()


@router.get("/digests")
def list_digests():
    session = get_session()
    try:
        rows = session.query(Digest).order_by(Digest.created_at.desc()).all()
        return [
            {
                "id": d.id,
                "week": d.week,
                "headline": d.headline,
                "weighting_label": d.weighting_label,
                "empty_bar": d.empty_bar,
                "email_status": d.email_status,
                "email_path": d.email_path,
                "created_at": d.created_at,
                "item_count": len(d.items),
            }
            for d in rows
        ]
    finally:
        session.close()


@router.get("/digests/{digest_id}")
def get_digest(digest_id: int):
    session = get_session()
    try:
        d = session.get(Digest, digest_id)
        if not d:
            raise HTTPException(404, "Digest not found")
        items = (
            session.query(DigestItem)
            .filter_by(digest_id=d.id)
            .order_by(DigestItem.rank)
            .all()
        )
        run = session.get(PipelineRun, d.pipeline_run_id) if d.pipeline_run_id else None
        return {
            "id": d.id,
            "week": d.week,
            "headline": d.headline,
            "weighting_label": d.weighting_label,
            "data_health": d.data_health_json,
            "empty_bar": d.empty_bar,
            "email_status": d.email_status,
            "email_path": d.email_path,
            "created_at": d.created_at,
            "items": [
                {
                    "id": i.id,
                    "rank": i.rank,
                    "title": i.title,
                    "what": i.what,
                    "why": i.why,
                    "connects": i.connects,
                    "action": i.action,
                    "kra_ids": i.kra_ids_json,
                    "signal_ids": i.signal_ids_json,
                    "citations": i.citations_json,
                    "is_red_line": i.is_red_line,
                    "priority_score": i.priority_score,
                    "score_decomp": i.score_decomp_json,
                    "relationship_ids": i.relationship_ids_json,
                    "verification": i.verification_json or {},
                }
                for i in items
            ],
            "run_audit": {
                "run_id": run.id if run else None,
                "step_timings": run.step_timings_json if run else {},
                "token_usage": run.token_usage_json if run else {},
                "cost_usd": run.cost_usd if run else 0,
                "suppressed": run.suppressed_json if run else [],
            }
            if run
            else None,
        }
    finally:
        session.close()


@router.post("/digests/{digest_id}/email")
def resend_email(digest_id: int):
    session = get_session()
    try:
        d = session.get(Digest, digest_id)
        if not d:
            raise HTTPException(404)
        send_digest_email(session, d)
        session.commit()
        return {"email_status": d.email_status, "email_path": d.email_path}
    finally:
        session.close()
