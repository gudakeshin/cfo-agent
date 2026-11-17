"""Failure alerts, the watchdog, the monthly AI budget, metrics and backups."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cfo_agent import backup, budgetmeter, ops_alerts, watchdog
from cfo_agent.models import (
    ConnectorConfig,
    Digest,
    ErpCashDaily,
    Job,
    JobWorker,
    OpsAlert,
    PipelineRun,
    SchedulerLease,
    WeightingVersion,
)
from cfo_agent.settings import get_settings


@pytest.fixture()
def sent(monkeypatch):
    """Capture webhook posts; a webhook is configured."""
    calls: list[dict] = []

    class Resp:
        def raise_for_status(self): pass

    def fake_post(url, json=None, timeout=None):
        calls.append({"url": url, "text": json["text"]})
        return Resp()

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example.test/abc")
    get_settings.cache_clear()
    return calls


# ── the alert state machine ─────────────────────────────────────────────────

def test_an_alert_notifies_once_repeats_on_a_timer_and_says_when_it_clears(db_session, sent):
    assert ops_alerts.raise_alert("k1", "critical", "Digest failed", "boom") is True
    assert len(sent) == 1 and "🔴 [CRITICAL] default: Digest failed" in sent[0]["text"] and "boom" in sent[0]["text"]
    assert sent[0]["url"] == "https://hooks.example.test/abc"
    assert ops_alerts.raise_alert("k1", "critical", "Digest failed", "boom") is False  # same condition, no spam
    assert len(sent) == 1
    row = db_session.query(OpsAlert).one()
    row.last_notified_at = datetime.utcnow() - timedelta(hours=13)  # past ALERT_REPEAT_HOURS (12)
    db_session.commit()
    assert ops_alerts.raise_alert("k1", "critical", "Digest failed", "still") is True
    assert len(sent) == 2
    ops_alerts.resolve_alert("k1")
    assert "✅ [RESOLVED]" in sent[-1]["text"] and len(sent) == 3
    assert ops_alerts.open_alerts() == []
    assert ops_alerts.resolve_alert("k1") is False  # already closed: silent


def test_escalation_notifies_immediately(db_session, sent):
    ops_alerts.raise_alert("k", "warn", "Slow", "")
    ops_alerts.raise_alert("k", "warn", "Slow", "")
    assert len(sent) == 1
    ops_alerts.raise_alert("k", "critical", "Down", "")
    assert len(sent) == 2 and "[CRITICAL]" in sent[1]["text"]


def test_a_failed_delivery_is_retried_next_check_and_never_raises(db_session, monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example.test/abc")
    get_settings.cache_clear()
    attempts = []

    def boom(url, json=None, timeout=None):
        attempts.append(1)
        raise ConnectionError("down")

    monkeypatch.setattr("httpx.post", boom)
    ops_alerts.raise_alert("k", "critical", "Down", "")  # must not raise
    assert db_session.query(OpsAlert).one().last_notified_at is None  # so the next check tries again
    ops_alerts.raise_alert("k", "critical", "Down", "")
    assert len(attempts) == 2


def test_without_a_channel_alerts_are_stored_not_sent(db_session, monkeypatch, caplog):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    get_settings.cache_clear()
    ops_alerts.raise_alert("k", "warn", "Heads up", "detail")
    assert [a.key for a in ops_alerts.open_alerts()] == ["k"]


def test_email_channel(db_session, monkeypatch):
    import cfo_agent.digest.email as mail

    got = []
    monkeypatch.setattr(mail, "_send_resend", lambda html, subject, to, text=None: got.append((subject, to, text)) or True)
    monkeypatch.setenv("ALERT_EMAIL_TO", "ops@example.test, oncall@example.test")
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    get_settings.cache_clear()
    ops_alerts.raise_alert("k", "critical", "Digest failed", "boom")
    assert got[0][1] == ["ops@example.test", "oncall@example.test"] and "Digest failed" in got[0][0]


def test_sync_alerts_raises_what_holds_and_resolves_the_rest(db_session, sent):
    ops_alerts.sync_alerts("watchdog:", {"watchdog:a": ("warn", "A", ""), "watchdog:b": ("critical", "B", "")})
    ops_alerts.raise_alert("other:x", "warn", "Unrelated", "")
    ops_alerts.sync_alerts("watchdog:", {"watchdog:b": ("critical", "B", "")})
    keys = {a.key for a in ops_alerts.open_alerts()}
    assert keys == {"watchdog:b", "other:x"}  # a cleared, and the other prefix is left alone


# ── the checks ──────────────────────────────────────────────────────────────

NOW = datetime(2026, 11, 20, 12, 0)


def _wv(s):
    return s.query(WeightingVersion).first()


def test_dead_jobs_and_a_stalled_queue(db_session):
    db_session.add(Job(kind="digest", status="dead", attempts=2, last_error="DB timeout", finished_at=NOW - timedelta(hours=2)))
    db_session.add(Job(kind="news_refresh", status="dead", attempts=2, last_error="x", finished_at=NOW - timedelta(hours=30)))  # old
    db_session.add(Job(kind="digest", status="queued", run_after=NOW - timedelta(minutes=10)))
    db_session.commit()
    a = watchdog.run_checks(db_session, NOW)
    assert a["watchdog:job_dead:digest"][0] == "critical" and "DB timeout" in a["watchdog:job_dead:digest"][2]
    assert "watchdog:job_dead:news_refresh" not in a  # more than a day ago
    assert a["watchdog:queue_stalled"][0] == "critical"
    db_session.add(JobWorker(id="w1", threads=2, heartbeat_at=NOW - timedelta(seconds=30)))
    db_session.commit()
    assert "watchdog:queue_stalled" not in watchdog.run_checks(db_session, NOW)  # someone is there to run it


def test_failed_digest_and_missing_monday_digest(db_session, monkeypatch):
    wv = _wv(db_session)
    db_session.add(PipelineRun(week="2026-W46", weighting_version_id=wv.id, status="failed", error="citation gate crashed",
                               created_at=NOW - timedelta(days=1)))
    db_session.commit()
    a = watchdog.run_checks(db_session, NOW)
    assert a["watchdog:digest_failed"][0] == "critical" and "crashed" in a["watchdog:digest_failed"][2]
    assert "watchdog:digest_late" not in a  # replay/demo mode: digests are manual
    monkeypatch.setenv("DATA_MODE", "live")
    get_settings.cache_clear()
    db_session.add(Digest(week="2026-W44", weighting_version_id=wv.id, headline="h", created_at=NOW - timedelta(days=12)))
    db_session.commit()
    assert watchdog.run_checks(db_session, NOW)["watchdog:digest_late"][0] == "critical"


def test_stale_data_and_failing_connectors_in_live_mode(db_session, monkeypatch):
    monkeypatch.setenv("DATA_MODE", "live")
    get_settings.cache_clear()
    db_session.add(ErpCashDaily(business_key="C1", as_of_date=(NOW - timedelta(days=6)).date(), account_key="P", balance_paise=1))
    db_session.add(ConnectorConfig(id="csv_drop", enabled=True, last_status="error"))
    db_session.commit()
    a = watchdog.run_checks(db_session, NOW)
    assert a["watchdog:data_stale"][0] == "warn" and "6 days" in a["watchdog:data_stale"][1]
    assert "watchdog:connector:csv_drop" in a


def test_a_lapsed_scheduler_lease_but_not_a_missing_one(db_session):
    assert "watchdog:scheduler_down" not in watchdog.run_checks(db_session, NOW)  # never started here
    db_session.add(SchedulerLease(name="main", owner="w", expires_at=datetime.utcnow() - timedelta(minutes=30),
                                  heartbeat_at=datetime.utcnow() - timedelta(minutes=33), job_runs_json={}))
    db_session.commit()
    assert watchdog.run_checks(db_session, datetime.utcnow())["watchdog:scheduler_down"][0] == "critical"


def test_one_broken_check_does_not_blind_the_others(db_session, monkeypatch):
    def broken(s, now):
        raise RuntimeError("bug")

    db_session.add(Job(kind="digest", status="dead", attempts=1, last_error="x", finished_at=NOW))
    db_session.commit()
    monkeypatch.setattr(watchdog, "CHECKS", (broken, watchdog.check_jobs))
    assert "watchdog:job_dead:digest" in watchdog.run_checks(db_session, NOW)


def test_the_watchdog_job_alerts_then_clears(db_session, sent):
    db_session.add(Job(kind="digest", status="dead", attempts=2, last_error="boom", finished_at=datetime.utcnow()))
    db_session.commit()
    watchdog.watchdog_job()
    watchdog.watchdog_job()
    assert len([c for c in sent if "CRITICAL" in c["text"]]) == 1  # once, not every five minutes
    db_session.query(Job).update({"status": "queued", "run_after": datetime.utcnow()})  # retried by hand
    db_session.add(JobWorker(id="w", threads=1, heartbeat_at=datetime.utcnow()))
    db_session.commit()
    watchdog.watchdog_job()
    assert "[RESOLVED]" in sent[-1]["text"]


def test_a_job_that_goes_dead_alerts_at_once(db_session, sent):
    from cfo_agent import jobqueue

    jid, _ = jobqueue.enqueue("digest", max_attempts=1)
    jobqueue.claim_next("w", max_running=1)
    jobqueue.fail(jid, "boom")
    assert "Background job 'digest' failed after 1 attempt" in sent[0]["text"]


# ── monthly AI budget ───────────────────────────────────────────────────────

def _spend(s, usd, when):
    wv = _wv(s)
    s.add(PipelineRun(week="2026-W40", weighting_version_id=wv.id, status="completed", cost_usd=usd, created_at=when))
    s.commit()


def test_spend_and_remaining(db_session, monkeypatch):
    monkeypatch.setenv("MONTHLY_LLM_BUDGET_USD", "10")
    get_settings.cache_clear()
    now = datetime(2026, 11, 20, 12, 0)
    _spend(db_session, 4.0, datetime(2026, 11, 3))
    _spend(db_session, 99.0, datetime(2026, 10, 30))  # last month: not counted
    assert budgetmeter.spend_this_month(db_session, now)["total"] == 4.0
    assert budgetmeter.remaining_usd(db_session) is not None


def test_no_ceiling_means_no_cap(db_session):
    assert budgetmeter.remaining_usd(db_session) is None


def test_the_ceiling_caps_a_run_and_pauses_chat(db_session, monkeypatch):
    from cfo_agent import chat, llm

    monkeypatch.setenv("MONTHLY_LLM_BUDGET_USD", "3")
    monkeypatch.setenv("MAX_LLM_COST_USD_PER_RUN", "5")
    get_settings.cache_clear()
    _spend(db_session, 2.0, datetime.utcnow())
    assert llm.budget_for_items(5).max_cost_usd == pytest.approx(1.0)  # what is left, not the per-run 5
    _spend(db_session, 2.0, datetime.utcnow())
    assert llm.budget_for_items(5).max_cost_usd == 0  # used up: the run falls back to deterministic text
    monkeypatch.setattr(llm, "stream_round", lambda **k: pytest.fail("the model must not be called"))
    events = []
    text, budget = chat.converse(db_session, "sys", [{"role": "user", "content": "hi"}], None, "2026-W40",
                                 lambda name, data: events.append((name, data)))
    assert "AI budget for the month" in text and events and budget.calls == 0


def test_budget_is_a_per_tenant_override():
    from cfo_agent.registry import clean_overrides

    assert clean_overrides({"monthly_llm_budget_usd": "25"}) == {"monthly_llm_budget_usd": 25.0}


# ── metrics ─────────────────────────────────────────────────────────────────

def test_metrics_endpoint_needs_its_token(db_session, monkeypatch):
    from cfo_agent.main import app

    with TestClient(app) as c:
        assert c.get("/metrics").status_code == 404  # off by default
        monkeypatch.setenv("METRICS_TOKEN", "s3cret-token")
        get_settings.cache_clear()
        assert c.get("/metrics").status_code == 401
        assert c.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
        db_session.add(Job(kind="digest", status="queued"))
        db_session.commit()
        ops_alerts.raise_alert("watchdog:x", "critical", "X", "")
        r = c.get("/metrics", headers={"Authorization": "Bearer s3cret-token"})
    assert r.status_code == 200
    body = r.text
    assert 'cfo_jobs{tenant="default",status="queued"} 1' in body
    assert 'cfo_alerts_open{tenant="default",severity="critical"} 1' in body
    assert 'cfo_workers_alive{tenant="default"} 0' in body
    assert "# TYPE cfo_ai_spend_month_usd gauge" in body


def test_ops_alert_view(db_session, sent):
    from cfo_agent.api import api_router

    ops_alerts.raise_alert("k", "warn", "Slow feed", "x")
    app = FastAPI()
    app.include_router(api_router)
    with TestClient(app) as c:
        body = c.get("/api/ops/alerts").json()
    assert body["alerts"][0]["title"] == "Slow feed" and body["channels"] == {"webhook": True, "email": False}
    assert body["backup"]["enabled"] is False


# ── backups ─────────────────────────────────────────────────────────────────

def test_backup_is_taken_checked_and_pruned(db_session, tmp_path):
    db_session.add(Digest(week="2026-W40", weighting_version_id=_wv(db_session).id, headline="kept"))
    db_session.commit()
    paths = []
    for _ in range(3):
        paths.append(backup.backup_current_tenant(tmp_path, keep=2))
        import time; time.sleep(1.1)  # file names carry the second
    left = sorted((tmp_path / "default").glob("*.sqlite"))
    assert len(left) == 2 and paths[0] not in left  # oldest pruned, with its note
    note = json.loads(left[-1].with_suffix(".sqlite.json").read_text())
    assert note["verified"] and note["rows"]["digests"] == 1 and len(note["sha256"]) == 64
    assert oct(left[-1].stat().st_mode)[-3:] == "600"  # client data: not world-readable
    import sqlite3
    assert sqlite3.connect(left[-1]).execute("SELECT headline FROM digests").fetchone() == ("kept",)


def test_a_bad_backup_is_caught(db_session, tmp_path):
    good = backup.backup_current_tenant(tmp_path)
    garbage = tmp_path / "broken.sqlite"
    garbage.write_bytes(b"this is not a database" * 100)
    with pytest.raises(backup.BackupError, match="not a readable SQLite"):
        backup.verify_file(garbage)
    # A backup with fewer rows than the live database had when it began is not a backup of it.
    with pytest.raises(backup.BackupError, match="in the live database"):
        backup.verify_file(good, {"digests": 999})


def test_backup_freshness_feeds_the_watchdog(db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    get_settings.cache_clear()
    assert backup.last_backup_age_hours() is None
    assert "watchdog:backup_stale" in watchdog.run_checks(db_session, NOW)
    backup.backup_job()
    assert backup.last_backup_age_hours() < 0.1
    assert "watchdog:backup_stale" not in watchdog.run_checks(db_session, datetime.utcnow())


def test_backup_runs_as_a_queued_job(db_session, tmp_path, monkeypatch):
    from cfo_agent import jobqueue

    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    get_settings.cache_clear()
    jobqueue.enqueue("backup")
    info = jobqueue.claim_next("w", max_running=1)
    assert jobqueue.run_job(info) == "succeeded"
    assert list((tmp_path / "default").glob("*.sqlite"))
