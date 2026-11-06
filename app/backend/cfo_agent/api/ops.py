"""Operational status for the delegate/admin: is the weekly digest on schedule,
are sources syncing, is the scheduler alive, what is AI spend."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter
from sqlalchemy import func, text

from cfo_agent.db import get_session
from cfo_agent.models import ChatMessage, ConnectorConfig, Digest, PipelineRun

router = APIRouter()


@router.get("/ops/status")
def ops_status():
    from cfo_agent.lease import lease_status
    from cfo_agent.residency import processing_location
    from cfo_agent.settings import get_settings

    s = get_settings()
    session = get_session()
    try:
        now = datetime.utcnow()
        last_digest = session.query(Digest).order_by(Digest.created_at.desc()).first()
        week_ago = now - timedelta(days=7)
        failed = session.query(PipelineRun).filter(
            PipelineRun.created_at >= week_ago, PipelineRun.status.in_(["failed", "data_unavailable"])
        ).count()
        month_ago = now - timedelta(days=30)
        pipeline_cost = session.query(func.coalesce(func.sum(PipelineRun.cost_usd), 0.0)).filter(
            PipelineRun.created_at >= month_ago
        ).scalar() or 0.0
        chat_cost = 0.0
        for (v,) in session.query(ChatMessage.verification_json).filter(
            ChatMessage.role == "assistant", ChatMessage.created_at >= month_ago
        ):
            chat_cost += float((v or {}).get("cost_usd") or 0)
        connectors = [
            {
                "id": c.id,
                "status": c.last_status,
                "last_run_at": c.last_run_at.isoformat() + "Z" if c.last_run_at else None,
            }
            for c in session.query(ConnectorConfig).filter_by(enabled=True).all()
        ]
        lease = lease_status()
        hours_since = (now - last_digest.created_at).total_seconds() / 3600 if last_digest else None
        return {
            "now": now.isoformat() + "Z",
            "data_mode": s.data_mode,
            "demo_mode": s.demo_mode,
            "scheduler": {**lease, "expected": True, "digest_cron": s.digest_cron, "redline_cron": s.redline_cron},
            "digest": {
                "last_created_at": last_digest.created_at.isoformat() + "Z" if last_digest else None,
                "last_week": last_digest.week if last_digest else None,
                # Weekly cadence: anything over 8 days means a Monday was missed.
                "on_schedule": hours_since is not None and hours_since <= 8 * 24,
                "failed_runs_7d": failed,
            },
            "connectors": connectors,
            "ai_spend_usd_30d": {"pipeline": round(pipeline_cost, 2), "chat": round(chat_cost, 2)},
            "ai": {**processing_location(s), "pii_masking": s.pii_masking},
        }
    finally:
        session.close()


@router.get("/ops/ready")
def ready():
    """Readiness: the database answers. (Liveness is /api/health.)"""
    session = get_session()
    try:
        session.execute(text("SELECT 1"))
        return {"ready": True}
    finally:
        session.close()
