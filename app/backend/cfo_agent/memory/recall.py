"""Recall memory — semantic search over chat_messages via chat_vec, with substring fallback."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.orm import Session

from cfo_agent.memory.embeddings import embed_query, embed_texts
from cfo_agent.models import ChatMessage, DigestItem

logger = logging.getLogger(__name__)


def _vec_literal(emb: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in emb) + "]"


def embed_chat_message(session: Session, message_id: int, content: str) -> None:
    """Best-effort: embed a chat message into chat_vec. Never raises to callers of chat."""
    try:
        emb = embed_texts([content])[0]
        # Replace if exists
        session.execute(
            text("DELETE FROM chat_vec WHERE message_id = :id"),
            {"id": message_id},
        )
        session.execute(
            text("INSERT INTO chat_vec(message_id, embedding) VALUES (:id, :emb)"),
            {"id": message_id, "emb": _vec_literal(emb)},
        )
        session.flush()
    except Exception as e:  # noqa: BLE001 — chat path must never fail on embed
        logger.warning("chat_vec embed failed for message %s: %s", message_id, e)


def recall_search(
    session: Session,
    query: str,
    *,
    limit: int = 10,
    scope: str | None = "global",
) -> list[dict[str, Any]]:
    q = query.strip()
    if not q:
        return []

    # Try vector KNN first
    try:
        qemb = embed_query(q)
        rows = session.execute(
            text(
                """
                SELECT message_id, distance
                FROM chat_vec
                WHERE embedding MATCH :emb
                ORDER BY distance
                LIMIT :k
                """
            ),
            {"emb": _vec_literal(qemb), "k": limit * 3},
        ).fetchall()
        hits: list[dict[str, Any]] = []
        for message_id, distance in rows:
            m = session.get(ChatMessage, message_id)
            if not m:
                continue
            if scope is not None and (m.scope or "global") != scope:
                continue
            item = session.get(DigestItem, m.digest_item_id)
            hits.append(
                {
                    "message_id": m.id,
                    "digest_item_id": m.digest_item_id,
                    "role": m.role,
                    "content": m.content[:500],
                    "item_title": item.title if item else None,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                    "distance": float(distance) if distance is not None else None,
                    "scope": m.scope,
                }
            )
            if len(hits) >= limit:
                break
        if hits:
            return hits
    except (OperationalError, DatabaseError) as e:
        logger.warning("chat_vec KNN failed, falling back to substring: %s", e)
    except Exception as e:  # noqa: BLE001
        logger.warning("semantic recall failed, falling back to substring: %s", e)

    return _substring_fallback(session, q, limit=limit, scope=scope)


def _substring_fallback(
    session: Session,
    query: str,
    *,
    limit: int,
    scope: str | None,
) -> list[dict[str, Any]]:
    q = query.strip().lower()
    tokens = [t for t in q.split() if len(t) > 2][:8] or [q]
    rows_q = session.query(ChatMessage)
    if scope is not None:
        rows_q = rows_q.filter(ChatMessage.scope == scope)
    rows = rows_q.order_by(ChatMessage.created_at.desc()).limit(500).all()
    hits: list[dict[str, Any]] = []
    for m in rows:
        text_l = m.content.lower()
        if any(t in text_l for t in tokens):
            item = session.get(DigestItem, m.digest_item_id)
            hits.append(
                {
                    "message_id": m.id,
                    "digest_item_id": m.digest_item_id,
                    "role": m.role,
                    "content": m.content[:500],
                    "item_title": item.title if item else None,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                    "scope": m.scope,
                }
            )
            if len(hits) >= limit:
                break
    return hits


def backfill_chat_embeddings(session: Session, *, cap: int = 500) -> int:
    """Embed up to ``cap`` chat messages missing from chat_vec."""
    existing = {
        r[0]
        for r in session.execute(text("SELECT message_id FROM chat_vec")).fetchall()
    }
    candidates = (
        session.query(ChatMessage)
        .filter(ChatMessage.role.in_(("user", "assistant")))
        .order_by(ChatMessage.created_at.desc())
        .limit(cap * 2)
        .all()
    )
    count = 0
    for m in candidates:
        if m.id in existing:
            continue
        if count >= cap:
            break
        try:
            emb = embed_texts([m.content])[0]
            session.execute(
                text("INSERT INTO chat_vec(message_id, embedding) VALUES (:id, :emb)"),
                {"id": m.id, "emb": _vec_literal(emb)},
            )
            count += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("chat backfill failed for %s: %s", m.id, e)
    session.flush()
    return count
