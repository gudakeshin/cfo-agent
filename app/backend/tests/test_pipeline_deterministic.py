"""Deterministic weighting, redlines, citations tests."""

from datetime import date, timedelta

from cfo_agent.citations import gate_citations, resolve_citation
from cfo_agent.kra import get_active_framework
from cfo_agent.pipeline.step1_map import step1_map
from cfo_agent.pipeline.step2_weight import step2_weight
from cfo_agent.redlines import (
    evaluate_rules,
    open_or_reuse_episode,
    set_replay_clock_week,
)
from cfo_agent.weighting import load_preset


def test_two_weightings_different_order(seeded_erp, db_session):
    week = seeded_erp["weeks"][-2]  # W27-ish
    fw = get_active_framework()
    mapped = step1_map(db_session, week)
    db_session.commit()
    acq = load_preset("acquisition")
    der = load_preset("derisking")
    w1 = step2_weight(mapped, acq, fw)
    w2 = step2_weight(mapped, der, fw)
    order1 = [x.mapped.signal.metric_id for x in w1[:5]]
    order2 = [x.mapped.signal.metric_id for x in w2[:5]]
    # Scores or order should differ
    scores1 = [x.priority_score for x in w1[:8]]
    scores2 = [x.priority_score for x in w2[:8]]
    assert order1 != order2 or scores1 != scores2


def test_fake_citation_suppressed(seeded_erp, db_session):
    week = seeded_erp["weeks"][-1]
    step1_map(db_session, week)
    db_session.commit()
    ok, bad = gate_citations(
        db_session,
        [f"metric:dso_days:{week}", "erp:erp_sales_invoices:INV-FAKE-99999"],
    )
    assert f"metric:dso_days:{week}" in ok
    assert any("FAKE" in b for b in bad)
    r = resolve_citation(db_session, "erp:erp_sales_invoices:INV-FAKE-99999")
    assert r.ok is False


def test_redline_episode_dedup(seeded_erp, db_session):
    weeks = seeded_erp["weeks"]
    set_replay_clock_week(db_session, weeks[-1])
    db_session.commit()
    rules = load_preset("acquisition")["red_lines"]
    as_of = date.fromisoformat(
        __import__("cfo_agent.timeutil", fromlist=["week_sunday"]).week_sunday(weeks[-1]).isoformat()
    )
    fires = evaluate_rules(db_session, rules, as_of=as_of, last_check_date=None)
    level = [f for f in fires if f.rule_id == "RL-1"]
    assert level, "expected cash red line in final week"
    a1 = open_or_reuse_episode(db_session, level[0])
    db_session.commit()
    assert a1 is not None
    a2 = open_or_reuse_episode(db_session, level[0])
    assert a2 is None  # episode open — no re-alert


def test_replay_clock_hides_future(seeded_erp, db_session):
    weeks = seeded_erp["weeks"]
    # Clock at week before cash dip and GST
    set_replay_clock_week(db_session, weeks[9])
    db_session.commit()
    rules = load_preset("acquisition")["red_lines"]
    from cfo_agent.timeutil import week_sunday

    # Try to evaluate as of final week — should clamp to clock
    fires = evaluate_rules(
        db_session,
        rules,
        as_of=week_sunday(weeks[-1]),
        last_check_date=None,
    )
    # After clamp to weeks[9], cash should still be above 22
    cash_fires = [f for f in fires if f.rule_id == "RL-1"]
    assert cash_fires == []


def test_rl2_event_fires_once_no_refire(seeded_erp, db_session):
    """RL-2 (GST show-cause, S2) fires on first sweep; episode dedup blocks re-alert."""
    from cfo_agent.timeutil import week_monday, week_sunday

    weeks = seeded_erp["weeks"]
    gst_week = weeks[11]  # S2 week index 11
    set_replay_clock_week(db_session, gst_week)
    db_session.commit()
    rules = load_preset("acquisition")["red_lines"]
    as_of = week_sunday(gst_week)

    fires = evaluate_rules(db_session, rules, as_of=as_of, last_check_date=None)
    event = [f for f in fires if f.rule_id == "RL-2"]
    assert event, "expected GST show-cause RL-2 in week 12"

    a1 = open_or_reuse_episode(db_session, event[0])
    db_session.commit()
    assert a1 is not None

    # Second sweep with last_check after notice day — delta empty, and episode open
    fires2 = evaluate_rules(
        db_session,
        rules,
        as_of=as_of,
        last_check_date=week_monday(gst_week),
    )
    event2 = [f for f in fires2 if f.rule_id == "RL-2"]
    assert event2 == [], "RL-2 must not re-fire on standing notice after last_check"

    # Even if evaluate returned a fire, episode reuse must suppress
    a2 = open_or_reuse_episode(db_session, event[0])
    assert a2 is None
