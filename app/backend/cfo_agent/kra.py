from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from cfo_agent.models import KraVersion, WeightingVersion
from cfo_agent.settings import CONFIG_DIR

logger = logging.getLogger(__name__)

class FrameworkError(ValueError):
    """A KRA framework failed validation; the message is safe to show to users."""


GROUNDED = "erp"
UNGROUNDED = "none"

# Cache: content_hash → framework (hash-keyed so tmp test DBs cannot collide on id)
_fw_cache: dict[str, Any] = {}


@dataclass
class KRA:
    id: str
    name: str
    face: str
    grounding: str
    metrics: list[str]
    concerns: list[str]
    note: str | None = None


@dataclass
class Relationship:
    id: str
    from_kra: str
    to_kra: str
    description: str


@dataclass
class KraFramework:
    version: str
    label: str
    india_regulatory_context: str
    kras: dict[str, KRA]
    relationships: list[Relationship]
    metric_map: dict[str, list[str]]
    faces: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def grounded_kras(self) -> list[KRA]:
        return [k for k in self.kras.values() if k.grounding == GROUNDED]

    def kra_ids(self) -> set[str]:
        return set(self.kras.keys())

    def metrics_for_signals(self) -> set[str]:
        """Metrics that appear in metric_map and belong to at least one grounded KRA."""
        out: set[str] = set()
        for metric, kra_ids in self.metric_map.items():
            if any(self.kras[k].grounding == GROUNDED for k in kra_ids if k in self.kras):
                out.add(metric)
        return out


def load_framework_yaml(path: Path | None = None) -> dict[str, Any]:
    path = path or (CONFIG_DIR / "kra_framework.yaml")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_framework(data: dict[str, Any]) -> KraFramework:
    kras: dict[str, KRA] = {}
    faces_raw = data.get("faces", {})
    for face_key, face_data in faces_raw.items():
        for k in face_data.get("kras", []):
            kid = k["id"]
            if kid in kras:
                raise FrameworkError(f"Duplicate KRA id: {kid}")
            kras[kid] = KRA(
                id=kid,
                name=k["name"],
                face=face_key,
                grounding=k.get("grounding", GROUNDED),
                metrics=list(k.get("metrics") or []),
                concerns=list(k.get("concerns") or []),
                note=k.get("note"),
            )

    relationships: list[Relationship] = []
    for r in data.get("relationships", []):
        relationships.append(
            Relationship(
                id=r["id"],
                from_kra=r["from"],
                to_kra=r["to"],
                description=r["description"],
            )
        )

    # Rebuild metric_map from per-KRA metrics (server-side source of truth)
    metric_map: dict[str, list[str]] = {}
    for k in kras.values():
        for m in k.metrics:
            metric_map.setdefault(m, []).append(k.id)
    # Preserve any explicit map entries for metrics not on KRAs (shouldn't happen)
    for metric, kids in (data.get("metric_map") or {}).items():
        if metric not in metric_map:
            metric_map[str(metric)] = list(kids)

    return KraFramework(
        version=str(data.get("version", "1.0")),
        label=str(data.get("label", "")),
        india_regulatory_context=str(data.get("india_regulatory_context", "")),
        kras=kras,
        relationships=relationships,
        metric_map=metric_map,
        faces=faces_raw,
        raw=data,
    )


def validate_framework_hard(fw: KraFramework, session: Session | None = None) -> list[str]:
    """Hard errors → 422. Soft count floors are separate."""
    from cfo_agent.signals import SIGNAL_REGISTRY

    if session is not None:
        from cfo_agent.metrics_dsl import ensure_custom_metrics

        ensure_custom_metrics(session)  # declarative metrics count as known metrics

    errors: list[str] = []
    kra_ids = fw.kra_ids()

    rel_ids = [r.id for r in fw.relationships]
    if len(rel_ids) != len(set(rel_ids)):
        errors.append("Duplicate relationship ids")

    for r in fw.relationships:
        if r.from_kra not in kra_ids:
            errors.append(f"Relationship {r.id} from unknown KRA {r.from_kra}")
        if r.to_kra not in kra_ids:
            errors.append(f"Relationship {r.id} to unknown KRA {r.to_kra}")

    for metric, kids in fw.metric_map.items():
        if metric not in SIGNAL_REGISTRY:
            errors.append(f"Unknown metric id (not in SIGNAL_REGISTRY): {metric}")
        for kid in kids:
            if kid not in kra_ids:
                errors.append(f"metric_map[{metric}] references unknown KRA {kid}")

    for k in fw.kras.values():
        if k.grounding == UNGROUNDED and k.metrics:
            errors.append(f"Ungrounded KRA {k.id} should not list metrics")
        if k.grounding == GROUNDED and not k.metrics:
            errors.append(f"Grounded KRA {k.id} has no metrics")
        for m in k.metrics:
            if m not in SIGNAL_REGISTRY:
                errors.append(f"KRA {k.id} references unknown metric {m}")

    return errors


def validate_framework_soft(fw: KraFramework) -> list[str]:
    """Advisory warnings (count floors) — do not block save."""
    warnings: list[str] = []
    if len(fw.relationships) < 6:
        warnings.append(f"Expected ≥6 relationships, got {len(fw.relationships)}")
    grounded = fw.grounded_kras()
    if len(grounded) < 8:
        warnings.append(f"Expected ≥8 grounded KRAs, got {len(grounded)}")
    return warnings


def validate_framework(fw: KraFramework) -> list[str]:
    """Legacy combined validator (hard + soft) for seed path."""
    return validate_framework_hard(fw) + validate_framework_soft(fw)


def framework_hash(yaml_text: str) -> str:
    return hashlib.sha256(yaml_text.encode("utf-8")).hexdigest()


def framework_to_yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def framework_to_dict(fw: KraFramework) -> dict[str, Any]:
    """Serialize framework back to YAML-shaped dict with rebuilt faces/metric_map."""
    faces: dict[str, Any] = {}
    for kid, k in fw.kras.items():
        face = faces.setdefault(k.face, {"kras": []})
        entry: dict[str, Any] = {
            "id": k.id,
            "name": k.name,
            "grounding": k.grounding,
            "metrics": list(k.metrics),
            "concerns": list(k.concerns),
        }
        if k.note:
            entry["note"] = k.note
        face["kras"].append(entry)
    # Preserve face labels from raw if present
    for face_key, face_data in (fw.faces or {}).items():
        if face_key in faces and isinstance(face_data, dict) and "label" in face_data:
            faces[face_key]["label"] = face_data["label"]

    return {
        "version": fw.version,
        "label": fw.label,
        "india_regulatory_context": fw.india_regulatory_context,
        "faces": faces,
        "relationships": [
            {
                "id": r.id,
                "from": r.from_kra,
                "to": r.to_kra,
                "description": r.description,
            }
            for r in fw.relationships
        ],
        "metric_map": fw.metric_map,
    }


def upsert_kra_version(session: Session, path: Path | None = None) -> KraVersion:
    """Seed-only: insert from YAML only when KraVersion table is empty."""
    existing = (
        session.query(KraVersion).order_by(KraVersion.id.desc()).first()
    )
    if existing:
        return existing

    path = path or (CONFIG_DIR / "kra_framework.yaml")
    yaml_text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(yaml_text)
    fw = parse_framework(data)
    errs = validate_framework_hard(fw, session)
    if errs:
        raise ValueError("Invalid KRA framework: " + "; ".join(errs))

    h = framework_hash(yaml_text)
    row = KraVersion(version=fw.version, content_hash=h, content_yaml=yaml_text)
    session.add(row)
    session.flush()
    return row


def get_active_kra_version(session: Session) -> KraVersion:
    row = session.query(KraVersion).order_by(KraVersion.id.desc()).first()
    if not row:
        return upsert_kra_version(session)
    return row


def get_active_framework(session: Session | None = None) -> KraFramework:
    """Read latest KraVersion from DB; YAML file is fallback only when table empty."""
    if session is not None:
        ver = get_active_kra_version(session)
        if ver.content_hash in _fw_cache:
            return _fw_cache[ver.content_hash]
        data = yaml.safe_load(ver.content_yaml)
        fw = parse_framework(data)
        _fw_cache[ver.content_hash] = fw
        return fw

    # No session — try a short-lived one; fall back to YAML
    try:
        from cfo_agent.db import get_session

        s = get_session()
        try:
            if s.query(KraVersion).count() > 0:
                return get_active_framework(s)
        finally:
            s.close()
    except Exception:  # noqa: BLE001
        pass
    path = CONFIG_DIR / "kra_framework.yaml"
    return parse_framework(load_framework_yaml(path))


def _bump_version(current: str) -> str:
    """Auto-bump version string: 1.0 → 1.1, 1.0+abc → next numeric."""
    base = current.split("+")[0]
    parts = base.split(".")
    try:
        if len(parts) >= 2:
            parts[-1] = str(int(parts[-1]) + 1)
            return ".".join(parts)
        return f"{int(base) + 1}"
    except ValueError:
        return f"{base}.1"


def reconcile_weighting_after_kra_change(
    session: Session, fw: KraFramework
) -> WeightingVersion | None:
    """If active weighting ranks reference deleted KRA ids, strip them into a new version."""
    from cfo_agent.memory.blocks import set_block_content
    from cfo_agent.weighting import create_weighting_version, get_active_weighting, weighting_to_prompt_block

    try:
        active = get_active_weighting(session)
    except RuntimeError:
        return None

    content = dict(active.content_json or {})
    ranks = list(content.get("kra_ranks") or [])
    valid = fw.kra_ids()
    cleaned = [r for r in ranks if r in valid]
    if cleaned == ranks:
        return None

    content["kra_ranks"] = cleaned
    content["label"] = f"{content.get('label', 'Weighting')} (KRA sync)"
    # Pass kra_ids from the new framework — avoids uncommitted-session visibility issues
    row = create_weighting_version(session, content, activate=True, kra_ids=fw.kra_ids())
    try:
        set_block_content(
            session,
            "priority_weighting",
            weighting_to_prompt_block(content),
            edited_by="human",
            reason="KRA sync — stripped deleted ranks",
            enforce_agent_rules=False,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to resync priority_weighting after KRA change: %s", e)
    return row


def create_kra_version(
    session: Session, data: dict[str, Any]
) -> tuple[KraVersion, list[str], WeightingVersion | None]:
    """
    Create a new KraVersion from framework body.
    Returns (version, soft_warnings, reconciled_weighting|None).
    Raises ValueError on hard errors.
    """
    from cfo_agent.memory.blocks import set_block_content

    try:
        fw = parse_framework(data)
    except FrameworkError:
        raise
    except (KeyError, TypeError, AttributeError) as e:
        raise FrameworkError(f"Framework is missing a required field ({type(e).__name__}: {e})") from None
    hard = validate_framework_hard(fw, session)
    if hard:
        raise FrameworkError("; ".join(hard))
    warnings = validate_framework_soft(fw)

    # Rebuild serializable dict with server-side metric_map
    out = framework_to_dict(fw)
    # Auto-bump version from latest
    latest = session.query(KraVersion).order_by(KraVersion.id.desc()).first()
    if latest:
        out["version"] = _bump_version(latest.version)
    yaml_text = framework_to_yaml(out)
    h = framework_hash(yaml_text)

    same = session.query(KraVersion).filter_by(content_hash=h).one_or_none()
    if same:
        return same, warnings, None

    # Ensure unique version string
    ver = out["version"]
    collision = session.query(KraVersion).filter_by(version=ver).one_or_none()
    if collision:
        ver = f"{ver}+{h[:8]}"
        out["version"] = ver
        yaml_text = framework_to_yaml(out)
        h = framework_hash(yaml_text)

    row = KraVersion(version=ver, content_hash=h, content_yaml=yaml_text)
    session.add(row)
    session.flush()
    fw_parsed = parse_framework(out)
    _fw_cache[h] = fw_parsed

    # Resync kra_framework memory block
    try:
        set_block_content(
            session,
            "kra_framework",
            framework_to_prompt_block(fw_parsed),
            edited_by="human",
            reason="KRA editor save",
            enforce_agent_rules=False,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to resync kra_framework block: %s", e)

    reconciled = reconcile_weighting_after_kra_change(session, fw_parsed)
    return row, warnings, reconciled


def framework_to_prompt_block(fw: KraFramework) -> str:
    lines = [
        f"# KRA Framework v{fw.version}: {fw.label}",
        fw.india_regulatory_context.strip(),
        "",
        "## KRAs",
    ]
    for kid, k in fw.kras.items():
        g = "grounded" if k.grounding == GROUNDED else "no live feed in v1"
        lines.append(f"- {kid} [{k.face}/{g}] {k.name}: metrics={k.metrics}")
    lines.append("")
    lines.append("## Relationships")
    for r in fw.relationships:
        lines.append(f"- {r.id}: {r.from_kra} → {r.to_kra}: {r.description}")
    return "\n".join(lines)
