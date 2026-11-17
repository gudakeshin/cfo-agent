"""Job executor: claims jobs from every tenant's queue and runs them on a thread pool.

Any number of executors can run (in `python -m cfo_agent.worker`, or embedded in the API
process when RUN_SCHEDULER=true). They share nothing but the databases: each tenant's
queue is polled round-robin, one claim per tenant per pass, so a tenant with a long
backlog cannot starve the others, and a tenant never exceeds JOB_MAX_PER_TENANT running
jobs. A heartbeat keeps running jobs alive; one that outlives JOB_MAX_RUNTIME_S stops
being heartbeated and is retried (a Python thread cannot be killed, so the stuck thread
is abandoned, not stopped)."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from cfo_agent import jobqueue
from cfo_agent.db import get_session
from cfo_agent.models import JobWorker
from cfo_agent.settings import global_settings
from cfo_agent.tenancy import is_multi_tenant, tenant_context, tenants

logger = logging.getLogger(__name__)

HEARTBEAT_S = 15
REAP_EVERY_S = 30
PRESENCE_EVERY_S = 20
MAX_RUNTIME_S = 3600


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Executor:
    def __init__(self, *, threads: int | None = None, max_per_tenant: int | None = None,
                 poll_s: float | None = None, stale_after_s: float | None = None, max_runtime_s: float = MAX_RUNTIME_S):
        g = global_settings()
        self.threads = threads or g.job_worker_threads
        self.max_per_tenant = max_per_tenant or g.job_max_per_tenant
        self.poll_s = g.job_poll_s if poll_s is None else poll_s
        self.stale_after_s = stale_after_s or g.job_stale_after_s
        self.max_runtime_s = max_runtime_s
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self._pool = ThreadPoolExecutor(max_workers=self.threads, thread_name_prefix="job")
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._inflight: dict[tuple[str | None, int], float] = {}  # (tenant, job id) -> started (monotonic)
        self._threads: list[threading.Thread] = []
        self._last_served: str | None = None  # tenant that got the previous claim
        self._last_reap = 0.0
        self._last_presence = 0.0
        self._started_at = _now()

    # ── lifecycle ──
    def start(self) -> "Executor":
        for name, target in (("loop", self._loop), ("heartbeat", self._heartbeat_loop)):
            t = threading.Thread(target=target, name=f"job-{name}", daemon=True)
            t.start()
            self._threads.append(t)
        logger.info("Job executor %s started (%d threads, %d per tenant)", self.worker_id, self.threads, self.max_per_tenant)
        return self

    def stop(self, timeout: float = 30.0) -> None:
        """Stop claiming; give running jobs `timeout` seconds to finish. Anything still running
        is left for another worker, which takes it over once its heartbeat goes stale."""
        self._stop.set()
        deadline = time.monotonic() + timeout
        while self._inflight and time.monotonic() < deadline:
            time.sleep(0.1)
        self._pool.shutdown(wait=False, cancel_futures=True)
        for t in self._threads:
            t.join(timeout=2)
        logger.info("Job executor %s stopped (%d job(s) abandoned)", self.worker_id, len(self._inflight))

    # ── tenants ──
    def _tenant_ids(self) -> list[str | None]:
        return list(tenants()) if is_multi_tenant() else [None]

    def _in_tenant(self, tid: str | None):
        if tid is None:
            from contextlib import nullcontext

            return nullcontext()
        if global_settings().control_database_url:
            from cfo_agent.tenant_prepare import ensure_ready

            ensure_ready(tenants()[tid])  # a tenant added since startup has no queue table yet
        return tenant_context(tid)

    # ── main loop ──
    def _capacity(self) -> int:
        with self._lock:
            return self.threads - len(self._inflight)

    def _pass(self) -> int:
        """One round-robin pass: at most one claim per tenant. Returns how many jobs started."""
        ids = self._tenant_ids()
        if not ids:
            return 0
        started = 0
        n = len(ids)
        # Start after the tenant served last, so every tenant gets its turn however often we poll.
        first = (ids.index(self._last_served) + 1) % n if self._last_served in ids else 0
        for k in range(n):
            if self._capacity() <= 0 or self._stop.is_set():
                break
            tid = ids[(first + k) % n]
            try:
                with self._in_tenant(tid):
                    info = jobqueue.claim_next(self.worker_id, max_running=self.max_per_tenant)
            except Exception:  # noqa: BLE001 — one tenant's database being down must not stop the rest
                logger.exception("Could not claim a job for tenant %s", tid)
                continue
            if info is None:
                continue
            with self._lock:
                self._inflight[(tid, info.id)] = time.monotonic()
            self._pool.submit(self._run, tid, info)
            self._last_served = tid
            started += 1
        return started

    def _run(self, tid: str | None, info: jobqueue.JobInfo) -> None:
        try:
            with self._in_tenant(tid):
                status = jobqueue.run_job(info)
            logger.info("Job %s (%s) tenant=%s attempt=%d -> %s", info.id, info.kind, tid, info.attempt, status)
        except Exception:  # noqa: BLE001
            logger.exception("Job %s crashed the runner", info.id)
        finally:
            with self._lock:
                self._inflight.pop((tid, info.id), None)

    def _housekeeping(self) -> None:
        now = time.monotonic()
        if now - self._last_reap >= REAP_EVERY_S:
            self._last_reap = now
            for tid in self._tenant_ids():
                try:
                    with self._in_tenant(tid):
                        jobqueue.reap_stale(self.stale_after_s)
                except Exception:  # noqa: BLE001
                    logger.exception("Stale-job check failed for tenant %s", tid)
        if now - self._last_presence >= PRESENCE_EVERY_S:
            self._last_presence = now
            for tid in self._tenant_ids():
                try:
                    with self._in_tenant(tid):
                        self._note_presence()
                except Exception:  # noqa: BLE001
                    logger.exception("Worker presence update failed for tenant %s", tid)

    def _note_presence(self) -> None:
        s = get_session()
        try:
            row = s.get(JobWorker, self.worker_id)
            if row is None:
                s.add(JobWorker(id=self.worker_id, threads=self.threads, started_at=self._started_at, heartbeat_at=_now()))
            else:
                row.heartbeat_at = _now()
            s.commit()
        finally:
            s.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._housekeeping()
                started = self._pass()
            except Exception:  # noqa: BLE001
                logger.exception("Job loop error")
                started = 0
            # Keep claiming straight away while there is work and room; otherwise wait a poll.
            if not started:
                self._stop.wait(self.poll_s)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_S):
            with self._lock:
                alive = [(t, j) for (t, j), t0 in self._inflight.items() if time.monotonic() - t0 < self.max_runtime_s]
            by_tenant: dict[str | None, list[int]] = {}
            for t, j in alive:
                by_tenant.setdefault(t, []).append(j)
            for tid, ids in by_tenant.items():
                try:
                    with self._in_tenant(tid):
                        jobqueue.heartbeat(ids, self.worker_id)
                except Exception:  # noqa: BLE001
                    logger.exception("Heartbeat failed for tenant %s", tid)

    # ── test helper ──
    def drain(self, timeout: float = 20.0) -> None:
        """Wait until nothing is running (used by tests and shutdown)."""
        deadline = time.monotonic() + timeout
        while self._inflight and time.monotonic() < deadline:
            time.sleep(0.05)
