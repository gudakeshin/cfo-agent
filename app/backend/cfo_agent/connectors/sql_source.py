"""Read-only SQL connector: run the client's own SELECT statements against a
reporting replica (or the ERP database) with a read-only login.

Configuration (per connector):
  secret   SQLAlchemy URL with a *read-only* database user, e.g.
           postgresql+psycopg://cfo_reader:…@erp-replica:5432/erp
  queries  {table: SELECT …}  — one statement per target table, selecting columns
           named like the CSV templates (business_key, invoice_date, amount_inr, …).
           A `:since` bind parameter is available for incremental pulls, and a
           `_modified_at` column (if selected) advances the watermark.

Defences in depth (the read-only login is the real control):
  * only a single SELECT/WITH statement is accepted per table;
  * PostgreSQL sessions are set READ ONLY; MySQL/MariaDB transactions READ ONLY;
  * every transaction is rolled back, never committed;
  * a statement timeout and a row cap bound the load on the source.
Drivers bundled: PostgreSQL (psycopg) and SQLite. For SQL Server or Oracle add the
driver (pyodbc / oracledb) to the image.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import create_engine, text

from cfo_agent.connectors.csv_drop import SPEC_BY_NAME
from cfo_agent.connectors.extractor import ExtractBatch

MAX_ROWS_PER_TABLE = 200_000
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|upsert|drop|alter|create|truncate|grant|revoke|call|exec|execute|copy|"
    r"vacuum|attach|pragma|into)\b",
    re.IGNORECASE,
)


def validate_query(sql: str) -> str:
    s = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql or "", flags=re.S).strip().rstrip(";").strip()
    if not s:
        raise ValueError("query is empty")
    if ";" in s:
        raise ValueError("only one statement is allowed")
    if not re.match(r"^(select|with)\b", s, re.IGNORECASE):
        raise ValueError("query must be a SELECT (or WITH … SELECT)")
    m = _FORBIDDEN.search(s)
    if m:
        raise ValueError(f"'{m.group(0)}' is not allowed in a read-only query")
    return s


def _text_value(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


class SqlReadOnlyExtractor:
    def __init__(self, url: str, queries: dict[str, str], *, timeout_s: int = 120):
        if not url:
            raise ValueError("database URL (secret) is required")
        unknown = [t for t in queries if t not in SPEC_BY_NAME]
        if unknown:
            raise ValueError(f"unknown target table(s): {', '.join(unknown)}")
        if not queries:
            raise ValueError("configure at least one query")
        self.queries = {t: validate_query(q) for t, q in queries.items()}
        self.url = url
        self.timeout_s = timeout_s

    def _engine(self):
        eng = create_engine(self.url, pool_pre_ping=True)
        return eng

    def _connect(self, eng):
        conn = eng.connect()
        dialect = eng.dialect.name
        if dialect == "postgresql":
            conn.exec_driver_sql("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            conn.exec_driver_sql(f"SET statement_timeout = {int(self.timeout_s) * 1000}")
        elif dialect in ("mysql", "mariadb"):
            conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
        elif dialect == "sqlite":
            conn.exec_driver_sql("PRAGMA query_only = ON")
        return conn

    def test(self) -> dict:
        eng = self._engine()
        try:
            with self._connect(eng) as conn:
                counts = {}
                for table, q in self.queries.items():
                    params = {"since": "1900-01-01"} if ":since" in q else {}
                    n = conn.execute(text(f"SELECT COUNT(*) FROM ({q}) AS cfo_q"), params).scalar()
                    counts[table] = int(n or 0)
                conn.rollback()
            return {"ok": True, "message": "Connected (read-only). Rows available: "
                    + ", ".join(f"{t} {n}" for t, n in counts.items())}
        finally:
            eng.dispose()

    def extract(self, since: str | None) -> ExtractBatch:
        batch = ExtractBatch()
        watermark = since
        eng = self._engine()
        try:
            with self._connect(eng) as conn:
                for table, q in self.queries.items():
                    params = {"since": since or "1900-01-01"} if ":since" in q else {}
                    res = conn.execute(text(q), params)
                    cols = list(res.keys())
                    rows = []
                    for i, rec in enumerate(res):
                        if i >= MAX_ROWS_PER_TABLE:
                            batch.notes.append(f"{table}: stopped at {MAX_ROWS_PER_TABLE} rows; narrow the query with :since")
                            break
                        row = {c: _text_value(v) for c, v in zip(cols, rec)}
                        mod = row.pop("_modified_at", "")
                        if mod and (watermark is None or mod > watermark):
                            watermark = mod
                        rows.append(row)
                    batch.tables[table] = rows
                conn.rollback()  # never commit anything on the source
        finally:
            eng.dispose()
        batch.watermark = watermark
        return batch
