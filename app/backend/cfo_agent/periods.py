"""Period lens: month-to-date, quarter-to-date, year-to-date and year-ago views of a
metric, beside the week-on-week change the briefing already shows.

Metrics are computed per ISO week, so a period view is built from those readings:
 * level metrics (cash, DSO, overdue %…) compare today's reading with the reading at
   the end of the period before this one started;
 * flow metrics add up over the period (net operating cash flow) or average (rates),
   and compare with the same number of weeks immediately before the period.
A comparison is left out, never guessed, when the data does not reach back far
enough to support it.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

from sqlalchemy.orm import Session

from cfo_agent.models import ErpCashDaily, ErpSalesInvoice
from cfo_agent.timeutil import iso_week_id, week_sunday

# How a metric's weekly readings roll up into a period. Anything not listed is a
# level: the reading at the end of the period is the figure.
AGGREGATION: dict[str, str] = {
    "free_cash_flow_cr": "sum",
    "collections_coverage_pct": "avg",
    "cost_center_variance_pct": "avg",
    "segment_revenue_growth_pct": "avg",
}

# Metrics need a few weeks of history to be meaningful (DSO looks back 30 days).
MIN_HISTORY_DAYS = 28
MAX_PERIOD_WEEKS = 53

PERIODS = ("mtd", "qtd", "ytd")
PERIOD_LABELS = {
    "mtd": "Month to date",
    "qtd": "Quarter to date",
    "ytd": "Year to date",
    "yoy": "Vs a year ago",
}

ValueFn = Callable[[str], float | None]


def period_start(end: date, kind: str, fy_start_month: int = 4) -> date:
    """First day of the month / fiscal quarter / fiscal year containing `end`."""
    if kind == "mtd":
        return end.replace(day=1)
    fy_year = end.year if end.month >= fy_start_month else end.year - 1
    if kind == "ytd":
        return date(fy_year, fy_start_month, 1)
    if kind == "qtd":
        months_in = (end.year - fy_year) * 12 + end.month - fy_start_month
        m = fy_start_month + (months_in // 3) * 3  # may run past 12
        return date(fy_year + (m - 1) // 12, (m - 1) % 12 + 1, 1)
    raise ValueError(kind)


def _sundays(first: date, last: date) -> list[date]:
    """Sundays (ISO week ends) within [first, last]."""
    d = first + timedelta(days=(6 - first.weekday()) % 7)
    out: list[date] = []
    while d <= last:
        out.append(d)
        d += timedelta(days=7)
    return out


def data_start(session: Session) -> date | None:
    """First day the client's data covers."""
    starts = [
        d
        for d in (
            session.query(ErpCashDaily.as_of_date).order_by(ErpCashDaily.as_of_date).limit(1).scalar(),
            session.query(ErpSalesInvoice.invoice_date).order_by(ErpSalesInvoice.invoice_date).limit(1).scalar(),
        )
        if d
    ]
    return min(starts) if starts else None


def _aggregate(kind: str, readings: list[float]) -> float | None:
    if not readings:
        return None
    return float(sum(readings)) if kind == "sum" else float(sum(readings) / len(readings))


def _adverse(change: float | None, direction: str | None) -> bool | None:
    if not change or not direction:
        return None
    return change > 0 if direction == "up" else change < 0


def _row(
    label: str,
    value: float | None,
    base: float | None,
    direction: str | None,
    *,
    basis: str,
    since: date | None = None,
) -> dict:
    change = (value - base) if (value is not None and base is not None) else None
    pct = (100.0 * change / abs(base)) if (change is not None and base) else None
    return {
        "label": label,
        "value": None if value is None else round(value, 3),
        "baseline": None if base is None else round(base, 3),
        "change": None if change is None else round(change, 3),
        "change_pct": None if pct is None else round(pct, 1),
        "adverse": _adverse(change, direction),
        "basis": basis,
        "since": since.isoformat() if since else None,
    }


def period_lens(
    metric_id: str,
    week: str,
    value_fn: ValueFn,
    *,
    first_data_day: date | None,
    direction: str | None,
    fy_start_month: int = 4,
) -> dict:
    """The period views of one metric as of `week`. `value_fn(week_id)` returns the
    metric's reading for a week, or None where it can't be computed."""
    agg = AGGREGATION.get(metric_id, "last")
    end = week_sunday(week)
    earliest_ok = (first_data_day + timedelta(days=MIN_HISTORY_DAYS)) if first_data_day else None

    def reading(sunday: date) -> float | None:
        if earliest_ok is None or sunday < earliest_ok:
            return None
        return value_fn(iso_week_id(sunday))

    def window(first: date, last: date) -> tuple[float | None, int]:
        sundays = _sundays(first, last)[-MAX_PERIOD_WEEKS:]
        vals = [v for v in (reading(s) for s in sundays) if v is not None]
        # Only a complete window counts; a partly covered one would flatter or sink the figure.
        if not sundays or len(vals) != len(sundays):
            return None, len(sundays)
        return _aggregate(agg, vals), len(sundays)

    current = reading(end)
    rows: dict[str, dict | None] = {}

    for kind in PERIODS:
        start = period_start(end, kind, fy_start_month)
        if agg == "last":
            # Reading at the close of the period before this one began.
            base_end = start - timedelta(days=1)
            base_sunday = base_end - timedelta(days=(base_end.weekday() + 1) % 7)
            rows[kind] = _row(
                PERIOD_LABELS[kind],
                current,
                reading(base_sunday) if base_sunday < end else None,
                direction,
                basis=f"vs the week ending {base_sunday:%d %b %Y}",
                since=start,
            )
        else:
            val, n = window(start, end)
            if val is None:
                rows[kind] = None
                continue
            prior_end = start - timedelta(days=1)
            prior_first = prior_end - timedelta(days=7 * n - 1)
            prior, _ = window(prior_first, prior_end)
            rows[kind] = _row(
                PERIOD_LABELS[kind],
                val,
                prior,
                direction,
                basis=f"{'total' if agg == 'sum' else 'average'} of {n} week{'s' if n != 1 else ''}, vs the {n} before",
                since=start,
            )

    # Year on year: the same point (or the same span) a year earlier, 52 weeks back.
    year_ago = end - timedelta(days=364)
    if agg == "last":
        rows["yoy"] = _row(
            PERIOD_LABELS["yoy"], current, reading(year_ago), direction, basis=f"vs the week ending {year_ago:%d %b %Y}"
        )
    else:
        start = period_start(end, "mtd", fy_start_month)
        val, n = window(start, end)
        prior, _ = window(start - timedelta(days=364), end - timedelta(days=364))
        rows["yoy"] = _row(PERIOD_LABELS["yoy"], val, prior, direction, basis="this month vs the same weeks a year ago")

    # A period with nothing to compare against is shown as unavailable, not as a bare number.
    rows = {k: (r if r and r["change"] is not None else None) for k, r in rows.items()}
    return {"aggregation": agg, "periods": rows}
