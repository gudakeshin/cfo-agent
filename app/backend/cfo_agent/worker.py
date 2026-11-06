"""Dedicated job worker: `python -m cfo_agent.worker`.

Run the API with RUN_SCHEDULER=false (any number of replicas) and one or more of
these workers. Workers compete for a database lease; only the holder runs jobs, and
a standby takes over within ~3 minutes if the holder dies. Connector schedules edited
in the UI are picked up every two minutes.
"""

from __future__ import annotations

import logging
import signal
import threading

from cfo_agent.observability import configure_logging

configure_logging()
logger = logging.getLogger("cfo_agent.worker")


def sync_connector_jobs() -> None:
    from cfo_agent.tenancy import for_each_tenant

    for_each_tenant(_sync_connector_jobs_one)


def _sync_connector_jobs_one() -> None:
    from cfo_agent.connectors.sync import scheduled_connector_sync
    from cfo_agent.db import get_session
    from cfo_agent.models import ConnectorConfig
    from cfo_agent.scheduler import get_scheduler, remove_job, upsert_job

    sched = get_scheduler()
    if sched is None:
        return
    session = get_session()
    try:
        wanted = {
            f"connector:{c.id}": (c.id, c.cron)
            for c in session.query(ConnectorConfig).filter_by(enabled=True).all()
            if c.cron
        }
    finally:
        session.close()
    from cfo_agent.scheduler import tenant_job_id

    prefix = tenant_job_id("connector:")
    current = {j.id.removeprefix(prefix.removesuffix("connector:")) for j in sched.get_jobs() if j.id.startswith(prefix)}
    for job_id in current - wanted.keys():
        remove_job(job_id)
    for job_id, (cid, cron) in wanted.items():
        upsert_job(job_id, lambda cid=cid: scheduled_connector_sync(cid), cron)


def main() -> None:
    from cfo_agent.db import get_session, init_db
    from cfo_agent.kra import upsert_kra_version
    from cfo_agent.main import _recover_stale_runs
    from cfo_agent.memory.blocks import ensure_seed_blocks
    from cfo_agent.scheduler import start_scheduler
    from cfo_agent.security import validate_deployment_settings
    from cfo_agent.weighting import seed_weighting_from_yaml

    problems = validate_deployment_settings()
    if problems:
        raise SystemExit("Unsafe configuration:\n- " + "\n- ".join(problems))
    init_db()

    def _seed_one():
        session = get_session()
        try:
            upsert_kra_version(session)
            seed_weighting_from_yaml(session)
            ensure_seed_blocks(session)
            _recover_stale_runs(session)
            session.commit()
        finally:
            session.close()

    from cfo_agent.tenancy import for_each_tenant

    for_each_tenant(_seed_one)

    sched = start_scheduler()
    from cfo_agent.main import job_recover_stale_runs
    from cfo_agent.scheduler import leased

    sched.add_job(sync_connector_jobs, "interval", minutes=2, id="connector_jobs_sync")
    sched.add_job(leased("stale_runs", job_recover_stale_runs), "interval", minutes=10, id="stale_runs")
    sync_connector_jobs()
    logger.info("Worker started; waiting for jobs")

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    sched.shutdown(wait=True)
    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
