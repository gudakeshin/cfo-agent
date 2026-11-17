"""What each kind of background job does. Registered with the queue on import."""

from __future__ import annotations

import logging

from cfo_agent.db import get_session
from cfo_agent.jobqueue import PermanentError, enqueue, job_handler

logger = logging.getLogger(__name__)


@job_handler("digest")
def handle_digest(payload: dict, ctx: dict):
    """Run the weekly pipeline. payload: {week?, weighting_version_id?, run_id?}. A scheduled
    job names no week (the next unreported one is chosen); a manual run names the week and
    the run it already reserved. Safe to retry: a finished run is not repeated, and a run
    left half-done by a lost worker is closed out and started afresh."""
    from datetime import datetime

    from cfo_agent.models import PipelineRun
    from cfo_agent.pipeline.orchestrator import WeekBusy, reserve_week, run_pipeline
    from cfo_agent.scheduler import select_digest_week
    from cfo_agent.weighting import get_active_weighting

    s = get_session()
    try:
        wv_id = payload.get("weighting_version_id") or get_active_weighting(s).id
        week = payload.get("week") or select_digest_week(s, wv_id)
        if not week:
            return {"skipped": "nothing to digest"}
        run = s.get(PipelineRun, payload["run_id"]) if payload.get("run_id") else None
        if run is not None and run.status == "completed":
            return {"run_id": run.id, "status": "completed"}
        if run is None or run.status != "pending":
            if run is not None and run.status == "running":
                if ctx["attempt"] <= 1:
                    return {"skipped": f"run {run.id} is already in progress"}
                run.status, run.error, run.completed_at = "failed", "interrupted: the worker running it was lost", datetime.utcnow()
                s.commit()
            try:
                run = reserve_week(s, week, wv_id)
            except WeekBusy as e:
                return {"skipped": str(e)}
        rid = run.id
    finally:
        s.close()

    run_pipeline(rid, week, wv_id)

    s = get_session()
    try:
        run = s.get(PipelineRun, rid)
        if run.status == "failed":
            raise RuntimeError(f"pipeline run {rid} failed: {run.error}")
        return {"run_id": rid, "status": run.status}
    finally:
        s.close()


@job_handler("redline_sweep")
def handle_redline_sweep(payload: dict, ctx: dict):
    from cfo_agent.scheduler import job_redline_sweep

    return {"alerts": len(job_redline_sweep() or [])}


@job_handler("consolidation")
def handle_consolidation(payload: dict, ctx: dict):
    from cfo_agent.scheduler import job_consolidation_debounce

    job_consolidation_debounce()


@job_handler("news_refresh")
def handle_news(payload: dict, ctx: dict):
    from cfo_agent.news import job_news_refresh

    job_news_refresh()


@job_handler("connector_sync")
def handle_connector_sync(payload: dict, ctx: dict):
    from cfo_agent.connectors.sync import scheduled_connector_sync

    cid = payload.get("connector_id")
    if not cid:
        raise PermanentError("connector_sync needs a connector_id")
    return {"run_id": scheduled_connector_sync(cid)}


# ── enqueuers: what the cron triggers call ───────────────────────────────────
# Cheap and non-blocking, so a slow tenant never delays another tenant's schedule.

def enqueue_digest() -> None:
    enqueue("digest", {}, dedupe_key="digest:scheduled")


def enqueue_redline_sweep() -> None:
    enqueue("redline_sweep", {}, dedupe_key="redline_sweep", max_attempts=1)  # the next sweep is minutes away


def enqueue_consolidation() -> None:
    enqueue("consolidation", {}, dedupe_key="consolidation", max_attempts=1)


def enqueue_news() -> None:
    enqueue("news_refresh", {}, dedupe_key="news_refresh", max_attempts=2)


def enqueue_connector_sync(connector_id: str) -> None:
    enqueue("connector_sync", {"connector_id": connector_id}, dedupe_key=f"connector:{connector_id}", max_attempts=2)
