from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from cfo_agent.pipeline.schemas import Connection
from cfo_agent.pipeline.step2_weight import WeightedSignal
from cfo_agent.redlines import RuleFire
from cfo_agent.timeutil import week_sunday


DEFAULT_CONNECT_BOOST = 0.15
MAX_CONNECT_BOOST = 0.5

# Surfacing bars by risk appetite (min priority_score)
APPETITE_BAR = {
    "low": 0.8,
    "moderate": 1.2,
    "high": 2.0,
}


@dataclass
class FilterItem:
    weighted: WeightedSignal | None
    is_red_line: bool
    rule_fire: RuleFire | None = None
    connections: list[Connection] = field(default_factory=list)
    priority_score: float = 0.0
    kra_ids: list[str] = field(default_factory=list)
    signal_ids: list[str] = field(default_factory=list)
    relationship_ids: list[str] = field(default_factory=list)
    score_decomp: dict = field(default_factory=dict)


def step4_filter(
    session: Session,
    weighted: list[WeightedSignal],
    connections: list[Connection],
    weighting: dict,
    week: str,
) -> tuple[list[FilterItem], bool]:
    """Returns (items, empty_bar). Red lines always surface."""
    rules = weighting.get("red_lines") or []
    as_of = week_sunday(week)

    # For pipeline filter, evaluate standing level conditions (not just new events)
    # Re-run level rules without event first-check narrowing for GST — include open notices
    standing_fires: list[RuleFire] = []
    from cfo_agent.redlines import OPS
    from cfo_agent.signals import COMPUTE_FNS
    from cfo_agent.models import ErpGstEvent

    for rule in rules:
        if rule.get("type") == "level":
            fn = COMPUTE_FNS.get(rule["metric"])
            if not fn:
                continue
            mr = fn(session, week)
            if mr.available and OPS[rule["operator"]](mr.value, float(rule["threshold"])):
                standing_fires.append(
                    RuleFire(
                        rule_id=rule["id"],
                        description=rule.get("description", rule["id"]),
                        metric=rule["metric"],
                        value=mr.value,
                        threshold=float(rule["threshold"]),
                        evidence={"lineage": mr.lineage, "value": mr.value},
                        week=week,
                        as_of_date=as_of,
                    )
                )
        elif rule.get("type") == "event":
            rows = (
                session.query(ErpGstEvent)
                .filter(
                    ErpGstEvent.event_type == rule.get("event_type", "show_cause_notice"),
                    ErpGstEvent.event_date <= as_of,
                    ErpGstEvent.status == "open",
                )
                .all()
            )
            if rows:
                standing_fires.append(
                    RuleFire(
                        rule_id=rule["id"],
                        description=rule.get("description", rule["id"]),
                        metric=None,
                        value=float(len(rows)),
                        threshold=None,
                        evidence={
                            "events": [
                                {
                                    "business_key": r.business_key,
                                    "citation": f"erp:erp_gst_events:{r.business_key}",
                                    "amount_paise": r.amount_paise,
                                }
                                for r in rows
                            ]
                        },
                        week=week,
                        as_of_date=as_of,
                    )
                )

    # Signals the Connect step tied to other signals get a bounded boost: a thread
    # across KRAs is worth more than the same move in isolation.
    connect_boost = float((weighting.get("preferences") or {}).get("connect_boost", DEFAULT_CONNECT_BOOST))
    connect_boost = min(max(connect_boost, 0.0), MAX_CONNECT_BOOST)

    appetite = weighting.get("risk_appetite", "moderate")
    bar = APPETITE_BAR.get(appetite, 1.2)
    max_items = int((weighting.get("preferences") or {}).get("max_items", 7))

    # Index connections by signal
    conn_by_sig: dict[str, list[Connection]] = {}
    for c in connections:
        for sid in c.signal_ids:
            conn_by_sig.setdefault(sid, []).append(c)

    items: list[FilterItem] = []
    seen_metrics: set[str] = set()

    for fire in standing_fires:
        # Attach related weighted signal if metric matches
        wmatch = None
        if fire.metric:
            for w in weighted:
                if w.mapped.signal.metric_id == fire.metric:
                    wmatch = w
                    break
        sid = wmatch.mapped.signal.signal_id if wmatch else f"redline:{fire.rule_id}"
        items.append(
            FilterItem(
                weighted=wmatch,
                is_red_line=True,
                rule_fire=fire,
                connections=conn_by_sig.get(sid, []) if wmatch else [],
                priority_score=999.0,
                kra_ids=wmatch.mapped.kra_ids if wmatch else [],
                signal_ids=[wmatch.mapped.signal.signal_id] if wmatch else [],
                relationship_ids=[],
                score_decomp=wmatch.score_decomp if wmatch else {"red_line": fire.rule_id},
            )
        )
        if fire.metric:
            seen_metrics.add(fire.metric)

    for w in weighted:
        mid = w.mapped.signal.metric_id
        if mid in seen_metrics:
            continue
        cons = conn_by_sig.get(w.mapped.signal.signal_id, [])
        boost = 1.0 + connect_boost * min(len(cons), 2) / 2 if cons else 1.0
        score = w.priority_score * boost
        if score < bar:
            continue
        rel_ids = []
        for c in cons:
            rel_ids.extend(c.relationship_ids)
        items.append(
            FilterItem(
                weighted=w,
                is_red_line=False,
                connections=cons,
                priority_score=score,
                kra_ids=w.mapped.kra_ids,
                signal_ids=[w.mapped.signal.signal_id],
                relationship_ids=list(dict.fromkeys(rel_ids)),
                score_decomp={**w.score_decomp, "connect_boost": round(boost, 3), "priority_score": score},
            )
        )
        seen_metrics.add(mid)

    # Red lines first, then by score
    items.sort(key=lambda i: (0 if i.is_red_line else 1, -i.priority_score))
    items = items[:max_items]
    # What moved each surfaced metric (by customer, vendor, cost centre…), computed
    # here so the synthesis explains the move from records instead of guessing.
    from cfo_agent.continuity import continuity_for, key_for_filter_item
    from cfo_agent.drivers import compute_drivers
    from cfo_agent.periods import lens_for_digest

    for it in items:
        metric = it.weighted.mapped.signal.metric_id if it.weighted else (it.rule_fire.metric if it.rule_fire else None)
        drivers = compute_drivers(session, metric, week)
        if drivers:
            it.score_decomp = {**it.score_decomp, "drivers": drivers}
        periods = lens_for_digest(session, metric, week)
        if periods:
            it.score_decomp = {**it.score_decomp, "periods": periods}
        it.score_decomp = {
            **it.score_decomp,
            "continuity": continuity_for(session, key_for_filter_item(it), week),
        }
    empty_bar = len(items) == 0
    return items, empty_bar
