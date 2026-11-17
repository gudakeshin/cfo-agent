"""Persist digest + items from surfaced pipeline output."""

from __future__ import annotations

from sqlalchemy.orm import Session

from cfo_agent.models import Digest, DigestItem, KraVersion, PipelineRun, WeightingVersion
from cfo_agent.pipeline.step6_surface import SurfacedItem


def persist_digest(
    session: Session,
    *,
    week: str,
    weighting: WeightingVersion,
    kra_version: KraVersion,
    run: PipelineRun,
    headline: str,
    surfaced: list[SurfacedItem],
    empty_bar: bool,
    data_health: dict,
) -> Digest:
    digest = Digest(
        week=week,
        weighting_version_id=weighting.id,
        kra_version_id=kra_version.id,
        pipeline_run_id=run.id,
        headline=headline,
        weighting_label=weighting.label,
        data_health_json=data_health,
        empty_bar=empty_bar,
        email_status="pending",
    )
    session.add(digest)
    session.flush()

    for rank, s in enumerate(surfaced, start=1):
        syn = s.synthesis
        fi = s.filter_item
        item = DigestItem(
            digest_id=digest.id,
            rank=rank,
            title=syn.title,
            what=syn.what.text,
            why=syn.why.text,
            connects=syn.connects.text,
            action=syn.action.text,
            kra_ids_json=fi.kra_ids,
            signal_ids_json=fi.signal_ids,
            citations_json=s.citations,
            is_red_line=fi.is_red_line,
            priority_score=fi.priority_score,
            score_decomp_json=fi.score_decomp,
            relationship_ids_json=fi.relationship_ids,
            evidence_snapshot_json=getattr(s, "evidence_snapshot", None) or {},
            verification_json=getattr(s, "verification", None) or {},
        )
        session.add(item)
    session.flush()
    session.refresh(digest)
    return digest
