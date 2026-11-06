"""Deterministic red-line rule engine with episode semantics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from cfo_agent.models import Alert, AppState, ErpGstEvent
from cfo_agent.signals import COMPUTE_FNS
from cfo_agent.timeutil import iso_week_id, week_sunday


OPS = {
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "eq": lambda a, b: a == b,
}


@dataclass
class RuleFire:
    rule_id: str
    description: str
    metric: str | None
    value: float | None
    threshold: float | None
    evidence: dict[str, Any]
    week: str
    as_of_date: date


def compile_rules(weighting_content: dict) -> list[dict]:
    return list(weighting_content.get("red_lines") or [])


def get_replay_clock_week(session: Session) -> str | None:
    row = session.get(AppState, "replay_clock_week")
    if not row or row.value_json is None:
        return None
    return row.value_json


def set_replay_clock_week(session: Session, week: str | None) -> None:
    row = session.get(AppState, "replay_clock_week")
    if row is None:
        session.add(AppState(key="replay_clock_week", value_json=week))
    else:
        row.value_json = week
    session.flush()


def evaluate_rules(
    session: Session,
    rules: list[dict],
    *,
    as_of: date,
    last_check_date: date | None = None,
) -> list[RuleFire]:
    """Evaluate level + event rules as of a date. Event rules fire on delta since last_check."""
    week = iso_week_id(as_of)
    # Replay (demo) mode: do not evaluate beyond the replay clock week, so the
    # synthetic quarter's "future" weeks never leak. Live mode has no clamp.
    from cfo_agent.settings import get_settings

    clock = get_replay_clock_week(session) if get_settings().data_mode != "live" else None
    if clock:
        clock_end = week_sunday(clock)
        if as_of > clock_end:
            as_of = clock_end
            week = clock

    fires: list[RuleFire] = []
    for rule in rules:
        rid = rule["id"]
        if rule.get("type") == "level":
            metric = rule["metric"]
            fn = COMPUTE_FNS.get(metric)
            if not fn:
                continue
            # compute using week containing as_of
            mr = fn(session, week)
            op = OPS[rule["operator"]]
            thr = float(rule["threshold"])
            if mr.available and op(mr.value, thr):
                fires.append(
                    RuleFire(
                        rule_id=rid,
                        description=rule.get("description", rid),
                        metric=metric,
                        value=mr.value,
                        threshold=thr,
                        evidence={"lineage": mr.lineage, "value": mr.value},
                        week=week,
                        as_of_date=as_of,
                    )
                )
        elif rule.get("type") == "event":
            table = rule.get("table", "erp_gst_events")
            event_type = rule.get("event_type", "show_cause_notice")
            if table != "erp_gst_events":
                continue
            q = session.query(ErpGstEvent).filter(
                ErpGstEvent.event_type == event_type,
                ErpGstEvent.event_date <= as_of,
            )
            if last_check_date is not None:
                q = q.filter(ErpGstEvent.event_date > last_check_date)
            else:
                # first check: only events on as_of day (or in current week) to avoid backlog flood
                # Use week start as lower bound when no prior check
                from cfo_agent.timeutil import week_monday

                q = q.filter(ErpGstEvent.event_date >= week_monday(week))
            rows = q.all()
            if rows:
                fires.append(
                    RuleFire(
                        rule_id=rid,
                        description=rule.get("description", rid),
                        metric=None,
                        value=float(len(rows)),
                        threshold=None,
                        evidence={
                            "events": [
                                {
                                    "business_key": r.business_key,
                                    "amount_paise": r.amount_paise,
                                    "event_date": r.event_date.isoformat(),
                                    "citation": f"erp:erp_gst_events:{r.business_key}",
                                }
                                for r in rows
                            ]
                        },
                        week=week,
                        as_of_date=as_of,
                    )
                )
    return fires


def open_or_reuse_episode(
    session: Session,
    fire: RuleFire,
) -> Alert | None:
    """One alert per episode (rule + open). Returns new alert or None if episode already open."""
    open_alert = (
        session.query(Alert)
        .filter(Alert.rule_id == fire.rule_id, Alert.status.in_(["active", "acked"]))
        .order_by(Alert.created_at.desc())
        .first()
    )
    if open_alert:
        return None  # episode already open — do not re-alert

    episode_key = f"{fire.rule_id}:{fire.as_of_date.isoformat()}"
    existing = session.query(Alert).filter_by(episode_key=episode_key).one_or_none()
    if existing:
        return None

    alert = Alert(
        rule_id=fire.rule_id,
        episode_key=episode_key,
        week=fire.week,
        as_of_date=fire.as_of_date,
        title=fire.description,
        description=(
            f"{fire.description}. "
            + (f"Value={fire.value} threshold={fire.threshold}" if fire.metric else "")
        ),
        evidence_json={
            "metric": fire.metric,
            "value": fire.value,
            "threshold": fire.threshold,
            **fire.evidence,
        },
        status="active",
    )
    session.add(alert)
    session.flush()
    return alert


def close_cleared_episodes(session: Session, rules: list[dict], as_of: date) -> None:
    """Close active episodes whose level condition no longer holds."""
    week = iso_week_id(as_of)
    for rule in rules:
        if rule.get("type") != "level":
            continue
        rid = rule["id"]
        fn = COMPUTE_FNS.get(rule["metric"])
        if not fn:
            continue
        mr = fn(session, week)
        if not mr.available:
            continue  # unknown ≠ cleared; keep the episode open
        op = OPS[rule["operator"]]
        still = op(mr.value, float(rule["threshold"]))
        if still:
            continue
        for alert in (
            session.query(Alert)
            .filter(Alert.rule_id == rid, Alert.status.in_(["active", "acked"]))
            .all()
        ):
            alert.status = "closed"
            alert.closed_at = datetime.utcnow()


def get_last_redline_check(session: Session) -> date | None:
    row = session.get(AppState, "last_redline_check")
    if not row or not row.value_json:
        return None
    return date.fromisoformat(row.value_json)


def set_last_redline_check(session: Session, d: date) -> None:
    row = session.get(AppState, "last_redline_check")
    if row is None:
        session.add(AppState(key="last_redline_check", value_json=d.isoformat()))
    else:
        row.value_json = d.isoformat()
