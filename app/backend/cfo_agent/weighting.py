from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from cfo_agent.kra import get_active_framework
from cfo_agent.models import WeightingVersion
from cfo_agent.settings import CONFIG_DIR

PRESET_FILES = {
    "acquisition": "priority_weighting.yaml",
    "derisking": "weighting_derisking.yaml",
}


def load_weighting_yaml(path: Path | None = None) -> dict[str, Any]:
    path = path or (CONFIG_DIR / "priority_weighting.yaml")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def validate_weighting(content: dict[str, Any], kra_ids: set[str] | None = None) -> list[str]:
    errors: list[str] = []
    fw = get_active_framework()
    kra_ids = kra_ids or fw.kra_ids()

    weights = content.get("face_weights") or {}
    required = {"steward", "operator", "strategist", "catalyst"}
    if set(weights.keys()) != required:
        errors.append(f"face_weights must include exactly {required}")
    else:
        total = sum(float(weights[k]) for k in required)
        if abs(total - 1.0) > 1e-6:
            errors.append(f"face_weights must sum to 1.0, got {total}")

    ranks = content.get("kra_ranks") or []
    for kid in ranks:
        if kid not in kra_ids:
            errors.append(f"kra_ranks references unknown KRA {kid}")

    appetite = content.get("risk_appetite", "moderate")
    if appetite not in {"low", "moderate", "high"}:
        errors.append(f"Invalid risk_appetite: {appetite}")

    for rl in content.get("red_lines") or []:
        rid = rl.get("id", "?")
        rtype = rl.get("type")
        if rtype == "level":
            metric = rl.get("metric")
            if metric and metric not in fw.metric_map and metric not in fw.metrics_for_signals():
                # allow any metric that exists in metric_map keys
                if metric not in fw.metric_map:
                    errors.append(f"Red line {rid} unknown metric {metric}")
            if rl.get("operator") not in {"lt", "lte", "gt", "gte", "eq"}:
                errors.append(f"Red line {rid} bad operator")
            if "threshold" not in rl:
                errors.append(f"Red line {rid} missing threshold")
        elif rtype == "event":
            if rl.get("trigger") != "on_new_record":
                errors.append(f"Red line {rid} event trigger must be on_new_record")
        else:
            errors.append(f"Red line {rid} unknown type {rtype}")

    prefs = content.get("preferences") or {}
    if "max_items" in prefs and int(prefs["max_items"]) < 1:
        errors.append("preferences.max_items must be ≥1")
    if "connect_boost" in prefs:
        try:
            cb = float(prefs["connect_boost"])
            if not 0 <= cb <= 0.5:
                errors.append("preferences.connect_boost must be between 0 and 0.5")
        except (TypeError, ValueError):
            errors.append("preferences.connect_boost must be a number")

    for kid, mult in (content.get("kra_multipliers") or {}).items():
        if kid not in kra_ids:
            errors.append(f"kra_multipliers references unknown KRA {kid}")
        try:
            if not 0.5 <= float(mult) <= 1.5:
                errors.append(f"kra_multipliers[{kid}] must be between 0.5 and 1.5")
        except (TypeError, ValueError):
            errors.append(f"kra_multipliers[{kid}] must be a number")

    if not content.get("label"):
        errors.append("label is required")
    if not content.get("defining_priority"):
        errors.append("defining_priority is required")

    return errors


def normalize_face_weights(weights: dict[str, float]) -> dict[str, float]:
    total = sum(float(v) for v in weights.values()) or 1.0
    return {k: float(v) / total for k, v in weights.items()}


def create_weighting_version(
    session: Session,
    content: dict[str, Any],
    *,
    activate: bool = True,
    kra_ids: set[str] | None = None,
) -> WeightingVersion:
    content = deepcopy(content)
    if "face_weights" in content:
        content["face_weights"] = normalize_face_weights(content["face_weights"])
    errs = validate_weighting(content, kra_ids=kra_ids)
    if errs:
        raise ValueError("; ".join(errs))

    if activate:
        for row in session.query(WeightingVersion).filter_by(is_active=True):
            row.is_active = False

    row = WeightingVersion(
        label=content["label"],
        content_json=content,
        is_active=activate,
    )
    session.add(row)
    session.flush()
    return row


def get_active_weighting(session: Session) -> WeightingVersion:
    row = session.query(WeightingVersion).filter_by(is_active=True).one_or_none()
    if not row:
        raise RuntimeError("No active weighting version — run seed.py")
    return row


def get_weighting_version(session: Session, version_id: int) -> WeightingVersion:
    row = session.get(WeightingVersion, version_id)
    if not row:
        raise ValueError(f"Unknown weighting version {version_id}")
    return row


def list_presets() -> list[dict[str, Any]]:
    out = []
    for key, filename in PRESET_FILES.items():
        path = CONFIG_DIR / filename
        data = load_weighting_yaml(path)
        out.append({"id": key, "label": data.get("label"), "path": filename, "content": data})
    return out


def load_preset(preset_id: str) -> dict[str, Any]:
    if preset_id not in PRESET_FILES:
        raise ValueError(f"Unknown preset {preset_id}")
    return load_weighting_yaml(CONFIG_DIR / PRESET_FILES[preset_id])


def seed_weighting_from_yaml(session: Session) -> WeightingVersion:
    existing = session.query(WeightingVersion).filter_by(is_active=True).one_or_none()
    if existing:
        return existing
    content = load_weighting_yaml()
    return create_weighting_version(session, content, activate=True)


def weighting_to_prompt_block(content: dict[str, Any]) -> str:
    lines = [
        f"# Priority Weighting: {content.get('label')}",
        str(content.get("defining_priority", "")).strip(),
        "",
        f"Face weights: {content.get('face_weights')}",
        f"KRA ranks: {content.get('kra_ranks')}",
        f"Risk appetite: {content.get('risk_appetite')}",
        f"Red lines: {content.get('red_lines')}",
        f"Preferences: {content.get('preferences')}",
    ]
    return "\n".join(lines)
