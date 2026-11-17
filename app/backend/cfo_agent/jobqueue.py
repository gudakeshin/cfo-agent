"""Durable background jobs: a queue in each tenant's database.

Cron triggers and manual runs *enqueue* a job; workers (see jobrunner.py) claim and
run it. That gives what in-process background tasks cannot:

  * survives restarts and deploys: a job whose worker dies is picked up again once its
    heartbeat goes stale;
  * retries with exponential backoff, then `dead` (visible, and retryable by hand);
  * no pile-up: a job with a dedupe key is not queued twice while one is active;
  * fairness and limits: workers serve tenants round-robin, and a tenant never has
    more than JOB_MAX_PER_TENANT jobs running at once, however many workers exist.

Claiming is a single conditional UPDATE, atomic on SQLite and PostgreSQL, so any
number of workers can poll the same database. The per-tenant limit is checked just
before the claim, so two workers racing can briefly exceed it by one."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError

from cfo_agent.db import get_session
from cfo_agent.models import Job

logger = logging.getLogger(__name__)

BACKOFF_BASE_S = 30
BACKOFF_CAP_S = 900


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PermanentError(Exception):
    """Raised by a handler when retrying cannot help (bad payload, missing connector…)."""


# kind -> handler(payload, ctx) -> JSON-able result. ctx = {"job_id": int, "attempt": int}
HANDLERS: dict[str, Callable[[dict, dict], Any]] = {}


def job_handler(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


@dataclass(frozen=True)
class JobInfo:
    id: int
    kind: str
    payload: dict
    attempt: int
    max_attempts: int


def enqueue(kind: str, payload: dict | None = None, *, dedupe_key: str | None = None, delay_s: float = 0,
            max_attempts: int = 3, priority: int = 0, created_by: str = "") -> tuple[int, bool]:
    """Queue a job. Returns (job id, created). With a dedupe key already active the existing
    job is returned and nothing is added."""
    s = get_session()
    try:
        job = Job(kind=kind, payload_json=payload or {}, dedupe_key=dedupe_key, max_attempts=max_attempts,
                  priority=priority, created_by=created_by, run_after=_now() + timedelta(seconds=delay_s))
        s.add(job)
        try:
            s.commit()
            return job.id, True
        except IntegrityError:
            s.rollback()
            existing = (s.query(Job).filter(Job.dedupe_key == dedupe_key, Job.status.in_(["queued", "running"]))
                        .first()) if dedupe_key else None
            if existing is None:
                raise
            return existing.id, False
    finally:
        s.close()


def claim_next(worker_id: str, *, max_running: int, now: datetime | None = None) -> JobInfo | None:
    """Claim the next runnable job for this tenant, or None (nothing due, or the tenant is
    already running `max_running` jobs)."""
    now = now or _now()
    s = get_session()
    try:
        if (s.query(func.count(Job.id)).filter(Job.status == "running").scalar() or 0) >= max_running:
            return None
        ids = [i for (i,) in s.query(Job.id).filter(Job.status == "queued", Job.run_after <= now)
               .order_by(Job.priority.desc(), Job.id).limit(5)]
        for jid in ids:
            res = s.execute(
                update(Job).where(Job.id == jid, Job.status == "queued").values(
                    status="running", locked_by=worker_id, heartbeat_at=now, attempts=Job.attempts + 1,
                    started_at=func.coalesce(Job.started_at, now),
                )
            )
            s.commit()
            if res.rowcount == 1:
                job = s.get(Job, jid)
                return JobInfo(job.id, job.kind, dict(job.payload_json or {}), job.attempts, job.max_attempts)
        return None
    finally:
        s.close()


def heartbeat(job_ids: list[int], worker_id: str) -> None:
    if not job_ids:
        return
    s = get_session()
    try:
        s.execute(update(Job).where(Job.id.in_(job_ids), Job.status == "running", Job.locked_by == worker_id)
                  .values(heartbeat_at=_now()))
        s.commit()
    finally:
        s.close()


def complete(job_id: int, result: Any = None) -> None:
    s = get_session()
    try:
        job = s.get(Job, job_id)
        if job is None or job.status != "running":
            return
        job.status, job.result_json, job.finished_at, job.locked_by = "succeeded", result, _now(), None
        s.commit()
    finally:
        s.close()


def backoff_s(attempt: int) -> float:
    return min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** max(0, attempt - 1))


def fail(job_id: int, error: str, *, retryable: bool = True, backoff: bool = True) -> str:
    """Record a failed attempt: back to the queue after a backoff, or `dead`. Returns the new status.
    `backoff=False` retries at once (used when a worker was lost: the wait to notice was the delay)."""
    s = get_session()
    try:
        job = s.get(Job, job_id)
        if job is None or job.status != "running":
            return job.status if job else "gone"
        job.last_error = (error or "")[:2000]
        job.locked_by = None
        if retryable and job.attempts < job.max_attempts:
            job.status = "queued"
            job.run_after = _now() + timedelta(seconds=backoff_s(job.attempts) if backoff else 0)
        else:
            job.status, job.finished_at = "dead", _now()
        s.commit()
        status, kind, attempts, err = job.status, job.kind, job.attempts, job.last_error
    finally:
        s.close()
    if status == "dead":
        _alert_dead(kind, attempts, err)  # straight away, not at the next watchdog pass
    return status


def _alert_dead(kind: str, attempts: int, error: str) -> None:
    try:
        from cfo_agent.ops_alerts import raise_alert

        raise_alert(f"watchdog:job_dead:{kind}", "critical" if kind == "digest" else "warn",
                    f"Background job '{kind}' failed after {attempts} attempt(s)", f"Last error: {error[:400]}")
    except Exception:  # noqa: BLE001 — alerting must never break the queue
        logger.exception("Could not raise the dead-job alert")


def reap_stale(stale_after_s: float) -> int:
    """Running jobs whose worker stopped heartbeating (crash, deploy, hang) count as a
    failed attempt and are retried or declared dead."""
    s = get_session()
    try:
        cutoff = _now() - timedelta(seconds=stale_after_s)
        ids = [i for (i,) in s.query(Job.id).filter(Job.status == "running", Job.heartbeat_at < cutoff)]
    finally:
        s.close()
    for jid in ids:
        status = fail(jid, "worker lost or job exceeded its runtime (no heartbeat)", backoff=False)
        logger.warning("Job %s had no heartbeat; now %s", jid, status)
    return len(ids)


def retry_dead(job_id: int) -> bool:
    s = get_session()
    try:
        job = s.get(Job, job_id)
        if job is None or job.status != "dead":
            return False
        if job.dedupe_key and s.query(Job.id).filter(Job.dedupe_key == job.dedupe_key,
                                                      Job.status.in_(["queued", "running"])).first():
            return False  # a newer job with the same key is already active
        job.status, job.attempts, job.run_after, job.finished_at = "queued", 0, _now(), None
        s.commit()
        return True
    finally:
        s.close()


def run_job(info: JobInfo) -> str:
    """Execute one claimed job and record the outcome. Returns the job's new status."""
    from cfo_agent import job_handlers  # noqa: F401  (registers the handlers)

    handler = HANDLERS.get(info.kind)
    if handler is None:
        return fail(info.id, f"no handler for job kind '{info.kind}'", retryable=False)
    try:
        result = handler(info.payload, {"job_id": info.id, "attempt": info.attempt})
    except PermanentError as e:
        return fail(info.id, str(e), retryable=False)
    except Exception as e:  # noqa: BLE001
        logger.exception("Job %s (%s) attempt %s failed", info.id, info.kind, info.attempt)
        return fail(info.id, f"{type(e).__name__}: {e}")
    complete(info.id, result if isinstance(result, (dict, list, str, int, float, bool)) else None)
    return "succeeded"


def counts() -> dict[str, int]:
    s = get_session()
    try:
        out = {"queued": 0, "running": 0, "succeeded": 0, "dead": 0}
        for status, n in s.query(Job.status, func.count(Job.id)).group_by(Job.status):
            out[status] = n
        return out
    finally:
        s.close()
