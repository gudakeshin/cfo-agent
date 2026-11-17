from datetime import date

from cfo_agent.statutory import upcoming


def test_deadlines_ahead_sorted_and_counted():
    got = upcoming(date(2026, 7, 13), within_days=10)
    assert [(d["id"], d["due_date"], d["days_away"]) for d in got] == [
        ("pf_esi", "2026-07-15", 2),
        ("gstr3b", "2026-07-20", 7),
    ]


def test_a_due_date_today_still_counts_and_months_roll_over():
    assert upcoming(date(2026, 7, 11), within_days=0)[0]["id"] == "gstr1"
    # Late in the month the next monthly dates fall in the following month
    nxt = {d["id"]: d["due_date"] for d in upcoming(date(2026, 12, 28), within_days=20)}
    assert nxt["tds_deposit"] == "2027-01-07" and nxt["gstr9"] == "2026-12-31"


def test_annual_dates_wrap_into_next_year():
    nxt = {d["id"]: d["due_date"] for d in upcoming(date(2026, 12, 20), within_days=100)}
    assert nxt["advance_tax_q4"] == "2027-03-15"


def test_endpoint_dates_a_replayed_briefing_by_its_own_week(db_session):
    from fastapi.testclient import TestClient

    from cfo_agent.main import app

    with TestClient(app) as client:
        r = client.get("/api/statutory-calendar?week=2026-W28&days=10")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["as_of"] == "2026-07-13"  # the Monday after the week ending 12 Jul
    assert [d["id"] for d in body["deadlines"]] == ["pf_esi", "gstr3b"]
    assert "filing status is not tracked" in body["note"]
