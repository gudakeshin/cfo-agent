"""Scheduler lease (one active worker), ops status, readiness, request IDs, JSON logs."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cfo_agent.api import api_router
from cfo_agent.lease import acquire_or_renew, holds_lease, lease_status


def test_only_one_worker_holds_the_lease(db_session):
    t0 = datetime.utcnow()
    assert acquire_or_renew("worker-a", now=t0)
    assert not acquire_or_renew("worker-b", now=t0 + timedelta(seconds=30))
    assert acquire_or_renew("worker-a", now=t0 + timedelta(seconds=60))  # renew
    # A dies; after the lease lapses B takes over
    assert acquire_or_renew("worker-b", now=t0 + timedelta(minutes=10))
    assert lease_status()["owner"] == "worker-b"
    assert not holds_lease("worker-a")


def test_leased_job_skips_on_standby(db_session):
    from cfo_agent.scheduler import leased

    calls = []
    acquire_or_renew("someone-else")
    leased("t", lambda: calls.append(1))()
    assert calls == []


def test_ops_status_and_ready(seeded_erp, db_session):
    app = FastAPI()
    app.include_router(api_router)
    c = TestClient(app)
    body = c.get("/api/ops/status").json()
    assert {"scheduler", "digest", "connectors", "ai_spend_usd_30d"} <= body.keys()
    assert body["digest"]["on_schedule"] is False  # no digest yet
    assert c.get("/api/ops/ready").json() == {"ready": True}


def test_request_id_header_and_json_logs(db_session, capsys):
    from cfo_agent import main as m
    from cfo_agent.observability import JsonFormatter, RequestIdFilter

    app = FastAPI()
    app.include_router(api_router)
    app.middleware("http")(m.request_context)
    c = TestClient(app)
    r = c.get("/api/metrics", headers={"X-Request-ID": "abc123def456"})
    assert r.headers["X-Request-ID"] == "abc123def456"
    r2 = c.get("/api/metrics", headers={"X-Request-ID": "bad id with spaces!"})
    assert r2.headers["X-Request-ID"] != "bad id with spaces!"

    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    RequestIdFilter().filter(rec)
    out = json.loads(JsonFormatter().format(rec))
    assert out["msg"] == "hello world" and out["level"] == "INFO" and "request_id" in out
