"""Signal context beyond week-on-week change: plan vs actual, covenant proximity,
short-horizon forecasts and anomaly detection. Everything here is deterministic.

`signal_context` returns a JSON-able dict stored on each Signal (`context_json`) and
a severity floor. The pipeline's severity becomes max(change severity, floor), so
context can only *raise* attention: a metric that is far off plan, inside a covenant
buffer or forecast to breach within four weeks surfaces even if it barely moved.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from cfo_agent.models import ErpCovenant, ErpPlanMetric, Signal
from cfo_agent.timeutil import iso_week_id, paise_to_crore, week_sunday

FORECAST_HORIZON = 4
PLAN_MIN_GAP_PCT = 5.0  # smaller plan gaps are noise
ANOMALY_Z = 3.0

# Demo EPM rows use their own ids and units; anything uploaded with a signal
# metric_id (plan_metrics.csv) is used as-is in that metric's unit.
LEGACY_PLAN_LINKS: dict[str, tuple[str, float]] = {
    "cash_balance_cr": ("cash_plan", 1 / 1e9),  # paise → ₹ Cr
    "revenue_week_cr": ("revenue_plan", 1 / 1e9),  # summed across segments
}


def prev_week(week: str, n: int = 1) -> str:
    return iso_week_id(week_sunday(week) - timedelta(days=7 * n))


# ── statistics ──────────────────────────────────────────────────────────────

def robust_z(delta: float | None, history: list[float]) -> float | None:
    """How unusual this week's change is versus recent changes (median / MAD), so
    one past spike does not mask the next one the way a standard deviation does."""
    if delta is None or len(history) < 4:
        return None
    h = np.asarray(history, dtype=float)
    med = float(np.median(h))
    mad = float(np.median(np.abs(h - med))) * 1.4826
    scale = mad if mad > 1e-9 else (float(np.std(h)) or 1e-9)
    return float(abs(delta - med) / scale)


def holt_forecast(values: list[float], horizon: int = FORECAST_HORIZON, alpha: float = 0.5, beta: float = 0.3) -> list[dict]:
    """Holt's linear trend (double exponential smoothing) with an 80% band from
    the one-step-ahead residuals. Needs at least four observations."""
    if len(values) < 4:
        return []
    level, trend = values[0], values[1] - values[0]
    resid = []
    for v in values[1:]:
        pred = level + trend
        resid.append(v - pred)
        new_level = alpha * v + (1 - alpha) * (level + trend)
        trend = beta * (new_level - level) + (1 - beta) * trend
        level = new_level
    sigma = float(np.std(resid)) if len(resid) > 1 else 0.0
    out = []
    for h in range(1, horizon + 1):
        point = level + h * trend
        band = 1.2816 * sigma * np.sqrt(h)
        out.append({"h": h, "value": round(point, 3), "low": round(point - band, 3), "high": round(point + band, 3)})
    return out


def _breaches(value: float, threshold: float, operator: str) -> bool:
    # Covenant operator states what must hold (gte: value must stay ≥ threshold)
    return value < threshold if operator in ("gte", "gt") else value > threshold


# ── context ─────────────────────────────────────────────────────────────────

def value_history(session: Session, metric_id: str, week: str, n: int = 8) -> list[float]:
    """Values for the n weeks before `week` (oldest first): persisted signals where
    present, computed on the fly (not persisted) for gaps."""
    from cfo_agent.signals import COMPUTE_FNS

    weeks = [prev_week(week, i) for i in range(n, 0, -1)]
    stored = dict(
        session.query(Signal.week, Signal.value).filter(Signal.metric_id == metric_id, Signal.week.in_(weeks)).all()
    )
    fn = COMPUTE_FNS.get(metric_id)
    out = []
    for w in weeks:
        if w in stored:
            out.append(float(stored[w]))
        elif fn is not None:
            r = fn(session, w)
            if r.available and r.lineage:
                out.append(float(r.value))
    return out


def plan_context(session: Session, metric_id: str, week: str, value: float, adverse: str | None) -> dict | None:
    row = (
        session.query(ErpPlanMetric)
        .filter_by(week=week, metric_id=metric_id, kind="plan", segment="")
        .order_by(ErpPlanMetric.id.desc())
        .first()
    )
    if row is not None:
        plan = float(row.value)
    elif metric_id in LEGACY_PLAN_LINKS:
        legacy, scale = LEGACY_PLAN_LINKS[metric_id]
        rows = session.query(ErpPlanMetric).filter_by(week=week, metric_id=legacy, kind="plan").all()
        if not rows:
            return None
        row = rows[0]
        plan = sum(float(r.value) for r in rows) * scale
    else:
        return None
    if abs(plan) < 1e-9:
        return None
    gap = value - plan
    gap_pct = 100.0 * gap / abs(plan)
    adverse_gap = adverse is None or (gap > 0 if adverse == "up" else gap < 0)
    sev = float(np.clip(abs(gap_pct) / 10.0, 0.5, 3.0)) if adverse_gap and abs(gap_pct) >= PLAN_MIN_GAP_PCT else 0.0
    return {
        "plan": round(plan, 3),
        "gap": round(gap, 3),
        "gap_pct": round(gap_pct, 1),
        "adverse": bool(adverse_gap and abs(gap_pct) >= PLAN_MIN_GAP_PCT),
        "severity": round(sev, 3),
        "citation": f"erp:erp_plan_metrics:{row.business_key}",
    }


def covenant_context(session: Session, metric_id: str, value: float, forecast: list[dict]) -> dict | None:
    cov = session.query(ErpCovenant).filter_by(metric=metric_id).order_by(ErpCovenant.id).first()
    if cov is None:
        return None
    to_unit = paise_to_crore if metric_id.endswith("_cr") else (lambda p: p / 100.0)
    thr = float(to_unit(cov.threshold_paise))
    buf = float(to_unit(cov.buffered_threshold_paise))
    op = cov.operator or "gte"
    headroom = value - thr if op in ("gte", "gt") else thr - value
    breached = _breaches(value, thr, op)
    in_buffer = not breached and _breaches(value, buf, op)
    breach_in = next((f["h"] for f in forecast if _breaches(f["value"], thr, op)), None)
    sev = 3.0 if breached else 2.5 if in_buffer else (2.0 if breach_in else 0.0)
    return {
        "name": cov.name,
        "threshold": round(thr, 3),
        "buffer": round(buf, 3),
        "operator": op,
        "headroom": round(headroom, 3),
        "breached": breached,
        "in_buffer": in_buffer,
        "projected_breach_in_weeks": breach_in,
        "severity": sev,
        "citation": f"erp:erp_covenants:{cov.business_key}",
    }


def signal_context(
    session: Session,
    metric_id: str,
    week: str,
    value: float,
    delta: float | None,
    history_deltas: list[float],
    adverse: str | None,
) -> tuple[dict[str, Any], float, list[str]]:
    """Returns (context, severity floor, extra citations)."""
    ctx: dict[str, Any] = {}
    floor = 0.0
    cites: list[str] = []

    z = robust_z(delta, history_deltas)
    if z is not None:
        adverse_move = adverse is None or delta == 0 or ((delta or 0) > 0 if adverse == "up" else (delta or 0) < 0)
        ctx["anomaly"] = {"z": round(z, 2), "is_anomaly": z >= ANOMALY_Z and adverse_move}
        if z >= ANOMALY_Z and adverse_move:
            floor = max(floor, 2.0)

    p = plan_context(session, metric_id, week, value, adverse)
    if p:
        ctx["plan"] = p
        floor = max(floor, p["severity"])
        cites.append(p["citation"])

    has_cov = session.query(ErpCovenant.id).filter_by(metric=metric_id).first() is not None
    if has_cov:
        hist = value_history(session, metric_id, week)
        fc = holt_forecast(hist + [value])
        if fc:
            ctx["forecast"] = fc
        c = covenant_context(session, metric_id, value, fc)
        if c:
            ctx["covenant"] = c
            floor = max(floor, c["severity"])
            cites.append(c["citation"])
    return ctx, floor, cites
