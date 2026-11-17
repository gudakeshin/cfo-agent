"""Statutory due dates ahead, from `config/statutory_calendar.yaml`.

This is a calendar, not a compliance record: it applies the standard dates in the law
and knows nothing of extensions, holidays or whether a return was filed."""

from __future__ import annotations

from calendar import monthrange
from datetime import date
from pathlib import Path

import yaml

from cfo_agent.settings import CONFIG_DIR


def load_deadlines(path: Path | None = None) -> list[dict]:
    p = path or (CONFIG_DIR / "statutory_calendar.yaml")
    if not p.exists():
        return []
    return list((yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("deadlines") or [])


def _next_on_or_after(rule: dict, today: date) -> date | None:
    if "monthly" in rule:
        day = int(rule["monthly"])
        y, m = today.year, today.month
        for _ in range(2):
            d = date(y, m, min(day, monthrange(y, m)[1]))
            if d >= today:
                return d
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return None
    if "annual" in rule:
        a = rule["annual"]
        for y in (today.year, today.year + 1):
            d = date(y, int(a["month"]), min(int(a["day"]), monthrange(y, int(a["month"]))[1]))
            if d >= today:
                return d
    return None


def upcoming(today: date, within_days: int = 21, path: Path | None = None) -> list[dict]:
    """Deadlines due from `today` through `within_days` days ahead, soonest first."""
    out = []
    for rule in load_deadlines(path):
        due = _next_on_or_after(rule, today)
        if due is None:
            continue
        away = (due - today).days
        if away <= within_days:
            out.append(
                {
                    "id": rule["id"],
                    "label": rule["label"],
                    "authority": rule.get("authority", ""),
                    "due_date": due.isoformat(),
                    "days_away": away,
                }
            )
    return sorted(out, key=lambda r: (r["days_away"], r["label"]))
