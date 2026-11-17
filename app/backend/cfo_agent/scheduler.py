"""APScheduler jobs: Monday digest, red-line sweep, consolidation debounce."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from cfo_agent.db import get_session
from cfo_agent.digest.email import send_alert_email
from cfo_agent.memory.consolidation import has_unconsolidated_activity, run_consolidation
from cfo_agent.models import Digest
from cfo_agent.pipeline.orchestrator import run_pipeline
from cfo_agent.redlines import (
    close_cleared_episodes,
    evaluate_rules,
    get_last_redline_check,
    get_replay_clock_week,
    open_or_reuse_episode,
    set_last_redline_check,
)
from cfo_agent.settings import get_settings
from cfo_agent.timeutil import iso_week_id, last_completed_sunday, week_sunday
from cfo_agent.weighting import get_active_weighting

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_last_activity: datetime | None = None


def note_activity() -> None:
    global _last_activity
    _last_activity = datetime.utcnow()


def _parse_cron(expr: str) -> dict:
    """Parse 'm h dom mon dow' into CronTrigger kwargs."""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"Bad cron: {expr}")
    return {
        "minute": parts[0],
        "hour": parts[1],
        "day": parts[2],
        "month": parts[3],
        "day_of_week": parts[4],
    }


def _latest_data_sunday(session):
    """Sunday ending the latest week that has ERP data (None if no data)."""
    from cfo_agent.models import ErpCashDaily

    end = session.query(ErpCashDaily.as_of_date).order_by(ErpCashDaily.as_of_date.desc()).limit(1).scalar()
    if not end:
        return None
    if end.weekday() != 6:  # partial week → last completed Sunday before it
        end = end - timedelta(days=end.weekday() + 1)
    return end


def select_digest_week(session, weighting_version_id: int) -> str | None:
    """Which week the scheduled digest should cover.

    live   → the latest completed week with data.
    replay → the week after the replay clock (the demo walks the quarter one week
             per run); the first week when no clock is set.
    Returns None if that week already has a digest under the active weighting —
    a weighting change never back-fills old weeks."""
    from cfo_agent.timeutil import quarter_weeks_ending

    end = _latest_data_sunday(session)
    if not end:
        return None
    weeks = quarter_weeks_ending(end, 13)
    if get_settings().data_mode == "live":
        target = weeks[-1]
    else:
        clock = get_replay_clock_week(session)
        if clock in weeks:
            idx = weeks.index(clock)
            target = weeks[min(idx + 1, len(weeks) - 1)]
        else:
            target = weeks[0]
    exists = session.query(Digest).filter_by(week=target, weighting_version_id=weighting_version_id).first()
    return None if exists else target


def job_digest() -> None:
    """Scheduled weekly digest (see select_digest_week for which week)."""
    from cfo_agent.pipeline.orchestrator import WeekBusy, reserve_week

    session = get_session()
    try:
        wv = get_active_weighting(session)
        target = select_digest_week(session, wv.id)
        if not target:
            logger.info("Digest job: nothing to digest — no-op")
            return
        try:
            run = reserve_week(session, target, wv.id)
        except WeekBusy as e:
            logger.info("Digest job: %s", e)
            return
        rid = run.id
        session.close()
        run_pipeline(rid, target, wv.id)
    except Exception:
        logger.exception("Digest job failed")
    finally:
        session.close()


def job_redline_sweep(as_of=None) -> list[int]:
    session = get_session()
    alert_ids: list[int] = []
    try:
        wv = get_active_weighting(session)
        rules = wv.content_json.get("red_lines") or []
        clock = get_replay_clock_week(session)
        if as_of is None:
            if get_settings().data_mode == "live":
                # Live feed: evaluate against the latest data, never beyond today.
                from cfo_agent.models import ErpCashDaily
                from cfo_agent.timeutil import today_ist

                latest = session.query(ErpCashDaily.as_of_date).order_by(ErpCashDaily.as_of_date.desc()).limit(1).scalar()
                if not latest:
                    logger.info("Redline sweep: no ERP data — skip")
                    return []
                as_of = min(latest, today_ist())
            elif clock:
                as_of = week_sunday(clock)
            else:
                logger.info("Redline sweep: replay mode with no replay clock — skip (avoid future leak)")
                return []
        last = get_last_redline_check(session)
        fires = evaluate_rules(session, rules, as_of=as_of, last_check_date=last)
        close_cleared_episodes(session, rules, as_of)
        for fire in fires:
            alert = open_or_reuse_episode(session, fire)
            if alert:
                # Best-effort mini synthesis — alert already fired
                try:
                    from cfo_agent.llm import LlmBudget, parse
                    from pydantic import BaseModel

                    class Mini(BaseModel):
                        narrative: str

                    budget = LlmBudget(max_calls=1)
                    mini = parse(
                        Mini,
                        system="Summarize this red-line breach for a CFO in 2 sentences.",
                        user=str(alert.evidence_json),
                        purpose="alert_synthesis",
                        budget=budget,
                    )
                    alert.synthesis_json = {"narrative": mini.narrative}
                except Exception as e:  # noqa: BLE001
                    logger.warning("Alert synthesis failed (alert still delivered): %s", e)
                send_alert_email(session, alert)
                alert_ids.append(alert.id)
        set_last_redline_check(session, as_of)
        session.commit()
    except Exception:
        logger.exception("Redline sweep failed")
        session.rollback()
    finally:
        session.close()
    return alert_ids


def job_consolidation_debounce() -> None:
    global _last_activity
    if _last_activity is None:
        return
    if datetime.utcnow() - _last_activity < timedelta(minutes=10):
        return
    session = get_session()
    try:
        if has_unconsolidated_activity(session):
            run_consolidation(session)
            session.commit()
            _last_activity = None
    except Exception:
        logger.exception("Consolidation debounce failed")
        session.rollback()
    finally:
        session.close()


def get_scheduler() -> BackgroundScheduler | None:
    """Return the running scheduler, or None when not started (tests)."""
    return _scheduler


def tenant_job_id(job_id: str) -> str:
    from cfo_agent.tenancy import current_tenant_id

    tid = current_tenant_id()
    return f"{tid}:{job_id}" if tid else job_id


def _queue_mode() -> bool:
    return get_settings().job_mode == "queue"


def upsert_job(job_id: str, fn, cron: str) -> None:
    """Register or replace a cron job for the current tenant. No-op when the
    scheduler is not started."""
    from cfo_agent.tenancy import current_tenant_id

    sched = get_scheduler()
    if sched is None:
        return
    settings = get_settings()
    tz = ZoneInfo(settings.timezone)
    tid = current_tenant_id()
    if _queue_mode() and job_id.startswith("connector:"):
        # A connector's cron only enqueues; a worker runs the sync.
        from cfo_agent.job_handlers import enqueue_connector_sync

        fn = lambda cid=job_id.split(":", 1)[1]: enqueue_connector_sync(cid)  # noqa: E731
    job_id = tenant_job_id(job_id)
    sched.add_job(
        leased(job_id, fn, tenant=tid or None, per_tenant=False),
        CronTrigger(**_parse_cron(cron), timezone=tz),
        id=job_id,
        replace_existing=True,
    )


def remove_job(job_id: str) -> None:
    """Remove a job by id. No-op when scheduler is not started or job missing."""
    sched = get_scheduler()
    if sched is None:
        return
    try:
        sched.remove_job(tenant_job_id(job_id))
    except Exception:  # noqa: BLE001 — JobLookupError and friends
        pass


def leased(job_id: str, fn, *, tenant: str | None = None, per_tenant: bool = True):
    """Run `fn` only on the worker holding the scheduler lease, and record the run.
    Global jobs run once per tenant (each tenant DB has its own lease); a job bound
    to `tenant` runs only there."""
    import functools

    from cfo_agent.tenancy import for_each_tenant, tenant_context

    def once(*args, **kwargs):
        from cfo_agent.lease import holds_lease, note_job_run

        if not holds_lease():
            logger.debug("Job %s skipped: another worker holds the scheduler lease", job_id)
            return None
        try:
            return fn(*args, **kwargs)
        finally:
            note_job_run(job_id)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if tenant:
            from cfo_agent.tenancy import tenants

            if tenant not in tenants():
                logger.debug("Job %s skipped: tenant %s is disabled or removed", job_id, tenant)
                return None
            with tenant_context(tenant):
                return once(*args, **kwargs)
        if per_tenant:
            return for_each_tenant(once, *args, **kwargs)
        return once(*args, **kwargs)

    return wrapper


def job_lease_heartbeat() -> None:
    from cfo_agent.tenancy import for_each_tenant

    for_each_tenant(_heartbeat_one)


def _heartbeat_one() -> None:
    from cfo_agent.lease import OWNER_ID, acquire_or_renew

    try:
        held = acquire_or_renew()
        logger.debug("Scheduler lease %s by %s", "held" if held else "standby", OWNER_ID)
    except Exception:  # noqa: BLE001
        logger.exception("Scheduler lease heartbeat failed")


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    settings = get_settings()
    tz = ZoneInfo(settings.timezone)
    job_lease_heartbeat()  # take the lease now if it is free
    sched = BackgroundScheduler(timezone=tz)
    sched.add_job(job_lease_heartbeat, "interval", seconds=60, id="lease")
    queued = _queue_mode()
    from cfo_agent import job_handlers as jh

    sched.add_job(
        leased("digest", jh.enqueue_digest if queued else job_digest),
        CronTrigger(**_parse_cron(settings.digest_cron), timezone=tz), id="digest",
    )
    sched.add_job(
        leased("redline", jh.enqueue_redline_sweep if queued else job_redline_sweep),
        CronTrigger(**_parse_cron(settings.redline_cron), timezone=tz),
        id="redline",
    )
    sched.add_job(
        leased("consolidation", jh.enqueue_consolidation if queued else job_consolidation_debounce),
        "interval", minutes=2, id="consolidation",
    )
    # News refresh — only when scheduler is started (never in tests)
    news_cron = getattr(settings, "news_cron", "30 7 * * *")
    try:
        from cfo_agent.news import job_news_refresh

        sched.add_job(
            leased("news", jh.enqueue_news if queued else job_news_refresh),
            CronTrigger(**_parse_cron(news_cron), timezone=tz),
            id="news",
        )
    except Exception:  # noqa: BLE001 — module may not exist mid-migration
        logger.debug("News job not registered yet", exc_info=True)
    sched.start()
    _scheduler = sched
    logger.info("Scheduler started digest=%s redline=%s", settings.digest_cron, settings.redline_cron)
    return sched
