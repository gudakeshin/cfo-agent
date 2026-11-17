"""Durable job queue: claiming, retries, recovery, limits, fairness, and the digest handler."""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest

from cfo_agent import jobqueue
from cfo_agent.jobqueue import HANDLERS, PermanentError, claim_next, complete, enqueue, fail, reap_stale, run_job
from cfo_agent.models import Job


@pytest.fixture()
def handlers():
    added = []

    def add(kind, fn):
        HANDLERS[kind] = fn
        added.append(kind)

    yield add
    for k in added:
        HANDLERS.pop(k, None)


def _job(db_session, jid):
    db_session.expire_all()
    return db_session.get(Job, jid)


def test_enqueue_claim_complete(db_session):
    jid, created = enqueue("t", {"x": 1})
    assert created
    info = claim_next("w1", max_running=2)
    assert (info.id, info.kind, info.payload, info.attempt) == (jid, "t", {"x": 1}, 1)
    assert claim_next("w2", max_running=2) is None  # nothing else is due
    complete(jid, {"ok": True})
    j = _job(db_session, jid)
    assert j.status == "succeeded" and j.result_json == {"ok": True} and j.finished_at and j.locked_by is None


def test_a_dedupe_key_keeps_one_active_job(db_session):
    a, created_a = enqueue("sweep", dedupe_key="sweep")
    b, created_b = enqueue("sweep", dedupe_key="sweep")
    assert (created_a, created_b) == (True, False) and a == b
    info = claim_next("w", max_running=2)
    assert enqueue("sweep", dedupe_key="sweep")[1] is False  # still active while running
    complete(info.id)
    assert enqueue("sweep", dedupe_key="sweep")[1] is True  # a finished job no longer blocks the next one


def test_priority_and_delay(db_session):
    low, _ = enqueue("t", priority=0)
    high, _ = enqueue("t", priority=5)
    later, _ = enqueue("t", priority=9, delay_s=3600)
    assert claim_next("w", max_running=9).id == high
    assert claim_next("w", max_running=9).id == low
    assert claim_next("w", max_running=9) is None  # the delayed one is not due


def test_retry_with_backoff_then_dead_then_manual_retry(db_session):
    jid, _ = enqueue("t", max_attempts=2)
    claim_next("w", max_running=1)
    assert fail(jid, "boom") == "queued"
    j = _job(db_session, jid)
    assert j.last_error == "boom" and j.run_after > jobqueue._now() + timedelta(seconds=20)  # backed off
    assert claim_next("w", max_running=1) is None  # not due yet
    j.run_after = jobqueue._now()
    db_session.commit()
    info = claim_next("w", max_running=1)
    assert info.attempt == 2
    assert fail(jid, "boom again") == "dead"
    assert _job(db_session, jid).finished_at is not None
    assert jobqueue.retry_dead(jid) is True
    assert _job(db_session, jid).status == "queued" and _job(db_session, jid).attempts == 0
    assert jobqueue.backoff_s(1) == 30 and jobqueue.backoff_s(3) == 120 and jobqueue.backoff_s(20) == 900


def test_handler_outcomes(db_session, handlers):
    handlers("good", lambda p, c: {"echo": p["v"], "attempt": c["attempt"]})

    def flaky(p, c):
        raise ValueError("transient")

    def hopeless(p, c):
        raise PermanentError("bad payload")

    handlers("flaky", flaky)
    handlers("hopeless", hopeless)
    ids = {k: enqueue(k, {"v": 7}, max_attempts=3)[0] for k in ("good", "flaky", "hopeless", "unknown")}
    seen = {}
    while (info := claim_next("w", max_running=9)) is not None:
        seen[info.kind] = run_job(info)
    assert seen == {"good": "succeeded", "flaky": "queued", "hopeless": "dead", "unknown": "dead"}
    assert _job(db_session, ids["good"]).result_json == {"echo": 7, "attempt": 1}
    assert "ValueError: transient" in _job(db_session, ids["flaky"]).last_error
    assert "no handler" in _job(db_session, ids["unknown"]).last_error
    assert _job(db_session, ids["hopeless"]).attempts == 1  # a permanent error is not retried


def test_a_job_whose_worker_died_is_taken_over(db_session):
    jid, _ = enqueue("t", max_attempts=2)
    claim_next("dead-worker", max_running=1)
    j = _job(db_session, jid)
    j.heartbeat_at = jobqueue._now() - timedelta(seconds=600)
    db_session.commit()
    assert reap_stale(180) == 1
    assert _job(db_session, jid).status == "queued"
    _job(db_session, jid).run_after = jobqueue._now()
    db_session.commit()
    info = claim_next("new-worker", max_running=1)
    assert info.id == jid and info.attempt == 2
    # A live job (fresh heartbeat) is left alone.
    assert reap_stale(180) == 0
    # Lost again on the last attempt: dead, not retried forever.
    _job(db_session, jid).heartbeat_at = jobqueue._now() - timedelta(seconds=600)
    db_session.commit()
    reap_stale(180)
    assert _job(db_session, jid).status == "dead"


def test_a_tenant_never_exceeds_its_running_limit(db_session):
    a, _ = enqueue("t")
    b, _ = enqueue("t")
    first = claim_next("w", max_running=1)
    assert claim_next("w2", max_running=1) is None  # at the limit, even for a different worker
    complete(first.id)
    assert claim_next("w2", max_running=1).id == b


def test_racing_workers_never_claim_the_same_job(db_session):
    n = 12
    for _ in range(n):
        enqueue("t")
    claimed: list[int] = []
    lock = threading.Lock()

    def worker(name):
        while (info := claim_next(name, max_running=100)) is not None:
            with lock:
                claimed.append(info.id)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(claimed) == sorted(set(claimed)) and len(claimed) == n


# ── the executor ────────────────────────────────────────────────────────────

def _wait(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_executor_runs_jobs_and_survives_a_crashed_predecessor(db_session, handlers):
    from cfo_agent.jobrunner import Executor

    done = []
    handlers("work", lambda p, c: done.append(p["n"]))
    # A job left 'running' by a worker that died before this executor existed.
    stranded, _ = enqueue("work", {"n": 0}, max_attempts=3)
    claim_next("crashed-worker", max_running=1)
    j = _job(db_session, stranded)
    j.heartbeat_at = jobqueue._now() - timedelta(seconds=900)
    db_session.commit()
    for n in (1, 2, 3):
        enqueue("work", {"n": n})

    ex = Executor(threads=2, max_per_tenant=2, poll_s=0.05, stale_after_s=180).start()
    try:
        assert _wait(lambda: sorted(done) == [0, 1, 2, 3])
        ex.drain()
    finally:
        ex.stop(timeout=5)
    db_session.expire_all()
    assert {j.status for j in db_session.query(Job)} == {"succeeded"}
    assert db_session.get(Job, stranded).attempts == 2  # picked up again after its worker was lost


def test_executor_serves_tenants_fairly(tmp_path, monkeypatch, handlers):
    """A tenant with a long backlog does not make another tenant wait for all of it."""
    import cfo_agent.db as dbmod
    from cfo_agent.db import init_db
    from cfo_agent.jobrunner import Executor
    from cfo_agent.settings import get_settings
    from cfo_agent.tenancy import _load, current_tenant_id, tenant_context

    cfg = tmp_path / "tenants.yaml"
    cfg.write_text(f"""
tenants:
  - id: busy
    database_url: sqlite:///{tmp_path / 'busy.db'}
    hosts: [busy.local]
  - id: quiet
    database_url: sqlite:///{tmp_path / 'quiet.db'}
    hosts: [quiet.local]
""")
    monkeypatch.setenv("TENANTS_FILE", str(cfg))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'default.db'}")
    get_settings.cache_clear()
    _load.cache_clear()
    dbmod._engine = dbmod._SessionLocal = None
    dbmod._tenant_engines.clear()
    dbmod._tenant_urls.clear()
    try:
        init_db()
        order: list[str] = []
        handlers("w", lambda p, c: (order.append(current_tenant_id()), time.sleep(0.05)))
        with tenant_context("busy"):
            for _ in range(8):
                enqueue("w")
        with tenant_context("quiet"):
            enqueue("w")
        ex = Executor(threads=1, max_per_tenant=1, poll_s=0.02).start()
        try:
            assert _wait(lambda: len(order) == 9, timeout=20)
        finally:
            ex.stop(timeout=5)
        assert order.index("quiet") <= 2, order  # not stuck behind all eight of the other tenant's jobs
    finally:
        for eng, _ in dbmod._tenant_engines.values():
            eng.dispose()
        dbmod._tenant_engines.clear()
        dbmod._tenant_urls.clear()
        monkeypatch.delenv("TENANTS_FILE")
        get_settings.cache_clear()
        _load.cache_clear()


# ── the digest job ──────────────────────────────────────────────────────────

@pytest.fixture()
def quiet_pipeline(monkeypatch):
    from cfo_agent.settings import get_settings

    monkeypatch.setattr("cfo_agent.pipeline.orchestrator.archival_insert", lambda *a, **k: None)
    monkeypatch.setattr("cfo_agent.pipeline.orchestrator.has_unconsolidated_activity", lambda s: False)
    monkeypatch.setattr("cfo_agent.pipeline.orchestrator.send_digest_email", lambda s, d: d)
    monkeypatch.setattr("cfo_agent.pipeline.step5_synthesize.archival_search", lambda *a, **k: [])
    monkeypatch.setenv("MAX_LLM_COST_USD_PER_RUN", "0")
    get_settings.cache_clear()


def _run_all():
    statuses = []
    while (info := claim_next("w", max_running=9)) is not None:
        statuses.append(run_job(info))
    return statuses


def test_a_manual_digest_run_goes_through_the_queue(seeded_erp, db_session, quiet_pipeline, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from cfo_agent.api import api_router
    from cfo_agent.models import PipelineRun
    from cfo_agent.settings import get_settings

    monkeypatch.setenv("JOB_MODE", "queue")
    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(api_router)
    week = seeded_erp["weeks"][-1]
    with TestClient(app) as c:
        r = c.post("/api/digests/generate", json={"week": week}, headers={"X-Requested-With": "fetch"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending" and body["job_id"]
    assert db_session.get(PipelineRun, body["run_id"]).status == "pending"  # reserved, not yet run
    assert _run_all() == ["succeeded"]
    db_session.expire_all()
    assert db_session.get(PipelineRun, body["run_id"]).status == "completed"
    assert _job(db_session, body["job_id"]).result_json == {"run_id": body["run_id"], "status": "completed"}


def test_digest_retry_closes_out_a_run_the_lost_worker_left_behind(seeded_erp, db_session, quiet_pipeline):
    from cfo_agent.models import Digest, PipelineRun
    from cfo_agent.pipeline.orchestrator import reserve_week
    from cfo_agent.weighting import get_active_weighting

    week = seeded_erp["weeks"][-1]
    wv = get_active_weighting(db_session)
    stuck = reserve_week(db_session, week, wv.id)
    stuck.status = "running"  # the previous attempt's worker died mid-run
    db_session.commit()
    from cfo_agent.pipeline import orchestrator

    orchestrator._week_locks.clear()  # its in-memory week lock died with that process
    jid, _ = enqueue("digest", {"week": week, "weighting_version_id": wv.id, "run_id": stuck.id}, max_attempts=2)
    first = claim_next("w", max_running=1)
    assert run_job(first) == "succeeded"  # attempt 1 sees a live-looking run and does not touch it
    assert _job(db_session, jid).result_json["skipped"].startswith("run ")
    # Attempt 2 (after the lost worker's heartbeat expired): close the stuck run, run afresh.
    j = _job(db_session, jid)
    j.status, j.attempts, j.run_after = "queued", 1, jobqueue._now()
    db_session.commit()
    assert run_job(claim_next("w", max_running=1)) == "succeeded"
    db_session.expire_all()
    assert db_session.get(PipelineRun, stuck.id).status == "failed"
    assert db_session.query(PipelineRun).filter_by(week=week, status="completed").count() == 1
    assert db_session.query(Digest).filter_by(week=week).count() == 1


def test_a_finished_digest_is_not_run_twice(seeded_erp, db_session, quiet_pipeline):
    from cfo_agent.pipeline.orchestrator import reserve_week, run_pipeline
    from cfo_agent.models import Digest
    from cfo_agent.weighting import get_active_weighting

    week = seeded_erp["weeks"][-1]
    wv = get_active_weighting(db_session)
    run = reserve_week(db_session, week, wv.id)
    run_pipeline(run.id, week, wv.id)
    enqueue("digest", {"week": week, "weighting_version_id": wv.id, "run_id": run.id})
    assert _run_all() == ["succeeded"]
    db_session.expire_all()
    assert db_session.query(Digest).filter_by(week=week).count() == 1


def test_scheduled_triggers_only_enqueue_and_do_not_pile_up(db_session):
    from cfo_agent import job_handlers as jh

    for _ in range(3):
        jh.enqueue_digest()
        jh.enqueue_redline_sweep()
        jh.enqueue_connector_sync("csv_drop")
    kinds = sorted(j.kind for j in db_session.query(Job))
    assert kinds == ["connector_sync", "digest", "redline_sweep"]


def test_ops_view_reports_a_stalled_queue(db_session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from cfo_agent.api import api_router

    app = FastAPI()
    app.include_router(api_router)
    jid, _ = enqueue("t")
    j = _job(db_session, jid)
    j.run_after = jobqueue._now() - timedelta(seconds=600)
    db_session.commit()
    with TestClient(app) as c:
        body = c.get("/api/ops/jobs").json()
        assert body["counts"]["queued"] == 1 and body["workers"] == [] and body["stalled"] is True
        # A dead job can be retried from the ops view.
        claim_next("w", max_running=1)
        fail(jid, "x", retryable=False)
        assert c.post(f"/api/ops/jobs/{jid}/retry", headers={"X-Requested-With": "fetch"}).json()["status"] == "queued"
        assert c.post(f"/api/ops/jobs/{jid}/retry", headers={"X-Requested-With": "fetch"}).status_code == 409
