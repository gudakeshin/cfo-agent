"""CFO-facing read models: metric metadata, the Monday briefing (latest digest +
what changed), metric trend history, and a dry-run of a draft weighting."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from cfo_agent.db import get_session
from cfo_agent.models import (
    ErpCreditNote,
    ErpGlEntry,
    ErpPlanMetric,
    MetricDefinition,
    Digest,
    DigestItem,
    ErpCashDaily,
    ErpExpense,
    ErpGstEvent,
    ErpReceipt,
    ErpSalesInvoice,
    Signal,
)
from cfo_agent.signals import COMPUTE_FNS, METRIC_META, metric_meta
from cfo_agent.timeutil import iso_week_id, quarter_weeks_ending, week_sunday

router = APIRouter()

KPI_METRICS = [
    "cash_balance_cr",
    "covenant_headroom_cr",
    "dso_days",
    "ar_overdue_pct",
    "gst_notice_exposure_cr",
    "free_cash_flow_cr",
]

# ── metric history (computed on demand, cached per ERP data state) ──────────

_history_cache: dict[tuple, float | None] = {}


def _data_stamp(session: Session) -> tuple:
    """Cheap fingerprint of the ERP data; any sync or regeneration changes it."""
    from cfo_agent.tenancy import cache_key

    return (
        cache_key(),
        session.query(func.count(ErpSalesInvoice.id)).scalar(),
        session.query(func.max(ErpReceipt.id)).scalar(),
        session.query(func.max(ErpCashDaily.as_of_date)).scalar(),
        session.query(func.count(ErpGstEvent.id)).scalar(),
        session.query(func.max(ErpExpense.id)).scalar(),
        session.query(func.count(ErpCashDaily.id)).scalar(),
        session.query(func.count(ErpCreditNote.id)).scalar(),
        session.query(func.max(ErpGlEntry.id)).scalar(),
        session.query(func.max(ErpPlanMetric.id)).scalar(),
        session.query(func.max(MetricDefinition.id)).scalar(),
    )


def metric_value(session: Session, metric_id: str, week: str, stamp: tuple) -> float | None:
    key = (metric_id, week, stamp)
    if key not in _history_cache:
        fn = COMPUTE_FNS.get(metric_id)
        if not fn:
            return None
        mr = fn(session, week)
        _history_cache[key] = mr.value if mr.available else None
        if len(_history_cache) > 5000:
            _history_cache.clear()
    return _history_cache[key]


def _data_weeks(session: Session, n: int = 13) -> list[str]:
    end = session.query(ErpCashDaily.as_of_date).order_by(ErpCashDaily.as_of_date.desc()).limit(1).scalar()
    if not end:
        return []
    if end.weekday() != 6:
        end = end - timedelta(days=end.weekday() + 1)
    return quarter_weeks_ending(end, n)


def _prev_week(week: str) -> str:
    return iso_week_id(week_sunday(week) - timedelta(days=7))


@router.get("/metrics")
def list_metrics():
    from cfo_agent.metrics_dsl import ensure_custom_metrics, visible_metric_ids

    session = get_session()
    try:
        ensure_custom_metrics(session)
        session.commit()
    finally:
        session.close()
    return [metric_meta(m) for m in visible_metric_ids()]


@router.get("/signals/history")
def signal_history(
    metrics: str = Query(..., description="Comma-separated metric ids"),
    until: str | None = None,
    n: int = Query(13, ge=2, le=26),
):
    session = get_session()
    try:
        weeks = _data_weeks(session, 26)
        if until:
            weeks = [w for w in weeks if w <= until]
        weeks = weeks[-n:]
        stamp = _data_stamp(session)
        out: dict[str, Any] = {}
        for m in [x for x in metrics.split(",") if x in COMPUTE_FNS][:12]:
            out[m] = {
                **metric_meta(m),
                "points": [{"week": w, "value": metric_value(session, m, w, stamp)} for w in weeks],
            }
        return out
    finally:
        session.close()


@router.get("/periods")
def period_views(
    metrics: str | None = Query(None, description="Comma-separated metric ids; default is the KPI set"),
    week: str | None = None,
):
    """Month-, quarter-, year-to-date and year-ago views of each metric, as of `week`
    (default: the latest digest's week, else the latest week with data)."""
    from cfo_agent.analytics import plan_context
    from cfo_agent.periods import data_start, period_lens
    from cfo_agent.settings import get_settings

    session = get_session()
    try:
        if not week:
            latest = _latest_digest(session)
            weeks = _data_weeks(session, 1)
            week = latest.week if latest else (weeks[-1] if weeks else None)
        if not week:
            return {"week": None, "metrics": {}}
        stamp = _data_stamp(session)
        first_day = data_start(session)
        fy = get_settings().fiscal_year_start_month
        ids = [m for m in (metrics.split(",") if metrics else KPI_METRICS) if m in COMPUTE_FNS][:12]
        out: dict[str, Any] = {}
        for m in ids:
            meta = metric_meta(m)
            lens = period_lens(
                m,
                week,
                lambda w, m=m: metric_value(session, m, w, stamp),
                first_data_day=first_day,
                direction=meta["adverse_direction"],
                fy_start_month=fy,
            )
            current = metric_value(session, m, week, stamp)
            plan = plan_context(session, m, week, current, meta["adverse_direction"]) if current is not None else None
            out[m] = {
                **meta,
                "value": current,
                **lens,
                "vs_plan": None if not plan else {k: plan[k] for k in ("plan", "gap", "gap_pct", "adverse")},
            }
        return {"week": week, "fiscal_year_start_month": fy, "metrics": out}
    finally:
        session.close()


# ── briefing ────────────────────────────────────────────────────────────────

def item_key(item: DigestItem) -> str:
    """Stable identity of an insight across weeks: its metric, else its red-line rule."""
    for sid in item.signal_ids_json or []:
        parts = str(sid).split(":")
        if len(parts) >= 2:
            return f"metric:{parts[1]}"
    rl = (item.score_decomp_json or {}).get("red_line")
    if rl:
        return f"redline:{rl}"
    return f"title:{item.title.lower()}"


def _latest_digest(session: Session, before_week: str | None = None) -> Digest | None:
    q = session.query(Digest)
    if before_week:
        q = q.filter(Digest.week < before_week)
    return q.order_by(Digest.week.desc(), Digest.created_at.desc()).first()


@router.get("/briefing")
def briefing():
    session = get_session()
    try:
        latest = _latest_digest(session)
        if not latest:
            return {"digest_id": None}
        prev = _latest_digest(session, before_week=latest.week)

        cur_items = sorted(latest.items, key=lambda i: i.rank)
        prev_items = sorted(prev.items, key=lambda i: i.rank) if prev else []
        prev_keys = {item_key(i): i for i in prev_items}
        cur_keys = {item_key(i) for i in cur_items}

        def brief(i: DigestItem) -> dict:
            return {"item_id": i.id, "title": i.title, "is_red_line": i.is_red_line, "rank": i.rank}

        changes = {
            "new": [brief(i) for i in cur_items if item_key(i) not in prev_keys],
            "continuing": [brief(i) for i in cur_items if item_key(i) in prev_keys],
            "dropped": [brief(i) for k, i in prev_keys.items() if k not in cur_keys] if prev else [],
        }

        stamp = _data_stamp(session)
        pw = _prev_week(latest.week)
        kpis = []
        for m in KPI_METRICS:
            v = metric_value(session, m, latest.week, stamp)
            p = metric_value(session, m, pw, stamp)
            meta = metric_meta(m)
            delta = (v - p) if (v is not None and p is not None) else None
            adverse = None
            if delta and meta["adverse_direction"]:
                adverse = delta > 0 if meta["adverse_direction"] == "up" else delta < 0
            kpis.append({**meta, "value": v, "previous": p, "delta": delta, "adverse": adverse})

        return {
            "digest_id": latest.id,
            "week": latest.week,
            "previous_digest_id": prev.id if prev else None,
            "previous_week": prev.week if prev else None,
            "changes": changes,
            "kpis": kpis,
        }
    finally:
        session.close()


# ── weighting dry-run ───────────────────────────────────────────────────────

class PreviewBody(BaseModel):
    content: dict[str, Any]
    week: str | None = None


def _ranked(session: Session, mapped, weighting: dict, framework, week: str) -> dict:
    from cfo_agent.pipeline.step2_weight import step2_weight
    from cfo_agent.pipeline.step4_filter import step4_filter

    weighted = step2_weight(mapped, weighting, framework)
    items, empty = step4_filter(session, weighted, [], weighting, week)
    surfaced_metrics = {i.weighted.mapped.signal.metric_id for i in items if i.weighted}
    red = [
        {"rule_id": i.rule_fire.rule_id, "description": i.rule_fire.description}
        for i in items
        if i.is_red_line and i.rule_fire
    ]
    top = [
        {
            **metric_meta(w.mapped.signal.metric_id),
            "score": round(w.priority_score, 3),
            "surfaced": w.mapped.signal.metric_id in surfaced_metrics,
        }
        for w in weighted[:10]
    ]
    return {"top": top, "red_lines": red, "surfaced_count": len(items), "empty_bar": empty}


@router.post("/weighting/preview")
def weighting_preview(body: PreviewBody):
    """Re-rank one week's signals under the active and a draft weighting, with no
    LLM calls and nothing saved. Lets the delegate see the effect before saving."""
    from cfo_agent.kra import get_active_framework
    from cfo_agent.pipeline.step1_map import step1_map
    from cfo_agent.weighting import get_active_weighting, normalize_face_weights, validate_weighting

    session = get_session()
    try:
        framework = get_active_framework(session)
        draft = dict(body.content)
        draft["face_weights"] = normalize_face_weights(draft.get("face_weights") or {})
        errors = validate_weighting(draft, set(framework.kras))
        if errors:
            raise HTTPException(422, detail="; ".join(errors))
        weeks = _data_weeks(session)
        week = body.week or (weeks[-1] if weeks else None)
        if not week:
            raise HTTPException(409, detail="No ERP data to preview against")
        # Map the previous week first so week-on-week deltas exist; all writes are
        # rolled back below.
        if not session.query(Signal).filter_by(week=_prev_week(week)).first():
            step1_map(session, _prev_week(week))
        mapped = step1_map(session, week)
        active = get_active_weighting(session).content_json
        result = {
            "week": week,
            "active": _ranked(session, mapped, active, framework, week),
            "draft": _ranked(session, mapped, draft, framework, week),
        }
        return result
    finally:
        session.rollback()
        session.close()


# ── KRA heatmap ─────────────────────────────────────────────────────────────

@router.get("/insights/heatmap")
def kra_heatmap(n: int = Query(13, ge=4, le=26)):
    """Stress by Face and KRA for the last n weeks: for each KRA, the highest
    adverse severity among its metrics (0 = calm or improving, 3 = extreme), raised
    to the covenant / plan floor when a metric is inside a buffer or off plan."""
    import numpy as np

    from cfo_agent.analytics import covenant_context, plan_context
    from cfo_agent.kra import get_active_framework
    from cfo_agent.signals import ADVERSE_DIRECTION, directional_severity, severity

    session = get_session()
    try:
        fw = get_active_framework(session)
        weeks_all = _data_weeks(session, n + 9)
        if len(weeks_all) < 2:
            return {"weeks": [], "faces": []}
        weeks = weeks_all[-n:]
        stamp = _data_stamp(session)
        metric_cells: dict[str, dict[str, dict]] = {}
        for m in sorted(fw.metrics_for_signals()):
            if m not in COMPUTE_FNS:
                continue
            vals = [metric_value(session, m, w, stamp) for w in weeks_all]
            direction = ADVERSE_DIRECTION.get(m)
            cells = {}
            for i, w in enumerate(weeks_all):
                if w not in weeks or vals[i] is None:
                    continue
                prev = vals[i - 1] if i else None
                delta = (vals[i] - prev) if prev is not None else None
                hist = [vals[j] - vals[j - 1] for j in range(max(1, i - 8), i)
                        if vals[j] is not None and vals[j - 1] is not None]
                sev = directional_severity(m, delta, severity(delta, hist, prev))
                adverse = delta is not None and delta != 0 and (
                    direction is None or (delta > 0 if direction == "up" else delta < 0))
                score = sev if adverse else 0.0
                floor = 0.0
                cov = covenant_context(session, m, vals[i], [])
                if cov:
                    floor = max(floor, cov["severity"])
                plan = plan_context(session, m, w, vals[i], direction)
                if plan:
                    floor = max(floor, plan["severity"])
                cells[w] = {"score": round(float(np.clip(max(score, floor), 0, 3)), 2), "metric": m,
                            "value": vals[i], "delta": None if delta is None else round(delta, 3)}
            metric_cells[m] = cells
        faces = []
        for face_key, face in (fw.faces or {}).items():
            kra_rows = []
            for k in fw.kras.values():
                if k.face != face_key:
                    continue
                cells = []
                for w in weeks:
                    best = None
                    for m in k.metrics:
                        c = metric_cells.get(m, {}).get(w)
                        if c and (best is None or c["score"] > best["score"]):
                            best = c
                    cells.append({"week": w, **(best or {"score": None, "metric": None, "value": None, "delta": None})})
                kra_rows.append({"id": k.id, "name": k.name, "grounded": k.grounding == "erp", "cells": cells})
            face_cells = []
            for i, w in enumerate(weeks):
                scores = [r["cells"][i] for r in kra_rows if r["cells"][i]["score"] is not None]
                top = max(scores, key=lambda c: c["score"]) if scores else None
                face_cells.append({"week": w, "score": top["score"] if top else None,
                                   "metric": top["metric"] if top else None})
            faces.append({"id": face_key, "label": (face or {}).get("name", face_key.title()),
                          "cells": face_cells, "kras": kra_rows})
        return {"weeks": weeks, "faces": faces, "metrics": {m: metric_meta(m) for m in metric_cells}}
    finally:
        session.close()


# ── red-line backtest ───────────────────────────────────────────────────────

class BacktestBody(BaseModel):
    type: str = "level"
    metric: str | None = None
    operator: str | None = None
    threshold: float | None = None
    event_type: str | None = None
    n: int = 13


@router.post("/redlines/backtest")
def backtest_red_line(body: BacktestBody):
    """How often a draft red line would have fired over recent weeks."""
    from cfo_agent.redlines import OPS
    from cfo_agent.timeutil import week_monday

    session = get_session()
    try:
        weeks = _data_weeks(session, max(4, min(body.n, 26)))
        points = []
        if body.type == "level":
            if body.metric not in COMPUTE_FNS:
                raise HTTPException(422, "Choose a metric")
            if body.operator not in OPS or body.threshold is None:
                raise HTTPException(422, "Choose a condition and threshold")
            stamp = _data_stamp(session)
            for w in weeks:
                v = metric_value(session, body.metric, w, stamp)
                points.append({"week": w, "value": v,
                               "fires": v is not None and OPS[body.operator](v, float(body.threshold))})
        elif body.type == "event":
            et = body.event_type or "show_cause_notice"
            for w in weeks:
                cnt = session.query(func.count(ErpGstEvent.id)).filter(
                    ErpGstEvent.event_type == et,
                    ErpGstEvent.event_date >= week_monday(w),
                    ErpGstEvent.event_date <= week_sunday(w),
                ).scalar()
                points.append({"week": w, "value": int(cnt or 0), "fires": bool(cnt)})
        else:
            raise HTTPException(422, "Unknown red-line type")
        fired = [p["week"] for p in points if p["fires"]]
        return {
            "points": points,
            "fired_weeks": fired,
            "fired_count": len(fired),
            "weeks": len(points),
            "metric": metric_meta(body.metric) if body.metric else None,
            "event_types": [t for (t,) in session.query(ErpGstEvent.event_type).distinct()],
        }
    finally:
        session.close()


EVENT_LABELS = {
    "show_cause_notice": "GST show-cause notice",
    "itc_mismatch": "GST input-credit mismatch",
}


@router.get("/redlines/event-types")
def red_line_event_types():
    """Event types a red line can watch: whatever the client's event feed contains
    (plus the two standard GST ones, so a rule can be set before the first event)."""
    session = get_session()
    try:
        found = {t for (t,) in session.query(ErpGstEvent.event_type).distinct() if t}
        return [
            {"value": t, "label": EVENT_LABELS.get(t) or t.replace("_", " ").capitalize(), "in_data": t in found}
            for t in sorted(found | set(EVENT_LABELS))
        ]
    finally:
        session.close()
