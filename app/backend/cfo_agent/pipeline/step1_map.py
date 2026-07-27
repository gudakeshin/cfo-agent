from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from cfo_agent.models import Signal
from cfo_agent.signals import compute_all_signals


@dataclass
class MappedSignal:
    signal: Signal
    kra_ids: list[str]


def step1_map(session: Session, week: str) -> list[MappedSignal]:
    signals = compute_all_signals(session, week, persist=True)
    return [MappedSignal(signal=s, kra_ids=list(s.kra_ids_json or [])) for s in signals]
