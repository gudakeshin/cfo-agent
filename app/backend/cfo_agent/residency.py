"""Where model calls are processed, and the DATA_RESIDENCY=india guard.

Bedrock model IDs carry their routing in a prefix:
  anthropic.claude-…        in-region (processed in AWS_REGION)
  in.anthropic.claude-…     India geo profile (ap-south-1 ↔ ap-south-2)
  apac./us./eu./global.…    cross-region — may be processed outside India
Vertex AI processes a regional endpoint in VERTEX_REGION; "global" may route anywhere.
Regional model availability changes often: confirm the chosen model ID is offered in
the region (Bedrock: GetInferenceProfile; Vertex: the partner-model locations table).
"""

from __future__ import annotations

import re

from cfo_agent.settings import Settings, get_settings

INDIA_AWS_REGIONS = {"ap-south-1", "ap-south-2"}  # Mumbai, Hyderabad
INDIA_GCP_REGIONS = {"asia-south1", "asia-south2"}  # Mumbai, Delhi
_BEDROCK_PREFIX = re.compile(r"^([a-z]{2,6})\.anthropic\.")


def bedrock_routing(model_id: str) -> str:
    """'in-region', 'geo:<prefix>', or 'unknown' (ARNs and unrecognised IDs)."""
    if model_id.startswith("anthropic."):
        return "in-region"
    m = _BEDROCK_PREFIX.match(model_id)
    if m:
        return f"geo:{m.group(1)}"
    return "unknown"


def processing_location(s: Settings | None = None) -> dict:
    s = s or get_settings()
    if s.llm_provider == "bedrock":
        routing = bedrock_routing(s.anthropic_model)
        where = {
            "in-region": f"AWS {s.aws_region}",
            "geo:in": "AWS India (ap-south-1 / ap-south-2)",
        }.get(routing, f"AWS, routed via '{routing}' from {s.aws_region} — may leave the region")
    elif s.llm_provider == "vertex":
        where = f"Google Cloud {s.vertex_region}"
    else:
        where = "Anthropic API (outside India)"
    return {
        "provider": s.llm_provider,
        "model": s.anthropic_model,
        "processed_in": where,
        "residency_policy": s.data_residency or None,
        "news_search": s.news_effective_provider or "off",
    }


def residency_errors(s: Settings | None = None) -> list[str]:
    s = s or get_settings()
    errors: list[str] = []
    p = s.llm_provider
    if p not in ("anthropic", "bedrock", "vertex"):
        return [f"Unknown LLM_PROVIDER {p!r} (use anthropic, bedrock or vertex)"]
    if p == "bedrock" and not s.aws_region:
        errors.append("LLM_PROVIDER=bedrock needs AWS_REGION")
    if p == "vertex" and not (s.vertex_project_id and s.vertex_region):
        errors.append("LLM_PROVIDER=vertex needs VERTEX_PROJECT_ID and VERTEX_REGION")
    if s.news_provider not in ("same", "anthropic", "off"):
        errors.append(f"Unknown NEWS_PROVIDER {s.news_provider!r} (use same, anthropic or off)")

    policy = (s.data_residency or "").strip().lower()
    if not policy:
        return errors
    if policy != "india":
        return errors + [f"Unknown DATA_RESIDENCY {s.data_residency!r} (supported: india)"]

    if p == "anthropic":
        errors.append(
            "DATA_RESIDENCY=india: the Anthropic API does not process in India. Use "
            "LLM_PROVIDER=bedrock (AWS_REGION=ap-south-1 or ap-south-2) or "
            "LLM_PROVIDER=vertex (VERTEX_REGION=asia-south1 or asia-south2)."
        )
    elif p == "bedrock":
        if s.aws_region and s.aws_region not in INDIA_AWS_REGIONS:
            errors.append(f"DATA_RESIDENCY=india: AWS_REGION must be ap-south-1 or ap-south-2, not {s.aws_region}")
        routing = bedrock_routing(s.anthropic_model)
        if routing not in ("in-region", "geo:in"):
            errors.append(
                f"DATA_RESIDENCY=india: model '{s.anthropic_model}' routes via '{routing}', which may "
                "process outside India. Use the India geo profile (in.anthropic.…) or an in-region "
                "model ID (anthropic.…)."
            )
    elif p == "vertex" and s.vertex_region and s.vertex_region not in INDIA_GCP_REGIONS:
        errors.append(f"DATA_RESIDENCY=india: VERTEX_REGION must be asia-south1 or asia-south2, not {s.vertex_region}")
    return errors
