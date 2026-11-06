from __future__ import annotations

import os
from pathlib import Path

import pytest

# Must set before importing cfo_agent.settings
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-key-deterministic-suite-only")
os.environ.setdefault(
    "DATABASE_URL",
    f"sqlite:///{Path(__file__).resolve().parent / 'test_cfo_agent.db'}",
)

from cfo_agent.db import get_engine, get_session, init_db  # noqa: E402
from cfo_agent.kra import upsert_kra_version  # noqa: E402
from cfo_agent.models import Base  # noqa: E402
from cfo_agent.settings import DATA_DIR, get_settings  # noqa: E402
from cfo_agent.synthetic.generator import generate_erp  # noqa: E402
from cfo_agent.weighting import seed_weighting_from_yaml  # noqa: E402


@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    get_settings.cache_clear()
    pg_url = os.environ.get("TEST_DATABASE_URL")
    if pg_url:
        # Run the suite on PostgreSQL: fresh schema per test, built by the migrations.
        from sqlalchemy import create_engine, text

        eng = create_engine(pg_url)
        with eng.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        eng.dispose()
        monkeypatch.setenv("DATABASE_URL", pg_url)
    else:
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-deterministic-suite-only")
    get_settings.cache_clear()

    # Reset engine singleton
    import cfo_agent.db as dbmod

    dbmod._engine = None
    dbmod._SessionLocal = None

    init_db()
    session = get_session()
    upsert_kra_version(session)
    seed_weighting_from_yaml(session)
    session.commit()
    yield session
    session.close()
    if dbmod._engine is not None:
        dbmod._engine.dispose()
    dbmod._engine = None
    dbmod._SessionLocal = None
    get_settings.cache_clear()


@pytest.fixture()
def seeded_erp(db_session):
    meta = generate_erp(db_session, seed=42, stale_days=0)
    db_session.commit()
    return meta
