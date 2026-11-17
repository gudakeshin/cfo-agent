"""Industry KRA framework templates (config/templates/*.yaml, plus the default
manufacturing framework). A template is a starting point: loading one fills the KRA
editor (and optionally a weighting preset); nothing changes until it is saved."""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache

import yaml

from cfo_agent.settings import CONFIG_DIR

TEMPLATE_DIR = CONFIG_DIR / "templates"
MANUFACTURING = {
    "id": "manufacturing",
    "name": "Mid-market manufacturer (default)",
    "description": "Working capital, covenants, GST and acquisition readiness for an Indian manufacturer.",
    "data_needed": ["Sales / receipts", "Purchases / payments", "Expenses by cost centre", "Daily cash", "GST events"],
}


@lru_cache(maxsize=8)
def _read(template_id: str) -> dict:
    if template_id == "manufacturing":
        body = yaml.safe_load((CONFIG_DIR / "kra_framework.yaml").read_text(encoding="utf-8"))
        preset = yaml.safe_load((CONFIG_DIR / "priority_weighting.yaml").read_text(encoding="utf-8"))
        return {"template": MANUFACTURING, "framework": body, "weighting_preset": preset}
    path = TEMPLATE_DIR / f"{template_id}.yaml"
    if not path.is_file() or path.parent != TEMPLATE_DIR:
        raise KeyError(template_id)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    meta = data.pop("template")
    preset = data.pop("weighting_preset", None)
    return {"template": meta, "framework": data, "weighting_preset": preset}


def template_ids() -> list[str]:
    return ["manufacturing"] + sorted(p.stem for p in TEMPLATE_DIR.glob("*.yaml"))


def list_templates() -> list[dict]:
    out = []
    for tid in template_ids():
        t = _read(tid)
        fw = t["framework"]
        kras = [k for f in (fw.get("faces") or {}).values() for k in f.get("kras", [])]
        out.append({**t["template"], "kras": len(kras),
                    "grounded_kras": sum(1 for k in kras if k.get("grounding", "erp") == "erp")})
    return out


def load_template(template_id: str) -> dict:
    if template_id not in template_ids():
        raise KeyError(template_id)
    return deepcopy(_read(template_id))
