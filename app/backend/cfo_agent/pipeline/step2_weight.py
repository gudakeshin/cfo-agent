from __future__ import annotations

from dataclasses import dataclass

from cfo_agent.kra import KraFramework
from cfo_agent.pipeline.step1_map import MappedSignal
from cfo_agent.signals import priority_score, rank_boost


@dataclass
class WeightedSignal:
    mapped: MappedSignal
    primary_kra: str
    face_weight: float
    rank_boost: float
    severity: float
    priority_score: float
    score_decomp: dict


def step2_weight(
    mapped: list[MappedSignal],
    weighting: dict,
    framework: KraFramework,
) -> list[WeightedSignal]:
    face_weights = weighting.get("face_weights") or {}
    kra_ranks = list(weighting.get("kra_ranks") or [])
    out: list[WeightedSignal] = []
    for m in mapped:
        if not m.kra_ids:
            continue
        # Primary KRA = highest rank among assigned (lowest index) else first
        primary = m.kra_ids[0]
        best_idx = 10_000
        for kid in m.kra_ids:
            if kid in kra_ranks:
                idx = kra_ranks.index(kid)
                if idx < best_idx:
                    best_idx = idx
                    primary = kid
        face = framework.kras[primary].face
        fw = float(face_weights.get(face, 0.1))
        rb = rank_boost(primary, kra_ranks)
        sev = float(m.signal.severity)
        score = priority_score(fw, rb, sev)
        out.append(
            WeightedSignal(
                mapped=m,
                primary_kra=primary,
                face_weight=fw,
                rank_boost=rb,
                severity=sev,
                priority_score=score,
                score_decomp={
                    "face": face,
                    "face_weight": fw,
                    "rank_boost": rb,
                    "severity": sev,
                    "priority_score": score,
                    "primary_kra": primary,
                    "metric_id": m.signal.metric_id,
                    "value": m.signal.value,
                    "delta_wow": m.signal.delta_wow,
                },
            )
        )
    out.sort(key=lambda w: -w.priority_score)
    return out
