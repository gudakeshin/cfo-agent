"""Drill-down chat: a streamed tool-use loop over memory search plus read-only
ERP data and what-if tools, with the answer run through the citation gate.

Events emitted while a turn runs (for SSE):
  tool   {"name", "label"}          a tool started
  reset  {}                         text streamed so far was a preamble to a tool call; discard it
  delta  {"text"}                   streamed answer text
  done   {"message": {...}}         the saved assistant message (final, gated text)
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from sqlalchemy.orm import Session

from cfo_agent import llm
from cfo_agent.chat_tools import DATA_TOOLS, ChatDataContext, chat_as_of_week
from cfo_agent.citations import _numeric_values, known_figures, resolve_citation, unverified_figures
from cfo_agent.memory.tools import CHAT_TOOLS, dispatch_tool
from cfo_agent.models import ChatMessage, Digest, DigestItem
from cfo_agent.settings import get_settings

Emit = Callable[[str, dict], None]

CITE_RE = re.compile(r"\[((?:erp|metric):[^\[\]\s]+)\]")

TOOL_LABELS = {
    "get_metric": "Reading a metric",
    "metric_trend": "Pulling the weekly trend",
    "receivables_ageing": "Ageing receivables",
    "list_records": "Querying ERP records",
    "what_if": "Running a scenario",
    "archival_search": "Searching past insights",
    "recall_search": "Searching past conversations",
}


def get_chat_history(session: Session, item_id: int) -> list[ChatMessage]:
    return (
        session.query(ChatMessage)
        .filter_by(digest_item_id=item_id)
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .all()
    )


def message_to_dict(m: ChatMessage) -> dict[str, Any]:
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "created_at": m.created_at,
        "citations": m.citations_json or [],
        "verification": m.verification_json or {},
    }


def _item_evidence(session: Session, item: DigestItem) -> tuple[list[str], list[str], list[float]]:
    """(citation ids, prompt lines, numeric values) for the item's own evidence."""
    cites: list[str] = []
    for lst in (item.citations_json or {}).values():
        cites.extend(lst)
    cites = list(dict.fromkeys(cites))
    snapshot = item.evidence_snapshot_json or {}
    lines: list[str] = []
    values: list[float] = []
    for cid in cites:
        snap = snapshot.get(cid)
        if snap:
            lines.append(f"- {snap.get('summary', '')} [{cid}]")
            _numeric_values(snap.get("record"), values)
            continue
        r = resolve_citation(session, cid)
        if r.ok:
            lines.append(f"- {r.summary} [{cid}]")
            _numeric_values(r.record, values)
    _numeric_values(item.score_decomp_json, values)
    return cites, lines, values


def gate_answer(
    session: Session, text: str, allowed: set[str], known_values: list[float]
) -> tuple[str, list[str], dict]:
    """Strip citations the answer was not entitled to; flag unmatched figures."""
    used: list[str] = []
    rejected: list[str] = []

    def repl(m: re.Match) -> str:
        cid = m.group(1)
        if cid in allowed and resolve_citation(session, cid).ok:
            used.append(cid)
            return m.group(0)
        rejected.append(cid)
        return ""

    cleaned = CITE_RE.sub(repl, text).replace("  ", " ").strip()
    values = list(known_values)
    for cid in set(used):
        r = resolve_citation(session, cid)
        _numeric_values(r.record, values)
    prose = CITE_RE.sub(" ", cleaned)
    flagged = unverified_figures([prose], known_figures(values))
    verification = {
        "unverified_figures": flagged,
        "rejected_citations": list(dict.fromkeys(rejected)),
        "uncited": not used,
    }
    return cleaned, list(dict.fromkeys(used)), verification


def converse(
    session: Session,
    system: str,
    messages: list[dict[str, Any]],
    ctx: ChatDataContext,
    as_of_week: str,
    emit: Emit,
) -> tuple[str, "llm.LlmBudget"]:
    """The streamed tool-use loop shared by the per-insight chat and the Ask box:
    model turns interleaved with read-only data / memory tools, up to the round
    limit. Returns the final text (not yet gated) and the budget used."""
    tools = DATA_TOOLS + CHAT_TOOLS
    settings = get_settings()
    from cfo_agent.budgetmeter import remaining_usd

    left = remaining_usd(session)
    if left is not None and left <= 0:
        msg = ("This workspace has used its AI budget for the month, so I cannot answer new questions until it "
               "resets or an administrator raises it. The briefing and the data pages are unaffected.")
        emit("delta", {"text": msg})
        return msg, llm.LlmBudget(max_calls=0)
    budget = llm.LlmBudget(max_calls=settings.max_tool_rounds + 1, max_cost_usd=left)

    final_text = ""
    for _round in range(settings.max_tool_rounds + 1):
        if budget.remaining() <= 0:
            break
        round_text: list[str] = []

        def on_text(t: str) -> None:
            round_text.append(t)
            emit("delta", {"text": t})

        blocks, stop_reason = llm.stream_round(
            system=system,
            messages=messages,
            tools=tools,
            purpose="drilldown_chat",
            budget=budget,
            on_text=on_text,
        )
        messages.append({"role": "assistant", "content": blocks})
        tool_uses = [b for b in blocks if b["type"] == "tool_use"]
        text = "".join(b["text"] for b in blocks if b["type"] == "text")
        if not tool_uses or stop_reason != "tool_use":
            final_text = text
            break
        if round_text:
            emit("reset", {})  # preamble before a tool call — not part of the answer
        results = []
        for tu in tool_uses:
            emit("tool", {"name": tu["name"], "label": TOOL_LABELS.get(tu["name"], tu["name"])})
            if tu["name"] in {t["name"] for t in DATA_TOOLS}:
                out = ctx.dispatch(tu["name"], tu["input"])
            else:
                out = dispatch_tool(session, tu["name"], tu["input"], allow_writes=False, as_of_week=as_of_week)
                ctx.tools_used.append(tu["name"])
            results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": out})
        messages.append({"role": "user", "content": results})

    if not final_text:
        final_text = (
            "I couldn't complete that within the step limit. Try a narrower question "
            "(one metric, one customer or one scenario)."
        )
    return final_text, budget


def run_chat(
    session: Session,
    item_id: int,
    user_message: str,
    emit: Emit | None = None,
) -> ChatMessage:
    emit = emit or (lambda _e, _d: None)
    item = session.get(DigestItem, item_id)
    if not item:
        raise ValueError(f"Unknown digest item {item_id}")
    digest = session.get(Digest, item.digest_id)
    as_of_week = chat_as_of_week(session, digest.week if digest else "")

    user_row = ChatMessage(digest_item_id=item_id, role="user", content=user_message)
    session.add(user_row)
    session.flush()
    try:
        from cfo_agent.memory.recall import embed_chat_message

        embed_chat_message(session, user_row.id, user_message)
    except Exception:  # noqa: BLE001 — chat never fails on embed
        pass

    item_cites, evidence_lines, evidence_values = _item_evidence(session, item)
    ctx = ChatDataContext(session, as_of_week)

    system = (
        "You are the CFO agent's drill-down assistant for one digest insight. You advise; you never act.\n"
        "Rules:\n"
        "1. Every figure you state must come from the EVIDENCE below or from a tool result, and must be "
        "followed immediately by its source in square brackets, e.g. ₹21.4 Cr [metric:cash_balance_cr:2026-W28]. "
        "Use only citation IDs that appear in EVIDENCE or in a tool result's citations/citation fields. "
        "Cite a total or aggregate with the tool's totals_citation (or metric citation), and a single "
        "record's figure with that record's citation.\n"
        "2. Never calculate new numbers yourself — use what_if for scenarios and the data tools for facts. "
        "For money, quote the *_inr or *_cr fields as given.\n"
        f"3. Data is available up to {as_of_week}; do not speculate beyond it.\n"
        "4. If the data cannot answer the question, say so plainly.\n"
        "5. Be brief: a direct answer first, then at most a few bullets. No preamble.\n\n"
        f"ITEM: {item.title}\nWHAT: {item.what}\nWHY: {item.why}\n"
        f"CONNECTS: {item.connects}\nACTION: {item.action}\nKRAs: {item.kra_ids_json}\n\n"
        "EVIDENCE:\n" + "\n".join(evidence_lines)
    )

    messages: list[dict[str, Any]] = [
        {"role": m.role, "content": m.content}
        for m in get_chat_history(session, item_id)
        if m.role in ("user", "assistant")
    ]
    final_text, budget = converse(session, system, messages, ctx, as_of_week, emit)

    tool_values: list[float] = []
    for r in ctx.results:
        _numeric_values(r, tool_values)
    allowed = set(item_cites) | ctx.citations
    cleaned, used, verification = gate_answer(session, final_text, allowed, evidence_values + tool_values)
    verification["tools_used"] = list(dict.fromkeys(ctx.tools_used))
    verification["as_of_week"] = as_of_week
    verification["cost_usd"] = round(budget.total_cost(), 4)

    assistant = ChatMessage(
        digest_item_id=item_id,
        role="assistant",
        content=cleaned,
        citations_json=used,
        verification_json=verification,
    )
    session.add(assistant)
    session.flush()
    try:
        from cfo_agent.memory.recall import embed_chat_message

        embed_chat_message(session, assistant.id, CITE_RE.sub("", cleaned))
    except Exception:  # noqa: BLE001
        pass
    emit("done", {"message": json.loads(json.dumps(message_to_dict(assistant), default=str))})
    return assistant


def chat(session: Session, item_id: int, user_message: str) -> ChatMessage:
    """Non-streaming entry point (same loop, events discarded)."""
    return run_chat(session, item_id, user_message)
