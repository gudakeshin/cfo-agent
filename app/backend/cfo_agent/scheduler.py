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
from cfo_agent.models import Digest, PipelineRun
from cfo_agent.pipeline.orchestrator import run_pipeline, week_is_locked
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


def job_digest() -> None:
    """Generate earliest week that has data but no digest under active weighting."""
    session = get_session()
    try:
        from cfo_agent.models import ErpCashDaily
        from cfo_agent.timeutil import quarter_weeks_ending

        end = session.query(ErpCashDaily.as_of_date).order_by(ErpCashDaily.as_of_date.desc()).limit(1).scalar()
        if not end:
            logger.info("Digest job: no ERP data")
            return
        # Align to Sunday
        while end.weekday() != 6:
            end = end  # already should be Sunday from generator
            break
        weeks = quarter_weeks_ending(end if end.weekday() == 6 else last_completed_sunday(), 13)
        wv = get_active_weighting(session)
        target = None
        for w in weeks:
            exists = (
                session.query(Digest)
                .filter_by(week=w, weighting_version_id=wv.id)
                .first()
            )
            if not exists:
                target = w
                break
        if not target:
            logger.info("Digest job: no undigested week — no-op")
            return
        if week_is_locked(target):
            logger.info("Digest job: week %s locked", target)
            return
        run = PipelineRun(
            week=target,
            weighting_version_id=wv.id,
            status="pending",
        )
        session.add(run)
        session.commit()
        rid = run.id
        session.close()
        run_pipeline(rid, target, wv.id)
    except Exception:
        logger.exception("Digest job failed")
        session.close()


def job_redline_sweep(as_of=None) -> list[int]:
    session = get_session()
    alert_ids: list[int] = []
    try:
        wv = get_active_weighting(session)
        rules = wv.content_json.get("red_lines") or []
        clock = get_replay_clock_week(session)
        if as_of is None:
            if clock:
                as_of = week_sunday(clock)
            else:
                logger.info("Redline sweep: no replay clock — skip (avoid future leak)")
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


def upsert_job(job_id: str, fn, cron: str) -> None:
    """Register or replace a cron job. No-op when scheduler is not started."""
    sched = get_scheduler()
    if sched is None:
        return
    settings = get_settings()
    tz = ZoneInfo(settings.timezone)
    sched.add_job(
        fn,
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
        sched.remove_job(job_id)
    except Exception:  # noqa: BLE001 — JobLookupError and friends
        pass


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    settings = get_settings()
    tz = ZoneInfo(settings.timezone)
    sched = BackgroundScheduler(timezone=tz)
    sched.add_job(job_digest, CronTrigger(**_parse_cron(settings.digest_cron), timezone=tz), id="digest")
    sched.add_job(
        job_redline_sweep,
        CronTrigger(**_parse_cron(settings.redline_cron), timezone=tz),
        id="redline",
    )
    sched.add_job(job_consolidation_debounce, "interval", minutes=2, id="consolidation")
    # News refresh — only when scheduler is started (never in tests)
    news_cron = getattr(settings, "news_cron", "30 7 * * *")
    try:
        from cfo_agent.news import job_news_refresh

        sched.add_job(
            job_news_refresh,
            CronTrigger(**_parse_cron(news_cron), timezone=tz),
            id="news",
        )
    except Exception:  # noqa: BLE001 — module may not exist mid-migration
        logger.debug("News job not registered yet", exc_info=True)
    sched.start()
    _scheduler = sched
    logger.info("Scheduler started digest=%s redline=%s", settings.digest_cron, settings.redline_cron)
    return sched
