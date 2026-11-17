"""Period lens: month/quarter/year to date and year-ago views are built from weekly
readings and omitted wherever the data does not reach back far enough."""

from __future__ import annotations

from datetime import date

from cfo_agent.periods import period_lens, period_start
from cfo_agent.timeutil import iso_week_id, week_sunday

WEEK = "2026-W28"  # ends Sunday 12 Jul 2026


def test_fiscal_period_starts():
    end = date(2026, 7, 12)
    assert period_start(end, "mtd") == date(2026, 7, 1)
    assert period_start(end, "qtd", 4) == date(2026, 7, 1)  # Q2 of an April year
    assert period_start(end, "ytd", 4) == date(2026, 4, 1)
    # Before the financial year turns, the year started the previous April.
    assert period_start(date(2027, 2, 14), "ytd", 4) == date(2026, 4, 1)
    assert period_start(date(2027, 2, 14), "qtd", 4) == date(2027, 1, 1)
    assert period_start(date(2027, 2, 14), "mtd", 4) == date(2027, 2, 1)
    # Calendar-year client
    assert period_start(end, "ytd", 1) == date(2026, 1, 1)
    assert period_start(end, "qtd", 1) == date(2026, 7, 1)
    assert period_start(date(2026, 5, 20), "qtd", 1) == date(2026, 4, 1)


def _by_week(fn):
    """Reading as a function of how many weeks the week ends after 1 Jan 2026."""
    def value(week_id: str):
        return fn((week_sunday(week_id) - date(2026, 1, 4)).days // 7)
    return value


def test_level_metric_compares_with_the_close_of_the_previous_period():
    cash = _by_week(lambda n: 100.0 + n)
    lens = period_lens("cash_balance_cr", WEEK, cash, first_data_day=date(2026, 1, 1), direction="down")
    p = lens["periods"]
    assert lens["aggregation"] == "last"
    # 12 Jul is week 27; the month before closed on Sunday 28 Jun (week 25).
    assert p["mtd"]["change"] == 2.0 and p["mtd"]["since"] == "2026-07-01"
    # The quarter also began 1 Jul, the year on 1 Apr (close of week 12 = 29 Mar).
    assert p["qtd"]["change"] == 2.0
    assert p["ytd"]["change"] == 15.0
    assert p["ytd"]["adverse"] is False  # cash rose; adverse only if it fell
    assert p["yoy"] is None  # nothing a year earlier


def test_falling_cash_is_flagged_adverse():
    cash = _by_week(lambda n: 100.0 - n)
    p = period_lens("cash_balance_cr", WEEK, cash, first_data_day=date(2026, 1, 1), direction="down")["periods"]
    assert p["mtd"]["change"] == -2.0 and p["mtd"]["adverse"] is True


def test_flow_metric_sums_the_period_and_compares_with_the_weeks_before():
    fcf = _by_week(lambda n: float(n))
    lens = period_lens("free_cash_flow_cr", WEEK, fcf, first_data_day=date(2026, 1, 1), direction="down")
    p = lens["periods"]
    assert lens["aggregation"] == "sum"
    # Weeks ending 5 and 12 Jul are 26 and 27; the two before are 24 and 25.
    assert p["mtd"]["value"] == 53.0 and p["mtd"]["baseline"] == 49.0 and p["mtd"]["change"] == 4.0
    assert "total of 2 weeks" in p["mtd"]["basis"]
    # Year to date: weeks ending 5 Apr … 12 Jul = 15 weeks (13..27)
    assert p["ytd"] is None  # the 15 weeks before 1 Apr start before the data does
    full = period_lens("free_cash_flow_cr", WEEK, fcf, first_data_day=date(2025, 1, 1), direction="down")["periods"]
    assert full["ytd"]["value"] == float(sum(range(13, 28)))
    assert full["ytd"]["baseline"] == float(sum(range(-2, 13)))


def test_rate_metric_averages_the_period():
    rate = _by_week(lambda n: float(n))
    p = period_lens("collections_coverage_pct", WEEK, rate, first_data_day=date(2026, 1, 1), direction="down")["periods"]
    assert p["mtd"]["value"] == 26.5


def test_nothing_is_shown_where_the_data_does_not_reach():
    cash = _by_week(lambda n: 100.0 + n)
    # Data begins 13 Apr 2026: no reading exists for the week closing 29 Mar, so no YTD change.
    p = period_lens("cash_balance_cr", WEEK, cash, first_data_day=date(2026, 4, 13), direction="down")["periods"]
    assert p["ytd"] is None and p["yoy"] is None
    assert p["mtd"]["change"] == 2.0
    # Too little history for any reading at all
    p = period_lens("cash_balance_cr", WEEK, cash, first_data_day=date(2026, 7, 1), direction="down")["periods"]
    assert all(v is None for v in p.values())


def test_a_partly_covered_flow_window_is_not_summed():
    # Weeks before May are missing, so a YTD total would silently undercount.
    def fcf(week_id: str):
        return 5.0 if week_sunday(week_id) >= date(2026, 5, 3) else None

    p = period_lens("free_cash_flow_cr", WEEK, fcf, first_data_day=date(2026, 1, 1), direction="down")["periods"]
    assert p["ytd"] is None
    assert p["mtd"]["value"] == 10.0


def test_year_ago_comparison_when_a_year_of_data_exists():
    cash = _by_week(lambda n: 100.0 + n)
    # The reading a year earlier (364 days) is 52 weeks back; extend the data back to 2025.
    def with_2025(week_id: str):
        n = (week_sunday(week_id) - date(2026, 1, 4)).days // 7
        return 100.0 + n

    p = period_lens("cash_balance_cr", WEEK, with_2025, first_data_day=date(2025, 1, 1), direction="down")["periods"]
    assert p["yoy"]["change"] == 52.0
    assert p["yoy"]["basis"].startswith("vs the week ending 13 Jul 2025")
    assert iso_week_id(date(2025, 7, 13)) == "2025-W28"


# ── the digest uses the period views ────────────────────────────────────────

def test_digest_text_and_prompt_carry_the_period_figures(seeded_erp, db_session, monkeypatch):
    from cfo_agent.models import DigestItem, PipelineRun
    from cfo_agent.pipeline.orchestrator import reserve_week, run_pipeline
    from cfo_agent.settings import get_settings
    from cfo_agent.weighting import get_active_weighting

    monkeypatch.setattr("cfo_agent.pipeline.orchestrator.archival_insert", lambda *a, **k: None)
    monkeypatch.setattr("cfo_agent.pipeline.orchestrator.has_unconsolidated_activity", lambda s: False)
    monkeypatch.setattr("cfo_agent.pipeline.orchestrator.send_digest_email", lambda s, d: d)
    monkeypatch.setattr("cfo_agent.pipeline.step5_synthesize.archival_search", lambda *a, **k: [])
    monkeypatch.setenv("MAX_LLM_COST_USD_PER_RUN", "0")  # deterministic text
    get_settings.cache_clear()

    week = seeded_erp["weeks"][-1]
    wv = get_active_weighting(db_session)
    run = reserve_week(db_session, week, wv.id)
    run_pipeline(run.id, week, wv.id)
    db_session.expire_all()
    run = db_session.get(PipelineRun, run.id)
    assert run.status == "completed", run.error

    items = db_session.query(DigestItem).filter_by(digest_id=run.digest_id).all()
    with_periods = [i for i in items if (i.score_decomp_json or {}).get("periods")]
    assert with_periods, "a surfaced metric should carry its period views"
    for it in with_periods:
        views = it.score_decomp_json["periods"]["views"]
        assert all(v["change"] is not None for v in views)
        # The sentence uses only figures the periods hold, so none is flagged as invented.
        assert not it.verification_json.get("unverified_figures"), it.verification_json
    assert any("start of the" in i.what or "so far" in i.what or "over the year" in i.what for i in with_periods)


def test_prompt_payload_formats_every_figure(seeded_erp, db_session):
    from types import SimpleNamespace

    from cfo_agent.pipeline.fallback import period_display

    sig = SimpleNamespace(metric_id="cash_balance_cr")
    item = SimpleNamespace(
        weighted=SimpleNamespace(mapped=SimpleNamespace(signal=sig)),
        rule_fire=None,
        score_decomp={
            "periods": {
                "aggregation": "last",
                "views": [
                    {"label": "Month to date", "since": "2026-07-01", "value": 21.4, "baseline": 27.04,
                     "change": -5.64, "change_pct": -20.9, "adverse": True, "basis": "vs the week ending 28 Jun 2026"}
                ],
            }
        },
    )
    out = period_display(item)
    assert out == [
        {
            "period": "Month to date",
            "since": "2026-07-01",
            "change_display": "-₹5.64 Cr",
            "basis": "vs the week ending 28 Jun 2026",
            "change_pct_display": "-20.9%",
        }
    ]
