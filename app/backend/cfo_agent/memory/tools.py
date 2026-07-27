"""Memory tool definitions for Claude tool-use loops."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from cfo_agent.memory import archival as arch
from cfo_agent.memory import blocks as blk
from cfo_agent.memory import recall as rec

WRITE_TOOLS = [
    {
        "name": "memory_replace",
        "description": "Replace exact old string with new string in an editable core memory block (cfo_observations or quarter_narrative).",
        "input_schema": {
            "type": "object",
            "properties": {
                "block": {"type": "string", "enum": ["cfo_observations", "quarter_narrative"]},
                "old": {"type": "string"},
                "new": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["block", "old", "new"],
        },
    },
    {
        "name": "memory_insert",
        "description": "Append content to an editable core memory block.",
        "input_schema": {
            "type": "object",
            "properties": {
                "block": {"type": "string", "enum": ["cfo_observations", "quarter_narrative"]},
                "content": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["block", "content"],
        },
    },
    {
        "name": "archival_insert",
        "description": "Store a notable fact in long-term archival memory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "metadata": {"type": "object"},
                "week": {"type": "string"},
            },
            "required": ["text"],
        },
    },
]

SEARCH_TOOLS = [
    {
        "name": "archival_search",
        "description": "Semantic search over archival memory of past insights, alerts, and consolidations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "recall_search",
        "description": "Search past drill-down chat messages.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
        },
    },
]

CONSOLIDATION_TOOLS = WRITE_TOOLS + SEARCH_TOOLS
CHAT_TOOLS = SEARCH_TOOLS  # chat never writes memory


def dispatch_tool(
    session: Session,
    name: str,
    args: dict[str, Any],
    *,
    allow_writes: bool,
    as_of_week: str | None = None,
) -> str:
    try:
        if name in {"memory_replace", "memory_insert", "archival_insert"} and not allow_writes:
            return json.dumps({"error": "Write tools not available in this context"})

        if name == "memory_replace":
            return blk.memory_replace(
                session,
                args["block"],
                args["old"],
                args["new"],
                reason=args.get("reason", "memory_replace"),
            )
        if name == "memory_insert":
            return blk.memory_insert(
                session,
                args["block"],
                args["content"],
                reason=args.get("reason", "memory_insert"),
            )
        if name == "archival_insert":
            row = arch.archival_insert(
                session,
                args["text"],
                args.get("metadata") or {},
                week=args.get("week"),
            )
            return json.dumps({"id": row.id, "ok": True})
        if name == "archival_search":
            hits = arch.archival_search(
                session,
                args["query"],
                k=int(args.get("k") or 5),
                as_of_week=as_of_week,
            )
            return json.dumps(hits, default=str)
        if name == "recall_search":
            hits = rec.recall_search(session, args["query"])
            return json.dumps(hits, default=str)
        return json.dumps({"error": f"Unknown tool {name}"})
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})
