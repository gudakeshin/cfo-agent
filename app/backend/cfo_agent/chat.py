"""P1 drill-down chat — tool-use loop with search-only memory tools."""

from __future__ import annotations

from sqlalchemy.orm import Session

from cfo_agent.citations import resolve_citation
from cfo_agent.llm import LlmBudget, complete_with_tools
from cfo_agent.memory.tools import CHAT_TOOLS, dispatch_tool
from cfo_agent.models import ChatMessage, DigestItem
from cfo_agent.settings import get_settings


def get_chat_history(session: Session, item_id: int) -> list[ChatMessage]:
    return (
        session.query(ChatMessage)
        .filter_by(digest_item_id=item_id)
        .order_by(ChatMessage.created_at.asc())
        .all()
    )


def chat(
    session: Session,
    item_id: int,
    user_message: str,
) -> ChatMessage:
    item = session.get(DigestItem, item_id)
    if not item:
        raise ValueError(f"Unknown digest item {item_id}")

    user_row = ChatMessage(digest_item_id=item_id, role="user", content=user_message)
    session.add(user_row)
    session.flush()
    try:
        from cfo_agent.memory.recall import embed_chat_message

        embed_chat_message(session, user_row.id, user_message)
    except Exception:  # noqa: BLE001 — chat never fails on embed
        pass

    # Grounding evidence
    cites = []
    for lst in (item.citations_json or {}).values():
        cites.extend(lst)
    evidence = []
    for cid in dict.fromkeys(cites):
        r = resolve_citation(session, cid)
        if r.ok:
            evidence.append(f"- {r.summary} [{cid}]")

    system = (
        "You are the CFO agent's drill-down assistant. "
        "Answer only from the item's cited evidence and memory search tools. "
        "If you cannot ground an answer, say so. Never invent numbers.\n\n"
        f"ITEM: {item.title}\nWHAT: {item.what}\nWHY: {item.why}\n"
        f"CONNECTS: {item.connects}\nACTION: {item.action}\n"
        f"KRAs: {item.kra_ids_json}\n\nEVIDENCE:\n" + "\n".join(evidence)
    )

    history = get_chat_history(session, item_id)
    messages = []
    for m in history:
        if m.role in ("user", "assistant"):
            messages.append({"role": m.role, "content": m.content})

    budget = LlmBudget(max_calls=get_settings().max_tool_rounds + 1)

    def handler(name, args):
        return dispatch_tool(session, name, args, allow_writes=False)

    text, _ = complete_with_tools(
        system=system,
        messages=messages,
        tools=CHAT_TOOLS,
        purpose="drilldown_chat",
        budget=budget,
        tool_handler=handler,
    )

    assistant = ChatMessage(
        digest_item_id=item_id,
        role="assistant",
        content=text or "(no response)",
    )
    session.add(assistant)
    session.flush()
    try:
        from cfo_agent.memory.recall import embed_chat_message

        embed_chat_message(session, assistant.id, assistant.content)
    except Exception:  # noqa: BLE001
        pass
    return assistant
