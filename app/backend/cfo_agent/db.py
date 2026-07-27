from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from cfo_agent.models import Base
from cfo_agent.settings import DATA_DIR, get_settings

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _check_loadable_extensions() -> None:
    conn = sqlite3.connect(":memory:")
    if not hasattr(conn, "enable_load_extension"):
        conn.close()
        raise RuntimeError(
            "This Python build does not support loadable SQLite extensions "
            "(common with Apple's system / python.org Framework builds). "
            "Use Homebrew Python (e.g. /opt/homebrew/bin/python3) or `uv` with "
            "a Homebrew-linked interpreter so sqlite-vec can load."
        )
    try:
        conn.enable_load_extension(True)
    except Exception as e:  # noqa: BLE001
        conn.close()
        raise RuntimeError(
            "SQLite enable_load_extension failed. Use Homebrew Python / uv. "
            f"Detail: {e}"
        ) from e
    conn.close()


def _sqlite_url(url: str) -> str:
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        # Relative path → absolute under DATA_DIR if needed
        path = url.removeprefix("sqlite:///")
        if path.startswith("./"):
            path = str(DATA_DIR / path[2:].removeprefix("data/"))
            if "cfo_agent.db" not in path:
                path = str(DATA_DIR / "cfo_agent.db")
            else:
                # normalize to DATA_DIR/cfo_agent.db
                path = str(DATA_DIR / "cfo_agent.db")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{path}"
    return url


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is not None:
        return _engine

    _check_loadable_extensions()
    settings = get_settings()
    url = _sqlite_url(settings.database_url)
    _engine = create_engine(
        url,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(_engine, "connect")
    def _on_connect(dbapi_conn, _connection_record):  # noqa: ANN001
        dbapi_conn.execute("PRAGMA journal_mode=WAL")
        dbapi_conn.execute("PRAGMA busy_timeout=30000")
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
        # Load sqlite-vec
        try:
            import sqlite_vec

            dbapi_conn.enable_load_extension(True)
            sqlite_vec.load(dbapi_conn)
            dbapi_conn.enable_load_extension(False)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Failed to load sqlite-vec extension: {e}") from e

    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    return _engine


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    """Add a column if missing (SQLite ALTER TABLE ADD COLUMN)."""
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    existing = {r[1] for r in rows}
    if column not in existing:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))


def init_db() -> None:
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    # vec0 virtual tables for archival + chat embeddings (384-dim MiniLM)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS archival_vec USING vec0(
                    archival_id INTEGER PRIMARY KEY,
                    embedding float[384]
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS chat_vec USING vec0(
                    message_id INTEGER PRIMARY KEY,
                    embedding float[384]
                )
                """
            )
        )
        # Lightweight migrations for existing DBs (create_all won't add columns)
        _ensure_column(conn, "archival_memory", "content_hash", "content_hash VARCHAR(64)")
        _ensure_column(conn, "archival_memory", "scope", "scope VARCHAR(64) DEFAULT 'global'")
        _ensure_column(conn, "chat_messages", "scope", "scope VARCHAR(64) DEFAULT 'global'")


def get_session() -> Session:
    if _SessionLocal is None:
        get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


def session_scope():
    from contextlib import contextmanager

    @contextmanager
    def _cm():
        session = get_session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return _cm()
