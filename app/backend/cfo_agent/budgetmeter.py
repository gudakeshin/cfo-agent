"""AI spend metering and the per-tenant monthly ceiling.

Spend is what the app itself recorded this calendar month: digest runs, chat and Ask
answers, and meeting briefs. (News refresh and memory consolidation are not metered.)
The ceiling (MONTHLY_LLM_BUDGET_USD, overridable per tenant; 0 = none) is applied
before the money is spent: a digest run is capped to what remains and falls back to
deterministic text when nothing does, and chat / Ask say so instead of calling the model."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from cfo_agent.models import AskMessage, ChatMessage, MeetingBrief, PipelineRun
from cfo_agent.settings import get_settings


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def month_start(now: datetime | None = None) -> datetime:
    n = now or _now()
    return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _message_cost(session: Session, model, since: datetime) -> float:
    total = 0.0
    for (v,) in session.query(model.verification_json).filter(model.role == "assistant", model.created_at >= since):
        total += float((v or {}).get("cost_usd") or 0)
    return total


def spend_this_month(session: Session, now: datetime | None = None) -> dict[str, float]:
    since = month_start(now)
    pipeline = sum(c or 0.0 for (c,) in session.query(PipelineRun.cost_usd).filter(PipelineRun.created_at >= since))
    briefs = sum(c or 0.0 for (c,) in session.query(MeetingBrief.cost_usd).filter(MeetingBrief.created_at >= since))
    parts = {
        "digests": float(pipeline),
        "chat": _message_cost(session, ChatMessage, since),
        "ask": _message_cost(session, AskMessage, since),
        "briefs": float(briefs),
    }
    parts["total"] = round(sum(parts.values()), 4)
    return {k: round(v, 4) for k, v in parts.items()}


def monthly_budget_usd() -> float:
    return float(get_settings().monthly_llm_budget_usd or 0.0)


def remaining_usd(session: Session) -> float | None:
    """Dollars left this month, never below 0; None when the tenant has no ceiling."""
    budget = monthly_budget_usd()
    if budget <= 0:
        return None
    return max(0.0, round(budget - spend_this_month(session)["total"], 4))
