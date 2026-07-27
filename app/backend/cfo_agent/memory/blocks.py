"""Core memory blocks — load/render, char limits, revisions."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from cfo_agent.models import MemoryBlock, MemoryBlockRevision

HUMAN_ONLY = frozenset({"kra_framework", "priority_weighting"})
AGENT_EDITABLE = frozenset({"cfo_observations", "quarter_narrative"})
ALL_BLOCKS = HUMAN_ONLY | AGENT_EDITABLE


class MemoryError(Exception):
    pass


def get_block(session: Session, name: str) -> MemoryBlock:
    row = session.get(MemoryBlock, name)
    if not row:
        raise MemoryError(f"Unknown memory block: {name}")
    return row


def list_blocks(session: Session) -> list[MemoryBlock]:
    return session.query(MemoryBlock).order_by(MemoryBlock.name).all()


def render_blocks_for_prompt(session: Session) -> str:
    parts = []
    for name in ("kra_framework", "priority_weighting", "cfo_observations", "quarter_narrative"):
        row = session.get(MemoryBlock, name)
        if row:
            parts.append(f"<memory_block name=\"{name}\" limit=\"{row.char_limit}\">\n{row.content}\n</memory_block>")
    return "\n\n".join(parts)


def _next_version(session: Session, name: str) -> int:
    latest = (
        session.query(MemoryBlockRevision)
        .filter_by(block_name=name)
        .order_by(MemoryBlockRevision.version.desc())
        .first()
    )
    pending = [
        o
        for o in session.new
        if isinstance(o, MemoryBlockRevision) and o.block_name == name
    ]
    max_pending = max((o.version for o in pending), default=0)
    base = latest.version if latest else 0
    return max(base, max_pending) + 1


def set_block_content(
    session: Session,
    name: str,
    content: str,
    *,
    edited_by: str,
    reason: str = "",
    enforce_agent_rules: bool = True,
) -> MemoryBlock:
    if name not in ALL_BLOCKS:
        raise MemoryError(f"Unknown block {name}")
    if enforce_agent_rules and edited_by == "agent" and name in HUMAN_ONLY:
        raise MemoryError(f"Block {name} is human-only; agent edits rejected")

    row = get_block(session, name)
    if len(content) > row.char_limit:
        raise MemoryError(
            f"Content length {len(content)} exceeds char_limit {row.char_limit} "
            f"for block {name}. Summarize first."
        )

    row.content = content
    row.updated_at = datetime.utcnow()
    session.add(
        MemoryBlockRevision(
            block_name=name,
            version=_next_version(session, name),
            content=content,
            edited_by=edited_by,
            reason=reason,
        )
    )
    session.flush()
    return row


def memory_replace(
    session: Session,
    block: str,
    old: str,
    new: str,
    *,
    edited_by: str = "agent",
    reason: str = "memory_replace",
) -> str:
    row = get_block(session, block)
    if block in HUMAN_ONLY and edited_by == "agent":
        raise MemoryError(f"Block {block} is human-only")
    if old not in row.content:
        raise MemoryError(f"Old string not found in block {block}")
    content = row.content.replace(old, new, 1)
    set_block_content(session, block, content, edited_by=edited_by, reason=reason)
    return f"Replaced in {block}. New length={len(content)}/{row.char_limit}"


def memory_insert(
    session: Session,
    block: str,
    content: str,
    *,
    edited_by: str = "agent",
    reason: str = "memory_insert",
) -> str:
    row = get_block(session, block)
    if block in HUMAN_ONLY and edited_by == "agent":
        raise MemoryError(f"Block {block} is human-only")
    sep = "\n" if row.content and not row.content.endswith("\n") else ""
    new_content = row.content + sep + content
    set_block_content(session, block, new_content, edited_by=edited_by, reason=reason)
    return f"Inserted into {block}. New length={len(new_content)}/{row.char_limit}"


def revision_history(session: Session, name: str) -> list[MemoryBlockRevision]:
    return (
        session.query(MemoryBlockRevision)
        .filter_by(block_name=name)
        .order_by(MemoryBlockRevision.version.asc())
        .all()
    )


def ensure_seed_blocks(session: Session) -> None:
    from cfo_agent.kra import framework_to_prompt_block, get_active_framework
    from cfo_agent.weighting import get_active_weighting, weighting_to_prompt_block

    defaults = {
        "kra_framework": (framework_to_prompt_block(get_active_framework(session)), 50_000),
        "priority_weighting": (
            weighting_to_prompt_block(get_active_weighting(session).content_json),
            10_000,
        ),
        "cfo_observations": ("No observations recorded yet.", 2000),
        "quarter_narrative": ("", 2000),
    }
    for name, (content, limit) in defaults.items():
        if session.get(MemoryBlock, name) is None:
            session.add(MemoryBlock(name=name, content=content, char_limit=limit))
            session.flush()
            session.add(
                MemoryBlockRevision(
                    block_name=name,
                    version=1,
                    content=content,
                    edited_by="seed",
                    reason="ensure_seed_blocks",
                )
            )
