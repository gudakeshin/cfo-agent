from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from cfo_agent.db import get_session
from cfo_agent.kra import (
    create_kra_version,
    get_active_framework,
    get_active_kra_version,
)
from cfo_agent.models import KraVersion
from cfo_agent.signals import SIGNAL_REGISTRY

router = APIRouter()


class KraPutBody(BaseModel):
    version: str | None = None
    label: str = ""
    india_regulatory_context: str = ""
    faces: dict[str, Any] = Field(default_factory=dict)
    relationships: list[dict[str, Any]] = Field(default_factory=list)
    metric_map: dict[str, Any] = Field(default_factory=dict)
    # Also accept flat kras for convenience — converted to faces server-side if faces empty
    kras: list[dict[str, Any]] | None = None


def _framework_response(session) -> dict:
    fw = get_active_framework(session)
    ver = get_active_kra_version(session)
    kras = [
        {
            "id": k.id,
            "name": k.name,
            "face": k.face,
            "grounding": k.grounding,
            "metrics": k.metrics,
            "concerns": k.concerns,
            "note": k.note,
        }
        for k in fw.kras.values()
    ]
    return {
        "version": fw.version,
        "db_version_id": ver.id,
        "label": fw.label,
        "india_regulatory_context": fw.india_regulatory_context,
        "kras": kras,
        "faces": fw.faces,
        "relationships": [
            {"id": r.id, "from": r.from_kra, "to": r.to_kra, "description": r.description}
            for r in fw.relationships
        ],
        "metric_map": fw.metric_map,
        "registry_metrics": sorted(SIGNAL_REGISTRY.keys()),
    }


@router.get("/kra")
def get_kra():
    session = get_session()
    try:
        return _framework_response(session)
    finally:
        session.close()


@router.get("/kra/versions")
def list_kra_versions():
    session = get_session()
    try:
        rows = session.query(KraVersion).order_by(KraVersion.id.desc()).all()
        active_id = rows[0].id if rows else None
        return [
            {
                "id": r.id,
                "version": r.version,
                "content_hash": r.content_hash,
                "created_at": r.created_at,
                "is_active": r.id == active_id,
            }
            for r in rows
        ]
    finally:
        session.close()


@router.get("/kra/versions/{version_id}")
def get_kra_version(version_id: int):
    session = get_session()
    try:
        row = session.get(KraVersion, version_id)
        if not row:
            raise HTTPException(404, "Version not found")
        import yaml

        data = yaml.safe_load(row.content_yaml)
        return {
            "id": row.id,
            "version": row.version,
            "content_hash": row.content_hash,
            "created_at": row.created_at,
            "content": data,
        }
    finally:
        session.close()


@router.post("/kra/versions/{version_id}/restore")
def restore_kra_version(version_id: int):
    session = get_session()
    try:
        import yaml

        row = session.get(KraVersion, version_id)
        if not row:
            raise HTTPException(404, "Version not found")
        data = yaml.safe_load(row.content_yaml)
        new_row, warnings, reconciled = create_kra_version(session, data)
        session.commit()
        return {
            "version": _framework_response(session),
            "warnings": warnings,
            "restored_from": version_id,
            "new_version_id": new_row.id,
            "reconciled_weighting": (
                {"id": reconciled.id, "label": reconciled.label} if reconciled else None
            ),
        }
    except ValueError as e:
        session.rollback()
        raise HTTPException(422, str(e)) from e
    finally:
        session.close()


@router.put("/kra")
def put_kra(body: KraPutBody):
    session = get_session()
    try:
        data = body.model_dump(exclude_none=True)
        # If client sent flat kras without faces, rebuild faces
        if data.get("kras") and not data.get("faces"):
            faces: dict[str, Any] = {}
            for k in data["kras"]:
                face = k.get("face", "steward")
                faces.setdefault(face, {"kras": []})["kras"].append(
                    {kk: vv for kk, vv in k.items() if kk != "face"}
                )
            data["faces"] = faces
        data.pop("kras", None)

        row, warnings, reconciled = create_kra_version(session, data)
        session.commit()
        return {
            "version": _framework_response(session),
            "warnings": warnings,
            "db_version_id": row.id,
            "reconciled_weighting": (
                {"id": reconciled.id, "label": reconciled.label} if reconciled else None
            ),
        }
    except ValueError as e:
        session.rollback()
        raise HTTPException(422, detail=str(e)) from e
    finally:
        session.close()
