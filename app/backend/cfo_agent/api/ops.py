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


@router.get("/ops/jobs")
def ops_jobs(limit: int = 50):
    """The background-job queue: counts, recent jobs, and whether any worker is alive."""
    from cfo_agent import jobqueue
    from cfo_agent.models import Job, JobWorker

    session = get_session()
    try:
        now = datetime.utcnow()
        jobs = session.query(Job).order_by(Job.id.desc()).limit(min(max(limit, 1), 200)).all()
        workers = [w for w in session.query(JobWorker).all() if (now - w.heartbeat_at).total_seconds() < 120]
        counts = jobqueue.counts()
        oldest = session.query(func.min(Job.run_after)).filter(Job.status == "queued").scalar()
        waiting_s = max(0, int((now - oldest).total_seconds())) if oldest else 0
        return {
            "counts": counts,
            "workers": [{"id": w.id, "threads": w.threads, "seen_s_ago": int((now - w.heartbeat_at).total_seconds())} for w in workers],
            # Jobs are queued but nothing is there to run them: the one failure that is silent otherwise.
            "stalled": bool(counts.get("queued")) and not workers and waiting_s > 120,
            "oldest_waiting_s": waiting_s,
            "jobs": [
                {
                    "id": j.id, "kind": j.kind, "status": j.status, "attempts": j.attempts, "max_attempts": j.max_attempts,
                    "payload": j.payload_json, "last_error": j.last_error, "result": j.result_json,
                    "created_at": j.created_at, "started_at": j.started_at, "finished_at": j.finished_at,
                    "run_after": j.run_after,
                }
                for j in jobs
            ],
        }
    finally:
        session.close()


@router.post("/ops/jobs/{job_id}/retry")
def retry_job(job_id: int):
    from fastapi import HTTPException

    from cfo_agent import jobqueue

    if not jobqueue.retry_dead(job_id):
        raise HTTPException(409, "Only a dead job with no active twin can be retried")
    return {"job_id": job_id, "status": "queued"}


@router.get("/ops/alerts")
def ops_alerts():
    """Open operator alerts, this month's AI spend against its ceiling, and backup freshness."""
    from cfo_agent import backup
    from cfo_agent.budgetmeter import monthly_budget_usd, spend_this_month
    from cfo_agent.ops_alerts import open_alerts
    from cfo_agent.settings import get_settings

    session = get_session()
    try:
        spend = spend_this_month(session)
    finally:
        session.close()
    s = get_settings()
    budget = monthly_budget_usd()
    return {
        "alerts": [
            {"key": a.key, "severity": a.severity, "title": a.title, "detail": a.detail,
             "first_seen_at": a.first_seen_at, "last_seen_at": a.last_seen_at, "notify_count": a.notify_count}
            for a in open_alerts()
        ],
        "channels": {"webhook": bool(s.alert_webhook_url), "email": bool(s.alert_email_to.strip())},
        "ai_budget": {"monthly_usd": budget or None, "spent_usd": spend, "used_pct": round(100 * spend["total"] / budget, 1) if budget else None},
        "backup": {"enabled": bool(s.backup_dir), "last_verified_hours_ago": backup.last_backup_age_hours()},
    }
