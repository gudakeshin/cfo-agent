"""Async pipeline orchestrator — Map→Weight→Connect→Filter→Synthesize→Surface."""

from __future__ import annotations

import logging
import time
from datetime import datetime

from sqlalchemy.orm import Session

from cfo_agent.db import get_session
from cfo_agent.digest.builder import persist_digest
from cfo_agent.digest.email import send_digest_email
from cfo_agent.kra import get_active_framework, get_active_kra_version
from cfo_agent.llm import LlmBudget
from cfo_agent.memory.archival import archival_insert
from cfo_agent.memory.consolidation import has_unconsolidated_activity, run_consolidation
from cfo_agent.models import PipelineRun
from cfo_agent.pipeline.step1_map import step1_map
from cfo_agent.pipeline.step2_weight import step2_weight
from cfo_agent.pipeline.step3_connect import step3_connect
from cfo_agent.pipeline.step4_filter import step4_filter
from cfo_agent.pipeline.step5_synthesize import step5_headline, step5_synthesize_item
from cfo_agent.pipeline.step6_surface import step6_surface
from cfo_agent.redlines import set_replay_clock_week
from cfo_agent.settings import get_settings
from cfo_agent.validation import check_data_health
from cfo_agent.weighting import get_active_weighting, get_weighting_version

logger = logging.getLogger(__name__)

# In-process lock: week -> run_id
_week_locks: dict[str, int] = {}


def week_is_locked(week: str) -> int | None:
    return _week_locks.get(week)


def run_pipeline(
    run_id: int,
    week: str,
    weighting_version_id: int | None = None,
) -> None:
    session = get_session()
    try:
        run = session.get(PipelineRun, run_id)
        if not run:
            return
        run.status = "running"
        run.current_step = "validate"
        session.commit()

        _week_locks[week] = run_id
        timings: dict[str, int] = {}

        def tick(step: str):
            run.current_step = step
            session.commit()
            return time.time()

        # Pre-digest consolidation if dirty
        if has_unconsolidated_activity(session):
            try:
                run_consolidation(session)
                session.commit()
            except Exception as e:  # noqa: BLE001
                logger.warning("Pre-digest consolidation failed: %s", e)
                session.rollback()

        t0 = tick("validate")
        health = check_data_health(session, week=week)
        run.data_health_json = health.to_dict()
        if health.hard_failure:
            run.status = "data_unavailable"
            run.error = "; ".join(health.errors)
            run.completed_at = datetime.utcnow()
            # Still create empty digest message
            persist_digest(
                session,
                week=week,
                weighting=get_active_weighting(session) if not weighting_version_id else get_weighting_version(session, weighting_version_id),
                kra_version=get_active_kra_version(session),
                run=run,
                headline="Data unavailable — digest aborted",
                surfaced=[],
                empty_bar=True,
                data_health=health.to_dict(),
            )
            session.commit()
            return
        timings["validate"] = int((time.time() - t0) * 1000)

        wv = (
            get_weighting_version(session, weighting_version_id)
            if weighting_version_id
            else get_active_weighting(session)
        )
        weighting = wv.content_json
        framework = get_active_framework(session)
        kra_ver = get_active_kra_version(session)
        run.kra_version_id = kra_ver.id
        run.weighting_version_id = wv.id
        session.commit()

        budget = LlmBudget(max_calls=get_settings().max_llm_calls_per_run)

        t0 = tick("map")
        mapped = step1_map(session, week)
        session.commit()
        timings["map"] = int((time.time() - t0) * 1000)

        t0 = tick("weight")
        weighted = step2_weight(mapped, weighting, framework)
        timings["weight"] = int((time.time() - t0) * 1000)

        t0 = tick("connect")
        top15 = weighted[:15]
        connect_out = step3_connect(session, top15, framework, weighting, budget)
        timings["connect"] = int((time.time() - t0) * 1000)

        t0 = tick("filter")
        filtered, empty_bar = step4_filter(
            session, weighted, connect_out.connections, weighting, week
        )
        timings["filter"] = int((time.time() - t0) * 1000)

        t0 = tick("synthesize")
        pairs = []
        for item in filtered:
            syn = step5_synthesize_item(
                session, item, framework, weighting, week, budget
            )
            pairs.append((item, syn))

        def repair_fn(item, syn, errors):
            # One repair retry with the failed citations fed back — counts against budget
            return step5_synthesize_item(
                session, item, framework, weighting, week, budget, repair_errors=errors
            )

        surfaced, suppressed = step6_surface(session, pairs, repair_fn=repair_fn)
        titles = [s.synthesis.title for s in surfaced]
        headline = step5_headline(
            session, titles, weighting, week, budget, empty_bar or not surfaced
        )
        timings["synthesize"] = int((time.time() - t0) * 1000)

        t0 = tick("surface")
        digest = persist_digest(
            session,
            week=week,
            weighting=wv,
            kra_version=kra_ver,
            run=run,
            headline=headline,
            surfaced=surfaced,
            empty_bar=empty_bar or not surfaced,
            data_health=health.to_dict(),
        )
        # Archive after items exist so metadata.digest_item_id is set (consolidation needs it)
        items_by_rank = {di.rank: di for di in digest.items}
        for rank, s in enumerate(surfaced, start=1):
            di = items_by_rank.get(rank)
            archival_insert(
                session,
                f"[{week}] {s.synthesis.title}: {s.synthesis.what.text}",
                metadata={
                    "digest_id": digest.id,
                    "digest_item_id": di.id if di else None,
                    "digest_item_rank": rank,
                    "type": "digest_item",
                    "kra_ids": s.filter_item.kra_ids,
                    "is_red_line": s.filter_item.is_red_line,
                },
                week=week,
            )

        try:
            send_digest_email(session, digest)
        except Exception as e:  # noqa: BLE001
            logger.warning("Digest email failed: %s", e)
            digest.email_status = "failed"

        run.suppressed_json = [
            {"title": s.title, "reason": s.reason, "bad": s.bad_citations, "detail": s.detail}
            for s in suppressed
        ]
        run.step_timings_json = timings
        run.token_usage_json = budget.as_dict()
        run.cost_usd = budget.total_cost()
        run.digest_id = digest.id
        run.status = "completed"
        run.current_step = "done"
        run.completed_at = datetime.utcnow()
        timings["surface"] = int((time.time() - t0) * 1000)
        run.step_timings_json = timings

        set_replay_clock_week(session, week)
        session.commit()
        logger.info("Pipeline run %s completed digest %s", run_id, digest.id)

    except Exception as e:  # noqa: BLE001
        logger.exception("Pipeline run %s failed", run_id)
        session.rollback()
        run = session.get(PipelineRun, run_id)
        if run:
            run.status = "failed"
            run.error = str(e)
            run.completed_at = datetime.utcnow()
            session.commit()
    finally:
        _week_locks.pop(week, None)
        session.close()
