"""Declarative metric authoring: list, preview against real data, save, retire."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from cfo_agent.db import get_session
from cfo_agent.metrics_dsl import (
    AGGS,
    BUILTIN_IDS,
    SOURCES,
    UNITS,
    MetricDefinitionError,
    active_definitions,
    ensure_custom_metrics,
    evaluate,
    retire_definition,
    save_definition,
    validate_definition,
)
from cfo_agent.models import MetricDefinition

router = APIRouter()


class DefinitionBody(BaseModel):
    definition: dict[str, Any]


@router.get("/metrics/definitions")
def list_definitions():
    session = get_session()
    try:
        ensure_custom_metrics(session)
        session.commit()
        from cfo_agent.kra import get_active_framework

        used = get_active_framework(session).metric_map
        defs = active_definitions(session)
        history = {}
        for r in session.query(MetricDefinition).order_by(MetricDefinition.id.desc()).all():
            history.setdefault(r.metric_id, []).append(
                {"id": r.id, "active": r.active, "created_by": r.created_by, "created_at": r.created_at})
        return {
            "definitions": [
                {**d, "used_by_kras": used.get(mid, []), "versions": history.get(mid, [])}
                for mid, d in sorted(defs.items())
            ],
            "vocabulary": {
                "sources": {k: {"filters": sorted(v[3]), "money": bool(v[2])} for k, v in SOURCES.items()},
                "aggs": sorted(AGGS),
                "units": sorted(UNITS),
                "windows": ["week", "as_of", "month_to_date", "last_month", "quarter_to_date", "trailing_30d", "trailing_90d"],
            },
        }
    finally:
        session.close()


@router.post("/metrics/preview")
def preview_definition(body: DefinitionBody):
    """Validate a draft and compute it for the latest weeks without saving."""
    from cfo_agent.api.insights import _data_weeks

    session = get_session()
    try:
        ensure_custom_metrics(session)
        try:
            d = validate_definition(body.definition, builtin_ids=BUILTIN_IDS)
        except MetricDefinitionError as e:
            raise HTTPException(422, str(e)) from None
        points = []
        for w in _data_weeks(session, 8):
            r = evaluate(session, d, w)
            points.append({"week": w, "value": r.value if r.available else None, "sources": len(r.lineage)})
        last = evaluate(session, d, points[-1]["week"]) if points else None
        return {"definition": d, "points": points, "sample_sources": (last.lineage[:5] if last else [])}
    finally:
        session.rollback()
        session.close()


@router.post("/metrics/definitions")
def create_definition(body: DefinitionBody, request: Request):
    user = getattr(getattr(request.state, "user", None), "email", "unknown")
    session = get_session()
    try:
        row = save_definition(session, body.definition, user=user)
        session.commit()
        return {"id": row.id, "definition": row.definition_json}
    except MetricDefinitionError as e:
        session.rollback()
        raise HTTPException(422, str(e)) from None
    finally:
        session.close()


@router.post("/metrics/definitions/{metric_id}/retire")
def retire(metric_id: str, request: Request):
    user = getattr(getattr(request.state, "user", None), "email", "unknown")
    session = get_session()
    try:
        retire_definition(session, metric_id, user=user)
        session.commit()
        return {"ok": True}
    except MetricDefinitionError as e:
        session.rollback()
        raise HTTPException(422, str(e)) from None
    finally:
        session.close()
