"""Tier-0 hardening: LLM budget, citation gate, red-line guarantee, scheduler
week selection, live-mode sweep, run reservation. Deterministic — the LLM is
replaced by a fake that respects the budget like the real wrapper."""

from __future__ import annotations

import json
import re

import pytest

from cfo_agent.citations import unverified_figures
from cfo_agent.digest.builder import persist_digest
from cfo_agent.kra import get_active_framework, upsert_kra_version
from cfo_agent.llm import BudgetExhausted, LlmBudget, budget_for_items
from cfo_agent.models import Digest, DigestItem, ErpCustomer, PipelineRun
from cfo_agent.pipeline.schemas import (
    ConnectOutput,
    HeadlineOutput,
    SectionBlock,
    SynthesizeItemOutput,
)
from cfo_agent.pipeline.step1_map import step1_map
from cfo_agent.pipeline.step2_weight import step2_weight
from cfo_agent.pipeline.step4_filter import step4_filter
from cfo_agent.pipeline.step6_surface import step6_surface
from cfo_agent.redlines import set_replay_clock_week
from cfo_agent.settings import get_settings
from cfo_agent.signals import compute_covenant_headroom, directional_severity
from cfo_agent.weighting import get_active_weighting, load_preset

ALWAYS_RED = {"id": "RL-T", "type": "level", "metric": "cash_balance_cr",
              "operator": "gt", "threshold": 0.0, "description": "Test red line"}


# ── fake LLM ────────────────────────────────────────────────────────────────

def _allowed(user: str) -> list[str]:
    m = re.search(r"ALLOWED_CITATIONS: (\[.*?\])\n", user)
    return json.loads(m.group(1)) if m else []


def make_fake_parse(mode: str = "good", forged: str | None = None):
    def fake_parse(schema, *, system, user, purpose, budget=None, **_):
        if budget is not None:
            budget.check()
            budget.calls += 1
        if schema is ConnectOutput:
            return ConnectOutput(connections=[])
        if schema is HeadlineOutput:
            return HeadlineOutput(headline="Test headline")
        cites = _allowed(user)[:1]
        if mode == "forge" and forged:
            cites = [forged]
        return SynthesizeItemOutput(
            title="Item",
            what=SectionBlock(text="What happened.", citations=cites),
            why=SectionBlock(text="Why it matters.", citations=cites),
            connects=SectionBlock(text="", citations=[]),
            action=SectionBlock(text="Do something.", citations=cites),
        )

    return fake_parse


@pytest.fixture()
def hermetic(monkeypatch):
    """Stub everything in run_pipeline that would touch the network or disk."""
    monkeypatch.setattr("cfo_agent.pipeline.step5_synthesize.archival_search", lambda *a, **k: [])
    monkeypatch.setattr("cfo_agent.pipeline.orchestrator.archival_insert", lambda *a, **k: None)
    monkeypatch.setattr("cfo_agent.pipeline.orchestrator.has_unconsolidated_activity", lambda s: False)
    monkeypatch.setattr("cfo_agent.pipeline.orchestrator.send_digest_email", lambda s, d: d)

    def use(mode="good", forged=None):
        fp = make_fake_parse(mode, forged)
        monkeypatch.setattr("cfo_agent.pipeline.step3_connect.parse", fp)
        monkeypatch.setattr("cfo_agent.pipeline.step5_synthesize.parse", fp)

    use()
    return use


def _run(db_session, week):
    from cfo_agent.pipeline.orchestrator import reserve_week, run_pipeline

    wv = get_active_weighting(db_session)
    run = reserve_week(db_session, week, wv.id)
    run_pipeline(run.id, week, wv.id)
    db_session.expire_all()
    return db_session.get(PipelineRun, run.id)


# ── budget ──────────────────────────────────────────────────────────────────

def test_auto_budget_covers_full_digest():
    b = budget_for_items(7)
    # connect + 7×(synth+repair) + headline
    assert b.max_calls == 16


def test_budget_check_raises_on_cost_cap():
    b = LlmBudget(max_calls=10, max_cost_usd=0.01)
    from cfo_agent.llm import CallLog

    b.logs.append(CallLog(purpose="x", cost_usd=0.02))
    with pytest.raises(BudgetExhausted):
        b.check()


def test_full_week_completes_with_snapshots(seeded_erp, db_session, hermetic):
    run = _run(db_session, seeded_erp["weeks"][-1])
    assert run.status == "completed", run.error
    assert not (run.token_usage_json or {}).get("degraded_steps")
    items = db_session.query(DigestItem).filter_by(digest_id=run.digest_id).all()
    assert items
    for it in items:
        assert it.evidence_snapshot_json, "cited records must be frozen into the digest"
        for cid in {c for v in it.citations_json.values() for c in v}:
            assert cid in it.evidence_snapshot_json


def test_budget_exhaustion_degrades_instead_of_failing(seeded_erp, db_session, hermetic, monkeypatch):
    monkeypatch.setenv("MAX_LLM_CALLS_PER_RUN", "2")
    get_settings.cache_clear()
    run = _run(db_session, seeded_erp["weeks"][-1])
    assert run.status == "completed", run.error
    degraded = run.token_usage_json.get("degraded_steps") or []
    assert "synthesize_item" in degraded and "headline" in degraded
    digest = db_session.get(Digest, run.digest_id)
    assert digest.items, "deterministic fallback must still produce cited items"


def test_failed_week_is_not_blocked(seeded_erp, db_session):
    from cfo_agent.main import _recover_stale_runs
    from cfo_agent.pipeline.orchestrator import WeekBusy, _week_locks, reserve_week

    week = seeded_erp["weeks"][-2]
    wv = get_active_weighting(db_session)
    run = reserve_week(db_session, week, wv.id)
    with pytest.raises(WeekBusy):
        reserve_week(db_session, week, wv.id)  # in-process lock
    _week_locks.pop(week, None)
    with pytest.raises(WeekBusy):
        reserve_week(db_session, week, wv.id)  # pending row in DB
    from datetime import timedelta

    _recover_stale_runs(db_session, older_than=timedelta(0))  # simulated restart, run now stale
    db_session.commit()
    assert db_session.get(PipelineRun, run.id).status == "failed"
    again = reserve_week(db_session, week, wv.id)
    _week_locks.pop(week, None)
    assert again.id != run.id


# ── citation gate ──────────────────────────────────────────────────────────

def _items(db_session, week, weighting):
    fw = get_active_framework()
    mapped = step1_map(db_session, week)
    db_session.commit()
    weighted = step2_weight(mapped, weighting, fw)
    items, _ = step4_filter(db_session, weighted, [], weighting, week)
    return items


def _plain_item(db_session, week):
    """A non-red-line item built straight from the top weighted signal (no bar)."""
    from cfo_agent.pipeline.step4_filter import FilterItem

    fw = get_active_framework()
    mapped = step1_map(db_session, week)
    db_session.commit()
    w = step2_weight(mapped, load_preset("acquisition"), fw)[0]
    return FilterItem(weighted=w, is_red_line=False, priority_score=w.priority_score,
                      kra_ids=w.mapped.kra_ids, signal_ids=[w.mapped.signal.signal_id],
                      score_decomp=w.score_decomp)


def _syn(what_cites, why_cites):
    return SynthesizeItemOutput(
        title="t",
        what=SectionBlock(text="x", citations=what_cites),
        why=SectionBlock(text="y", citations=why_cites),
        connects=SectionBlock(text="", citations=[]),
        action=SectionBlock(text="z", citations=[]),
    )


def test_gate_rejects_real_but_unallowed_citation(seeded_erp, db_session):
    item = _plain_item(db_session, seeded_erp["weeks"][-1])
    cust = db_session.query(ErpCustomer).first()
    forged = f"erp:erp_customers:{cust.business_key}"  # resolves, but not this item's evidence
    surfaced, suppressed = step6_surface(db_session, [(item, _syn([forged], [forged]))])
    assert not surfaced
    assert suppressed and forged in suppressed[0].bad_citations


def test_gate_requires_citations_on_factual_sections(seeded_erp, db_session):
    item = _plain_item(db_session, seeded_erp["weeks"][-1])
    good = item.weighted.mapped.signal.signal_id
    surfaced, suppressed = step6_surface(db_session, [(item, _syn([good], []))])
    assert not surfaced and suppressed[0].reason == "no_citations"


def test_red_line_survives_failed_gate_with_fallback(seeded_erp, db_session):
    weighting = dict(load_preset("acquisition"), red_lines=[ALWAYS_RED])
    item = next(i for i in _items(db_session, seeded_erp["weeks"][-1], weighting) if i.is_red_line)
    surfaced, _ = step6_surface(db_session, [(item, _syn(["erp:erp_sales_invoices:NOPE"], []))])
    assert len(surfaced) == 1
    assert surfaced[0].verification["fallback"] is True
    assert surfaced[0].filter_item.is_red_line


def test_unverified_figures():
    known = [47.25, 2.5e10]  # 47.25 days; ₹25 Cr in paise (1 Cr = 1e9 paise)
    from cfo_agent.citations import known_figures

    k = known_figures(known)
    assert unverified_figures(["DSO rose to 47.3 days, AR ₹25 Cr in 2026-W27"], k) == []
    assert unverified_figures(["Exposure of ₹38.4 Cr"], k) == ["38.4"]


# ── metrics ────────────────────────────────────────────────────────────────

def test_directional_severity():
    assert directional_severity("dso_days", +5, 2.0) == 2.0
    assert directional_severity("dso_days", -5, 2.0) == 1.0
    assert directional_severity("cash_balance_cr", -1, 2.0) == 2.0
    assert directional_severity("dpo_days", -1, 2.0) == 2.0


def test_covenant_headroom_reads_covenant_register(seeded_erp, db_session):
    mr = compute_covenant_headroom(db_session, seeded_erp["weeks"][-1])
    assert any(c.startswith("erp:erp_covenants:") for c in mr.lineage)


# ── scheduler ──────────────────────────────────────────────────────────────

def test_digest_week_replay_advances_from_clock(seeded_erp, db_session):
    from cfo_agent.scheduler import select_digest_week

    weeks = seeded_erp["weeks"]
    wv = get_active_weighting(db_session)
    set_replay_clock_week(db_session, weeks[2])
    db_session.commit()
    assert select_digest_week(db_session, wv.id) == weeks[3]

    run = PipelineRun(week=weeks[3], weighting_version_id=wv.id, status="completed")
    db_session.add(run)
    db_session.flush()
    persist_digest(db_session, week=weeks[3], weighting=wv, kra_version=upsert_kra_version(db_session),
                   run=run, headline="h", surfaced=[], empty_bar=True, data_health={})
    db_session.commit()
    assert select_digest_week(db_session, wv.id) is None  # never back-fills


def test_digest_week_live_is_latest(seeded_erp, db_session, monkeypatch):
    from cfo_agent.scheduler import select_digest_week

    monkeypatch.setenv("DATA_MODE", "live")
    get_settings.cache_clear()
    wv = get_active_weighting(db_session)
    assert select_digest_week(db_session, wv.id) == seeded_erp["weeks"][-1]


def test_redline_sweep_fires_in_live_mode_without_clock(seeded_erp, db_session, monkeypatch):
    import cfo_agent.scheduler as sched

    monkeypatch.setenv("DATA_MODE", "live")
    get_settings.cache_clear()

    class _WV:
        content_json = {"red_lines": [ALWAYS_RED]}

    monkeypatch.setattr(sched, "get_active_weighting", lambda s: _WV())
    monkeypatch.setattr(sched, "send_alert_email", lambda s, a: a)

    def no_llm(*a, **k):
        raise RuntimeError("no LLM in tests")

    monkeypatch.setattr("cfo_agent.llm.parse", no_llm)
    alert_ids = sched.job_redline_sweep()
    assert alert_ids, "live mode must evaluate red lines without a replay clock"
