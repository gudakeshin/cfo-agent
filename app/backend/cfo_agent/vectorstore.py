"""Vector index for archival memory and chat recall, on either database:

- SQLite (demo / single machine): sqlite-vec ``vec0`` virtual tables
- PostgreSQL (client deployment): pgvector ``vector(384)`` columns + HNSW index

Both use L2 distance, so ranking is identical across the two.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

DIM = 384
# logical name -> id column
TABLES = {"archival_vec": "archival_id", "chat_vec": "message_id"}


def is_postgres(session_or_conn) -> bool:
    bind = session_or_conn.get_bind() if isinstance(session_or_conn, Session) else session_or_conn
    return bind.dialect.name == "postgresql"


def literal(emb: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in emb) + "]"


def create_tables(conn) -> None:
    """Idempotent DDL for both vector tables (called by init_db / migrations)."""
    if conn.dialect.name == "postgresql":
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        for table, idc in TABLES.items():
            conn.execute(text(
                f"CREATE TABLE IF NOT EXISTS {table} ({idc} INTEGER PRIMARY KEY, embedding vector({DIM}) NOT NULL)"
            ))
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS {table}_hnsw ON {table} USING hnsw (embedding vector_l2_ops)"
            ))
    else:
        for table, idc in TABLES.items():
            conn.execute(text(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0({idc} INTEGER PRIMARY KEY, embedding float[{DIM}])"
            ))


def upsert(session: Session, table: str, id_: int, emb: list[float]) -> None:
    idc = TABLES[table]
    if is_postgres(session):
        session.execute(
            text(
                f"INSERT INTO {table} ({idc}, embedding) VALUES (:id, CAST(:emb AS vector)) "
                f"ON CONFLICT ({idc}) DO UPDATE SET embedding = EXCLUDED.embedding"
            ),
            {"id": id_, "emb": literal(emb)},
        )
    else:  # vec0 has no UPSERT
        session.execute(text(f"DELETE FROM {table} WHERE {idc} = :id"), {"id": id_})
        session.execute(text(f"INSERT INTO {table}({idc}, embedding) VALUES (:id, :emb)"), {"id": id_, "emb": literal(emb)})


def delete(session: Session, table: str, id_: int) -> None:
    session.execute(text(f"DELETE FROM {table} WHERE {TABLES[table]} = :id"), {"id": id_})


def knn(session: Session, table: str, emb: list[float], k: int) -> list[tuple[int, float]]:
    idc = TABLES[table]
    if is_postgres(session):
        sql = text(
            f"SELECT {idc}, embedding <-> CAST(:emb AS vector) AS distance FROM {table} ORDER BY distance LIMIT :k"
        )
    else:
        sql = text(f"SELECT {idc}, distance FROM {table} WHERE embedding MATCH :emb ORDER BY distance LIMIT :k")
    return [(int(r[0]), float(r[1])) for r in session.execute(sql, {"emb": literal(emb), "k": k}).fetchall()]


def ids(session: Session, table: str) -> set[int]:
    return {int(r[0]) for r in session.execute(text(f"SELECT {TABLES[table]} FROM {table}")).fetchall()}
