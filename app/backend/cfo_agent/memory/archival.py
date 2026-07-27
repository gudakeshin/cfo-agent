"""sqlite-vec archival memory store + embedding search."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import numpy as np
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.orm import Session

from cfo_agent.memory.embeddings import embed_query, embed_texts
from cfo_agent.models import ArchivalMemory

logger = logging.getLogger(__name__)


def _content_hash(text_content: str) -> str:
    normalized = " ".join(text_content.split()).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _emb_to_bytes(emb: list[float]) -> bytes:
    return np.asarray(emb, dtype=np.float32).tobytes()


def _bytes_to_emb(raw: bytes) -> list[float]:
    return np.frombuffer(raw, dtype=np.float32).tolist()


def _vec_literal(emb: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in emb) + "]"


def archival_insert(
    session: Session,
    text_content: str,
    metadata: dict[str, Any] | None = None,
    *,
    week: str | None = None,
    scope: str = "global",
) -> ArchivalMemory:
    metadata = dict(metadata or {})
    if week:
        metadata.setdefault("week", week)
    ch = _content_hash(text_content)
    week_key = week or metadata.get("week")

    # Dedup: same content_hash + week → merge metadata, return existing
    existing_q = session.query(ArchivalMemory).filter_by(content_hash=ch)
    if week_key:
        existing_q = existing_q.filter_by(week=week_key)
    else:
        existing_q = existing_q.filter(ArchivalMemory.week.is_(None))
    existing = existing_q.first()
    if existing:
        meta = dict(existing.metadata_json or {})
        meta.update(metadata)
        existing.metadata_json = meta
        session.flush()
        return existing

    emb = embed_texts([text_content])[0]
    row = ArchivalMemory(
        text=text_content,
        metadata_json=metadata,
        embedding=_emb_to_bytes(emb),
        content_hash=ch,
        week=week_key,
        scope=scope or "global",
    )
    session.add(row)
    session.flush()

    session.execute(
        text("INSERT INTO archival_vec(archival_id, embedding) VALUES (:id, :emb)"),
        {"id": row.id, "emb": _vec_literal(emb)},
    )
    session.flush()
    return row


def delete_archival(session: Session, archival_id: int) -> bool:
    row = session.get(ArchivalMemory, archival_id)
    if not row:
        return False
    session.execute(
        text("DELETE FROM archival_vec WHERE archival_id = :id"),
        {"id": archival_id},
    )
    session.delete(row)
    session.flush()
    return True


def archival_search(
    session: Session,
    query: str,
    k: int = 5,
    *,
    as_of_week: str | None = None,
    scope: str | None = "global",
) -> list[dict[str, Any]]:
    """Semantic search; optionally filter to entries with week <= as_of_week and scope."""
    qemb = embed_query(query)
    vec_literal = _vec_literal(qemb)

    sql = text(
        """
        SELECT archival_id, distance
        FROM archival_vec
        WHERE embedding MATCH :emb
        ORDER BY distance
        LIMIT :k
        """
    )
    try:
        rows = session.execute(sql, {"emb": vec_literal, "k": k * 5}).fetchall()
    except (OperationalError, DatabaseError) as e:
        logger.warning("archival_vec KNN failed, falling back to brute-force: %s", e)
        return _brute_force_search(session, qemb, k, as_of_week=as_of_week, scope=scope)

    results: list[dict[str, Any]] = []
    for archival_id, distance in rows:
        row = session.get(ArchivalMemory, archival_id)
        if not row:
            continue
        if scope is not None and (row.scope or "global") != scope:
            continue
        if as_of_week and row.week and row.week > as_of_week:
            continue
        results.append(
            {
                "id": row.id,
                "text": row.text,
                "metadata": row.metadata_json,
                "week": row.week,
                "scope": row.scope,
                "distance": float(distance) if distance is not None else None,
            }
        )
        if len(results) >= k:
            break
    return results


def _brute_force_search(
    session: Session,
    qemb: list[float],
    k: int,
    *,
    as_of_week: str | None,
    scope: str | None = "global",
) -> list[dict[str, Any]]:
    q = np.asarray(qemb, dtype=np.float32)
    scored: list[tuple[float, ArchivalMemory]] = []
    for row in session.query(ArchivalMemory).all():
        if scope is not None and (row.scope or "global") != scope:
            continue
        if as_of_week and row.week and row.week > as_of_week:
            continue
        if row.embedding:
            v = np.frombuffer(row.embedding, dtype=np.float32)
        else:
            v = np.asarray(embed_texts([row.text])[0], dtype=np.float32)
        sim = float(np.dot(q, v) / (np.linalg.norm(q) * np.linalg.norm(v) + 1e-9))
        scored.append((sim, row))
    scored.sort(key=lambda x: -x[0])
    return [
        {
            "id": r.id,
            "text": r.text,
            "metadata": r.metadata_json,
            "week": r.week,
            "scope": r.scope,
            "distance": 1.0 - sim,
        }
        for sim, r in scored[:k]
    ]


def update_archival_metadata(session: Session, archival_id: int, patch: dict) -> None:
    row = session.get(ArchivalMemory, archival_id)
    if not row:
        return
    meta = dict(row.metadata_json or {})
    meta.update(patch)
    row.metadata_json = meta
    session.flush()


def check_archival_consistency(session: Session) -> dict[str, int]:
    """Repair orphan vec rows, missing vec inserts, hash/embedding backfill; chat_vec backfill."""
    stats = {
        "orphan_vec_removed": 0,
        "missing_vec_inserted": 0,
        "hash_backfilled": 0,
        "embedding_backfilled": 0,
        "chat_vec_backfilled": 0,
    }

    # Orphan vec rows
    vec_ids = {
        r[0]
        for r in session.execute(text("SELECT archival_id FROM archival_vec")).fetchall()
    }
    orm_ids = {r.id for r in session.query(ArchivalMemory.id).all()}
    for vid in vec_ids - orm_ids:
        session.execute(
            text("DELETE FROM archival_vec WHERE archival_id = :id"), {"id": vid}
        )
        stats["orphan_vec_removed"] += 1

    # Missing vec / hash / embedding backfill
    for row in session.query(ArchivalMemory).all():
        if not row.content_hash:
            row.content_hash = _content_hash(row.text)
            stats["hash_backfilled"] += 1
        if not row.scope:
            row.scope = "global"
        if not row.embedding:
            emb = embed_texts([row.text])[0]
            row.embedding = _emb_to_bytes(emb)
            stats["embedding_backfilled"] += 1
        else:
            emb = _bytes_to_emb(row.embedding)
        if row.id not in vec_ids:
            session.execute(
                text(
                    "INSERT INTO archival_vec(archival_id, embedding) VALUES (:id, :emb)"
                ),
                {"id": row.id, "emb": _vec_literal(emb)},
            )
            stats["missing_vec_inserted"] += 1

    # Chat vec backfill (capped)
    from cfo_agent.memory.recall import backfill_chat_embeddings

    stats["chat_vec_backfilled"] = backfill_chat_embeddings(session, cap=500)
    session.flush()
    return stats


def compact_archival(session: Session, *, per_week_cap: int = 50) -> int:
    """Evict low-importance archival rows per week. Scoring: +2 red-line/acted_on, +1 useful, −2 already_knew."""
    from collections import defaultdict

    by_week: dict[str | None, list[ArchivalMemory]] = defaultdict(list)
    for row in session.query(ArchivalMemory).all():
        by_week[row.week].append(row)

    deleted = 0
    for _week, rows in by_week.items():
        if len(rows) <= per_week_cap:
            continue

        def score(r: ArchivalMemory) -> int:
            meta = r.metadata_json or {}
            s = 0
            if meta.get("is_red_line") or meta.get("verdict") == "acted_on":
                s += 2
            if meta.get("verdict") == "useful":
                s += 1
            if meta.get("already_knew"):
                s -= 2
            return s

        ranked = sorted(rows, key=lambda r: (score(r), r.created_at or r.id), reverse=True)
        for victim in ranked[per_week_cap:]:
            if delete_archival(session, victim.id):
                deleted += 1
    return deleted
