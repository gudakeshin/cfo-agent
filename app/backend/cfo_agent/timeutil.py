"""IST week helpers and money formatting. All money is integer paise."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
PAISE_PER_RUPEE = 100
PAISE_PER_LAKH = 100 * 100_000  # ₹1 L = 100,000 rupees
PAISE_PER_CRORE = 100 * 10_000_000  # ₹1 Cr = 10,000,000 rupees


def now_ist() -> datetime:
    return datetime.now(IST)


def today_ist() -> date:
    return now_ist().date()


def iso_week_id(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def week_sunday(week_id: str) -> date:
    """Return the Sunday (end) of an ISO week. ISO weeks end on Sunday."""
    year_s, week_s = week_id.split("-W")
    year, week = int(year_s), int(week_s)
    # ISO: week 1's Thursday defines the year; Monday = day 1
    monday = date.fromisocalendar(year, week, 1)
    return monday + timedelta(days=6)


def week_monday(week_id: str) -> date:
    year_s, week_s = week_id.split("-W")
    return date.fromisocalendar(int(year_s), int(week_s), 1)


def last_completed_sunday(as_of: date | None = None) -> date:
    """Last completed Sunday in IST (if today is Sunday, previous Sunday)."""
    d = as_of or today_ist()
    # weekday(): Mon=0 … Sun=6
    if d.weekday() == 6:
        return d - timedelta(days=7)
    return d - timedelta(days=d.weekday() + 1)


def quarter_weeks_ending(end_sunday: date, n_weeks: int = 13) -> list[str]:
    """n complete ISO weeks ending on end_sunday (inclusive)."""
    if end_sunday.weekday() != 6:
        raise ValueError("end_sunday must be a Sunday")
    weeks: list[str] = []
    cursor = end_sunday
    for _ in range(n_weeks):
        weeks.append(iso_week_id(cursor))
        cursor -= timedelta(days=7)
    weeks.reverse()
    return weeks


def rupees_to_paise(rupees: float | int) -> int:
    return int(round(float(rupees) * PAISE_PER_RUPEE))


def crore_to_paise(crore: float | int) -> int:
    return int(round(float(crore) * PAISE_PER_CRORE))


def paise_to_crore(paise: int) -> float:
    return paise / PAISE_PER_CRORE


def paise_to_lakh(paise: int) -> float:
    return paise / PAISE_PER_LAKH


def format_inr(paise: int, *, style: str = "auto") -> str:
    """Render paise as ₹ lakh/crore strings for UI/email."""
    abs_p = abs(paise)
    sign = "-" if paise < 0 else ""
    if style == "crore" or (style == "auto" and abs_p >= PAISE_PER_CRORE):
        return f"{sign}₹{paise_to_crore(abs_p):.2f} Cr"
    if style == "lakh" or abs_p >= PAISE_PER_LAKH:
        return f"{sign}₹{paise_to_lakh(abs_p):.2f} L"
    rupees = abs_p / PAISE_PER_RUPEE
    return f"{sign}₹{rupees:,.0f}"


def add_days(d: date, n: int) -> date:
    return d + timedelta(days=n)
