from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from cfo_agent.db import get_session
from cfo_agent.memory.blocks import set_block_content
from cfo_agent.weighting import (
    create_weighting_version,
    get_active_weighting,
    list_presets,
    load_preset,
    weighting_to_prompt_block,
)
from cfo_agent.models import WeightingVersion

logger = logging.getLogger(__name__)
router = APIRouter()


class WeightingBody(BaseModel):
    label: str
    defining_priority: str
    face_weights: dict[str, float]
    kra_ranks: list[str]
    risk_appetite: str = "moderate"
    red_lines: list[dict[str, Any]] = []
    preferences: dict[str, Any] = {}


@router.get("/weighting")
def get_weighting():
    session = get_session()
    try:
        w = get_active_weighting(session)
        return {"id": w.id, "label": w.label, "content": w.content_json, "created_at": w.created_at}
    finally:
        session.close()


@router.get("/weighting/versions")
def weighting_versions():
    session = get_session()
    try:
        rows = session.query(WeightingVersion).order_by(WeightingVersion.id.desc()).all()
        return [
            {"id": r.id, "label": r.label, "is_active": r.is_active, "created_at": r.created_at}
            for r in rows
        ]
    finally:
        session.close()


@router.put("/weighting")
def put_weighting(body: WeightingBody):
    session = get_session()
    try:
        content = body.model_dump()
        row = create_weighting_version(session, content, activate=True)
        # Refresh memory block (weighting version already persisted; log if block sync fails)
        try:
            set_block_content(
                session,
                "priority_weighting",
                weighting_to_prompt_block(content),
                edited_by="human",
                reason="WeightingEditor save",
                enforce_agent_rules=False,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Failed to refresh priority_weighting memory block after save (version %s): %s",
                row.id,
                e,
            )
        session.commit()
        return {"id": row.id, "label": row.label, "content": row.content_json}
    except ValueError as e:
        session.rollback()
        raise HTTPException(422, str(e)) from e
    finally:
        session.close()


@router.get("/weighting/presets")
def presets():
    return list_presets()


@router.post("/weighting/presets/{preset_id}/load")
def load_preset_activate(preset_id: str):
    session = get_session()
    try:
        content = load_preset(preset_id)
        row = create_weighting_version(session, content, activate=True)
        set_block_content(
            session,
            "priority_weighting",
            weighting_to_prompt_block(content),
            edited_by="human",
            reason=f"Loaded preset {preset_id}",
            enforce_agent_rules=False,
        )
        session.commit()
        return {"id": row.id, "label": row.label, "content": row.content_json}
    except ValueError as e:
        session.rollback()
        raise HTTPException(422, str(e)) from e
    finally:
        session.close()
