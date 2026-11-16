"""New-vs-continuing is computed from stored digests, told to the model, and the
written text is checked against it."""

from __future__ import annotations

import json
import re
from datetime import datetime

from cfo_agent.continuity import continuity_conflicts, continuity_for, continuity_prompt
from cfo_agent.models import Digest, DigestItem, WeightingVersion
from cfo_agent.pipeline.schemas import ConnectOutput, HeadlineOutput, SectionBlock, SynthesizeItemOutput


def _digest(s, week, keys, created=None):
    wv = s.query(WeightingVersion).first()
    d = Digest(week=week, weighting_version_id=wv.id, headline=week, created_at=created or datetime(2026, 7, 1))
    s.add(d)
    s.flush()
    for rank, k in enumerate(keys, 1):
        s.add(DigestItem(digest_id=d.id, rank=rank, title=k, signal_ids_json=[f"metric:{k}:{week}"]))
    s.flush()
    return d


def test_continuity_counts_consecutive_earlier_briefings(db_session):
    s = db_session
    _digest(s, "2026-W26", ["dso_days", "cash_balance_cr"])
    _digest(s, "2026-W27", ["dso_days"])
    _digest(s, "2026-W28", ["dso_days", "gst_open_notices"])

    c = continuity_for(s, "metric:dso_days", "2026-W29")
    assert c == {"status": "continuing", "last_week": "2026-W28", "since_week": "2026-W26", "weeks_running": 3}
    # cash was in W26 only: the run is broken, so it is new again
    assert continuity_for(s, "metric:cash_balance_cr", "2026-W29") == {"status": "new"}
    assert continuity_for(s, "metric:gst_open_notices", "2026-W29")["weeks_running"] == 1
    # nothing earlier at all
    assert continuity_for(s, "metric:dso_days", "2026-W26") == {"status": "new", "first_briefing": True}
    # the week itself and later weeks are never looked at
    assert continuity_for(s, "metric:dso_days", "2026-W28")["last_week"] == "2026-W27"


def test_latest_digest_of_a_week_wins(db_session):
    s = db_session
    _digest(s, "2026-W27", ["dso_days"], created=datetime(2026, 7, 6))
    _digest(s, "2026-W27", ["ar_overdue_pct"], created=datetime(2026, 7, 7))  # re-run under another weighting
    assert continuity_for(s, "metric:dso_days", "2026-W28") == {"status": "new"}


def test_conflicts_in_both_directions_and_rule_names_ignored():
    cont = {"status": "continuing", "last_week": "2026-W27", "since_week": "2026-W27", "weeks_running": 1}
    out = continuity_conflicts(["It is a fresh matter.", "Fine."], cont)
    assert len(out) == 1 and "fresh" in out[0] and "2026-W27" in out[0]
    assert continuity_conflicts(["Still open and unresolved since W27."], cont) == []

    new = {"status": "new"}
    assert continuity_conflicts(["The notice was first flagged in 2026-W27."], new)
    assert continuity_conflicts(["This is the same notice, again."], new)
    assert continuity_conflicts(["Cash is down on last week, and still below the buffer."], new) == []

    # A rule called "Any new GST show-cause notice" describes the rule, not the finding
    rule = "Any new GST show-cause notice"
    assert continuity_conflicts([f"This trips red line RL-2 ({rule}). It continues."], cont, [rule]) == []
    assert continuity_conflicts([f"It trips {rule}; a new notice this week."], cont, [rule])  # real claim still caught
    assert continuity_conflicts(["anything"], None) == []

    # Denying the claim is the right wording and must not be flagged (found on a real run)
    assert continuity_conflicts(["This is the same notice carried forward, not a fresh matter."], cont) == []
    assert continuity_conflicts(["It is not newly raised; it is open."], cont) == []
    assert continuity_conflicts(["Rather than a new breach, this persists."], cont) == []
    assert continuity_conflicts(["This is not the same notice; it is a different one."], new) == []
    # Ordinary use of "again" about a movement is not a claim about the finding
    assert continuity_conflicts(["DSO rose again this week and is still high."], new) == []
    assert continuity_conflicts(["Red line RL-2 tripped again."], new)
    assert continuity_conflicts(["It is a fresh matter, not a continuation."], cont)  # claim still caught


def test_prompt_states_the_fact():
    assert "CONTINUING" in continuity_prompt({"status": "continuing", "last_week": "2026-W27", "since_week": "2026-W26", "weeks_running": 2})
    assert "NEW" in continuity_prompt({"status": "new"})


def test_pipeline_repairs_a_contradiction_once_and_records_what_remains(seeded_erp, db_session, monkeypatch):
    from cfo_agent.pipeline.orchestrator import reserve_week, run_pipeline
    from cfo_agent.models import PipelineRun
    from cfo_agent.weighting import get_active_weighting

    week = seeded_erp["weeks"][-1]
    # An earlier briefing that carried every metric, so every surfaced item is "continuing"
    from cfo_agent.signals import COMPUTE_FNS

    prev_week = seeded_erp["weeks"][-2]
    _digest(db_session, prev_week, list(COMPUTE_FNS))
    db_session.commit()

    calls: list[str] = []

    def fake_parse(schema, *, system, user, purpose, budget=None, **_):
        if budget is not None:
            budget.check()
            budget.calls += 1
        if schema is ConnectOutput:
            return ConnectOutput(connections=[])
        if schema is HeadlineOutput:
            return HeadlineOutput(headline="h")
        item, _ = json.JSONDecoder().raw_decode(user.split("ITEM:\n", 1)[1])  # repair text may follow
        status = item["continuity"]["status"]
        assert ("CONTINUING" in user) == (status == "continuing")
        allowed = json.loads(re.search(r"ALLOWED_CITATIONS: (\[.*?\])\n", user).group(1))
        repair = "REPAIR" in user
        calls.append(f"{status}:{'repair' if repair else 'first'}")
        text = "Open for weeks." if repair else "A fresh matter this week."
        return SynthesizeItemOutput(
            title="Item", what=SectionBlock(text=text, citations=allowed[:1]),
            why=SectionBlock(text="Matters.", citations=allowed[:1]), connects=SectionBlock(text="", citations=[]),
            action=SectionBlock(text="Act.", citations=allowed[:1]),
        )

    monkeypatch.setattr("cfo_agent.pipeline.step5_synthesize.archival_search", lambda *a, **k: [])
    monkeypatch.setattr("cfo_agent.pipeline.orchestrator.archival_insert", lambda *a, **k: None)
    monkeypatch.setattr("cfo_agent.pipeline.orchestrator.has_unconsolidated_activity", lambda s: False)
    monkeypatch.setattr("cfo_agent.pipeline.orchestrator.send_digest_email", lambda s, d: d)
    monkeypatch.setattr("cfo_agent.pipeline.step3_connect.parse", fake_parse)
    monkeypatch.setattr("cfo_agent.pipeline.step5_synthesize.parse", fake_parse)

    wv = get_active_weighting(db_session)
    run = reserve_week(db_session, week, wv.id)
    run_pipeline(run.id, week, wv.id)
    db_session.expire_all()
    run = db_session.get(PipelineRun, run.id)
    assert run.status == "completed", run.error
    items = db_session.query(DigestItem).filter_by(digest_id=run.digest_id).all()
    continuing = [i for i in items if i.score_decomp_json["continuity"]["status"] == "continuing"]
    new = [i for i in items if i.score_decomp_json["continuity"]["status"] == "new"]
    assert continuing, "the earlier briefing carried every metric, so metric items continue"
    assert calls.count("continuing:repair") == len(continuing)  # exactly one rewrite each
    assert "new:repair" not in calls  # "a fresh matter" is true of a new item
    for it in continuing:
        assert it.what == "Open for weeks." and it.verification_json["continuity_conflicts"] == []
    for it in new:
        assert it.what == "A fresh matter this week." and it.verification_json["continuity_conflicts"] == []
