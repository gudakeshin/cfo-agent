"""Alembic environment: uses the app's DATABASE_URL and model metadata."""

from __future__ import annotations

from alembic import context

from cfo_agent.db import get_engine
from cfo_agent.models import Base

target_metadata = Base.metadata

# Vector tables are created with raw DDL (pgvector type); keep autogenerate off them.
IGNORED_TABLES = {"archival_vec", "chat_vec"}


def include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001
    if type_ == "table" and name in IGNORED_TABLES:
        return False
    if type_ == "index" and name and name.endswith("_hnsw"):
        return False
    return True


def run_migrations_online() -> None:
    with get_engine().connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("Offline mode is not supported; run against a database.")
run_migrations_online()
