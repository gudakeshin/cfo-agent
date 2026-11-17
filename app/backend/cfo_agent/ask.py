"""The Ask box: the CFO questions a whole briefing, not one insight.

Same machinery as the per-insight chat (read-only data tools, what-if, memory search,
streamed, answers run through the citation gate) with the evidence of every insight in
the briefing in context, so "which of these is most urgent?" or "what's behind the
cash drop?" needs no tool call when the briefing already holds the answer.

History is private to the signed-in user (`ask_messages.user_email`).
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from cfo_agent.chat import CITE_RE, Emit, _item_evidence, converse, gate_answer
from cfo_agent.chat_tools import ChatDataContext, chat_as_of_week
from cfo_agent.citations import _numeric_values
from cfo_agent.drivers import driver_citations
from cfo_agent.models import AskMessage, Digest
from cfo_agent.timeutil import format_inr

HISTORY_TURNS = 12


def latest_digest(session: Session) -> Digest | None:
    return session.query(Digest).order_by(Digest.week.desc(), Digest.created_at.desc(), Digest.id.desc()).first()


def resolve_digest(session: Session, digest_id: int | None) -> Digest | None:
    return session.get(Digest, digest_id) if digest_id else latest_digest(session)


def ask_history(session: Session, user_email: str, digest_id: int | None) -> list[AskMessage]:
    return (
        session.query(AskMessage)
        .filter(AskMessage.user_email == user_email, AskMessage.digest_id == digest_id)
        .order_by(AskMessage.created_at.asc(), AskMessage.id.asc())
        .all()
    )


def clear_history(session: Session, user_email: str, digest_id: int | None) -> int:
    n = (
        session.query(AskMessage)
        .filter(AskMessage.user_email == user_email, AskMessage.digest_id == digest_id)
        .delete(synchronize_session=False)
    )
    session.flush()
    return n


def message_dict(m: AskMessage) -> dict[str, Any]:
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "created_at": m.created_at,
        "citations": m.citations_json or [],
        "verification": m.verification_json or {},
    }


def _driver_lines(item) -> list[str]:
    drivers = (item.score_decomp_json or {}).get("drivers") or {}
    out = []
    for r in drivers.get("rows") or []:
        now = format_inr(r["current_paise"])
        before = "" if r.get("previous_paise") is None else f" (before {format_inr(r['previous_paise'])})"
        cite = f" [{r['citation']}]" if r.get("citation") else ""
        out.append(f"  · {r['group']} {r['label']}: {now}{before}{cite}")
    return out


def briefing_context(session: Session, digest: Digest) -> tuple[str, set[str], list[float]]:
    """(prompt text, allowed citation ids, known numeric values) for the whole briefing."""
    allowed: set[str] = set()
    values: list[float] = []
    blocks: list[str] = []
    seen_lines: set[str] = set()
    for item in sorted(digest.items, key=lambda i: i.rank):
        cites, lines, vals = _item_evidence(session, item)
        allowed.update(cites)
        allowed.update(driver_citations((item.score_decomp_json or {}).get("drivers")))
        values.extend(vals)
        evidence = [ln for ln in lines if ln not in seen_lines]
        seen_lines.update(evidence)
        flag = " [RED LINE]" if item.is_red_line else ""
        cont = (item.score_decomp_json or {}).get("continuity") or {}
        status = f" ({'continuing since ' + cont['since_week'] if cont.get('status') == 'continuing' else 'new this week'})" if cont else ""
        block = [
            f"#{item.rank}{flag} {item.title}{status}",
            f"  WHAT: {item.what}",
            f"  WHY: {item.why}",
            f"  ACTION: {item.action}",
        ]
        drv = _driver_lines(item)
        if drv:
            block.append("  WHAT MOVED IT:")
            block.extend(drv)
        if evidence:
            block.append("  EVIDENCE:")
            block.extend(f"  {ln}" for ln in evidence)
        blocks.append("\n".join(block))
    return "\n\n".join(blocks), allowed, values


def run_ask(
    session: Session,
    user_email: str,
    digest_id: int | None,
    user_message: str,
    emit: Emit | None = None,
) -> AskMessage:
    emit = emit or (lambda _e, _d: None)
    digest = resolve_digest(session, digest_id)
    if digest is None:
        raise ValueError("There is no briefing to ask about yet.")
    as_of_week = chat_as_of_week(session, digest.week)

    session.add(AskMessage(digest_id=digest.id, user_email=user_email, role="user", content=user_message))
    session.flush()

    context, allowed_cites, context_values = briefing_context(session, digest)
    ctx = ChatDataContext(session, as_of_week)

    system = (
        "You are the CFO agent's Ask assistant. The CFO is questioning this week's briefing as a whole. "
        "You advise; you never act.\n"
        "Rules:\n"
        "1. Every figure you state must come from the BRIEFING below or from a tool result, and must be "
        "followed immediately by its source in square brackets, e.g. ₹21.4 Cr [metric:cash_balance_cr:2026-W28]. "
        "Use only citation IDs that appear in the BRIEFING or in a tool result's citations/citation fields. "
        "Cite a total with the tool's totals_citation (or metric citation) and a single record with its own.\n"
        "2. Never calculate new numbers yourself — use what_if for scenarios and the data tools for facts. "
        "For money, quote the *_inr or *_cr fields as given.\n"
        f"3. Data is available up to {as_of_week}; do not speculate beyond it.\n"
        "4. If the briefing and the data cannot answer the question, say so plainly. Never invent a cause.\n"
        "5. Prefer what the BRIEFING already says before calling a tool; call tools for anything it does not cover.\n"
        "6. Be brief: a direct answer first, then at most a few bullets. No preamble.\n\n"
        f"BRIEFING for {digest.week} — {digest.headline}\n\n{context}"
    )

    history = ask_history(session, user_email, digest.id)
    messages: list[dict[str, Any]] = [{"role": m.role, "content": m.content} for m in history[-HISTORY_TURNS:]]

    final_text, budget = converse(session, system, messages, ctx, as_of_week, emit)

    tool_values: list[float] = []
    for r in ctx.results:
        _numeric_values(r, tool_values)
    cleaned, used, verification = gate_answer(
        session, final_text, allowed_cites | ctx.citations, context_values + tool_values
    )
    verification["tools_used"] = list(dict.fromkeys(ctx.tools_used))
    verification["as_of_week"] = as_of_week
    verification["cost_usd"] = round(budget.total_cost(), 4)

    assistant = AskMessage(
        digest_id=digest.id, user_email=user_email, role="assistant",
        content=cleaned, citations_json=used, verification_json=verification,
    )
    session.add(assistant)
    session.flush()
    emit("done", {"message": json.loads(json.dumps(message_dict(assistant), default=str))})
    return assistant
