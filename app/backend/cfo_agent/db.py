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


def is_postgres_url(url: str) -> bool:
    return url.startswith(("postgresql", "postgres://"))


_tenant_engines: dict[str, tuple[Engine, sessionmaker]] = {}


def get_engine() -> Engine:
    global _engine, _SessionLocal
    from cfo_agent.tenancy import current_tenant

    tenant = current_tenant()
    if tenant is not None:
        if tenant.id not in _tenant_engines:
            eng = _make_engine(tenant.database_url)
            _tenant_engines[tenant.id] = (eng, sessionmaker(bind=eng, autoflush=False, autocommit=False))
        return _tenant_engines[tenant.id][0]
    if _engine is not None:
        return _engine
    _engine = _make_engine(get_settings().database_url)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    return _engine


def _make_engine(database_url: str) -> Engine:
    if is_postgres_url(database_url):
        url = database_url.replace("postgres://", "postgresql+psycopg://", 1)
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10, pool_recycle=1800)
    _check_loadable_extensions()
    url = _sqlite_url(database_url)
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
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

    return engine


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    """Add a column if missing (lightweight migration for existing SQLite demo DBs)."""
    from sqlalchemy import inspect

    existing = {c["name"] for c in inspect(conn).get_columns(table)}
    if column not in existing:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))


def _rebuild_if_constraint_changed(conn, table: str, wanted: str) -> None:
    """SQLite cannot alter constraints: copy the table into the current schema."""
    sql = conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:t"), {"t": table}).scalar()
    if not sql or wanted in sql:
        return
    cols_old = [r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))]
    for (name,) in conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=:t AND sql IS NOT NULL"
    ), {"t": table}).fetchall():
        conn.execute(text(f'DROP INDEX "{name}"'))
    conn.execute(text(f"ALTER TABLE {table} RENAME TO {table}__old"))
    tbl = Base.metadata.tables[table]
    tbl.create(conn)
    keep = [c for c in cols_old if c in tbl.c]
    collist = ", ".join(keep)
    conn.execute(text(f"INSERT INTO {table} ({collist}) SELECT {collist} FROM {table}__old"))
    conn.execute(text(f"DROP TABLE {table}__old"))


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def run_migrations() -> None:
    """Bring a PostgreSQL schema to the latest Alembic revision."""
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    # Several replicas may start at once: serialise migrations with an advisory lock.
    with get_engine().connect() as lock_conn:
        lock_conn.execute(text("SELECT pg_advisory_lock(727274001)"))
        try:
            command.upgrade(cfg, "head")
        finally:
            lock_conn.execute(text("SELECT pg_advisory_unlock(727274001)"))
            lock_conn.commit()


def init_db() -> None:
    from cfo_agent.tenancy import for_each_tenant

    for_each_tenant(_init_one)


def _init_one() -> None:
    engine = get_engine()
    if engine.dialect.name == "postgresql":
        # Client deployments: versioned, reviewable schema changes.
        run_migrations()
        return
    # SQLite demo: create everything and patch columns added since older demo DBs.
    from cfo_agent import vectorstore

    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        vectorstore.create_tables(conn)
        _ensure_column(conn, "archival_memory", "content_hash", "content_hash VARCHAR(64)")
        _ensure_column(conn, "archival_memory", "scope", "scope VARCHAR(64) DEFAULT 'global'")
        _ensure_column(conn, "chat_messages", "scope", "scope VARCHAR(64) DEFAULT 'global'")
        _ensure_column(conn, "digest_items", "evidence_snapshot_json", "evidence_snapshot_json JSON")
        _ensure_column(conn, "digest_items", "verification_json", "verification_json JSON")
        _ensure_column(conn, "chat_messages", "citations_json", "citations_json JSON")
        _ensure_column(conn, "chat_messages", "verification_json", "verification_json JSON")
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_pipeline_run_active_week ON pipeline_runs (week) "
            "WHERE status IN ('pending', 'running')"
        ))
        # Multi-entity / multi-currency columns (added after the first demo DBs)
        for table in ("erp_customers", "erp_sales_invoices", "erp_purchase_invoices", "erp_expenses",
                      "erp_revenue_daily", "erp_gst_events", "erp_covenants"):
            _ensure_column(conn, table, "entity", "entity VARCHAR(32) NOT NULL DEFAULT 'MAIN'")
        for table in ("erp_sales_invoices", "erp_receipts", "erp_purchase_invoices", "erp_payments", "erp_expenses"):
            _ensure_column(conn, table, "currency", "currency VARCHAR(3) NOT NULL DEFAULT 'INR'")
            _ensure_column(conn, table, "fx_rate", "fx_rate FLOAT NOT NULL DEFAULT 1")
        for table in ("erp_customers", "erp_purchase_invoices"):
            _ensure_column(conn, table, "is_intercompany", "is_intercompany BOOLEAN NOT NULL DEFAULT 0")
        _ensure_column(conn, "signals", "context_json", "context_json JSON")
        _ensure_column(conn, "connector_configs", "watermark", "watermark VARCHAR(64)")
        # One cash row per account per day, revenue per entity: old demo DBs had a
        # unique date (cash) / date+segment (revenue); rebuild those two tables.
        _rebuild_if_constraint_changed(conn, "erp_cash_daily", "uq_cash_day_account")
        _rebuild_if_constraint_changed(conn, "erp_revenue_daily", "uq_rev_day_seg_entity")


def get_session() -> Session:
    from cfo_agent.tenancy import current_tenant

    tenant = current_tenant()
    if tenant is not None:
        get_engine()
        return _tenant_engines[tenant.id][1]()
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
