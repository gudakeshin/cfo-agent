"""The watchdog: every few minutes, per tenant, check the things that fail quietly and
raise (or clear) operator alerts. Each check is a function returning the alerts that hold
right now; `sync_alerts` turns that into notifications, repeats and "resolved" messages.

What it watches: a digest run that failed, a Monday digest that never came (live mode),
jobs that died, a queue with nobody to run it, a scheduler that stopped, a data feed that
went stale (live mode), connectors in error, the AI monthly budget, and backups."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from cfo_agent.db import get_session
from cfo_agent.models import ConnectorConfig, Digest, ErpCashDaily, Job, JobWorker, PipelineRun
from cfo_agent.settings import get_settings

logger = logging.getLogger(__name__)

PREFIX = "watchdog:"
Alerts = dict[str, tuple[str, str, str]]  # key -> (severity, title, detail)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def check_jobs(s: Session, now: datetime) -> Alerts:
    out: Alerts = {}
    since = now - timedelta(hours=24)
    dead = s.query(Job).filter(Job.status == "dead", Job.finished_at >= since).order_by(Job.finished_at.desc()).all()
    by_kind: dict[str, list[Job]] = {}
    for j in dead:
        by_kind.setdefault(j.kind, []).append(j)
    for kind, jobs in by_kind.items():
        latest = jobs[0]
        sev = "critical" if kind == "digest" else "warn"
        out[f"{PREFIX}job_dead:{kind}"] = (
            sev, f"Background job '{kind}' failed after {latest.attempts} attempt(s)",
            f"{len(jobs)} dead in 24h. Last error: {(latest.last_error or 'unknown')[:400]}",
        )
    queued = s.query(func.count(Job.id)).filter(Job.status == "queued").scalar() or 0
    if queued:
        oldest = s.query(func.min(Job.run_after)).filter(Job.status == "queued").scalar()
        waiting = (now - oldest).total_seconds() if oldest else 0
        alive = [w for w in s.query(JobWorker).all() if (now - w.heartbeat_at).total_seconds() < 120]
        if waiting > 120 and not alive:
            out[f"{PREFIX}queue_stalled"] = (
                "critical", "Jobs are waiting and no worker is running",
                f"{queued} queued, oldest waiting {int(waiting // 60)} min. Start a worker: python -m cfo_agent.worker",
            )
    return out


def check_digests(s: Session, now: datetime) -> Alerts:
    out: Alerts = {}
    last_run = s.query(PipelineRun).order_by(PipelineRun.created_at.desc()).first()
    if last_run is not None and last_run.created_at >= now - timedelta(days=7) and last_run.status in ("failed", "data_unavailable"):
        failed = last_run.status == "failed"
        out[f"{PREFIX}digest_failed"] = (
            "critical" if failed else "warn",
            f"The latest digest run for {last_run.week} {'failed' if failed else 'could not run: data unavailable'}",
            (last_run.error or "")[:500],
        )
    if get_settings().data_mode == "live":
        last = s.query(Digest).order_by(Digest.created_at.desc()).first()
        if last is not None and now - last.created_at > timedelta(days=8):
            out[f"{PREFIX}digest_late"] = (
                "critical", "No weekly digest for more than 8 days",
                f"The last one was for {last.week}, created {last.created_at:%Y-%m-%d}.",
            )
    return out


def check_data_and_sources(s: Session, now: datetime) -> Alerts:
    out: Alerts = {}
    if get_settings().data_mode == "live":
        latest = s.query(func.max(ErpCashDaily.as_of_date)).scalar()
        limit = get_settings().alert_data_stale_days
        if latest is not None and (now.date() - latest).days > limit:
            out[f"{PREFIX}data_stale"] = (
                "warn", f"Finance data has not updated for {(now.date() - latest).days} days",
                f"The latest cash balance is dated {latest}. Check the connector or file drop.",
            )
    for c in s.query(ConnectorConfig).filter_by(enabled=True).all():
        if c.last_status == "error":
            out[f"{PREFIX}connector:{c.id}"] = ("warn", f"Connector '{c.id}' last sync failed", "")
    return out


def check_scheduler(s: Session, now: datetime) -> Alerts:
    from cfo_agent.lease import lease_status

    st = lease_status()
    # A lease that exists but has lapsed: a scheduler was running and stopped. (No lease at all
    # means none was ever started here, e.g. a demo without a worker, and is not an outage.)
    if st["heartbeat_at"] and not st["running"]:
        return {f"{PREFIX}scheduler_down": ("critical", "The scheduler has stopped", f"Last heartbeat {st['heartbeat_at']}.")}
    return {}


def check_budget(s: Session, now: datetime) -> Alerts:
    from cfo_agent.budgetmeter import monthly_budget_usd, spend_this_month

    budget = monthly_budget_usd()
    if budget <= 0:
        return {}
    spent = spend_this_month(s, now)["total"]
    pct = 100 * spent / budget
    if pct >= 100:
        return {f"{PREFIX}llm_budget": ("critical", "Monthly AI budget used up",
                                         f"${spent:.2f} of ${budget:.2f}. Digests use deterministic text and chat is paused until it resets.")}
    if pct >= 80:
        return {f"{PREFIX}llm_budget": ("warn", f"Monthly AI budget {pct:.0f}% used", f"${spent:.2f} of ${budget:.2f}.")}
    return {}


def check_backups(s: Session, now: datetime) -> Alerts:
    from cfo_agent import backup

    if not get_settings().backup_dir:
        return {}
    age = backup.last_backup_age_hours()
    if age is None or age > 36:
        return {f"{PREFIX}backup_stale": ("warn", "No recent backup",
                                           "No verified backup in the last 36 hours." if age is None else f"The last one is {age:.0f} hours old.")}
    return {}


CHECKS = (check_jobs, check_digests, check_data_and_sources, check_scheduler, check_budget, check_backups)


def run_checks(session: Session | None = None, now: datetime | None = None) -> Alerts:
    own = session is None
    s = session or get_session()
    try:
        now = now or _now()
        active: Alerts = {}
        for check in CHECKS:
            try:
                active.update(check(s, now))
            except Exception:  # noqa: BLE001 — one broken check must not blind the others
                logger.exception("Watchdog check %s failed", check.__name__)
        return active
    finally:
        if own:
            s.close()


def watchdog_job() -> None:
    """Scheduled entry point (once per tenant)."""
    from cfo_agent.ops_alerts import sync_alerts

    sync_alerts(PREFIX, run_checks())
