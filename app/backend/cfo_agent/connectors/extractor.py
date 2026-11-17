"""Read-side connector interface: anything that can pull canonical rows out of a
client system since a high-water mark.

    class SourceExtractor(Protocol):
        def extract(self, since: str | None) -> ExtractBatch
        def test(self) -> dict

An extractor returns rows keyed by the file-drop table names (`sales_invoices`,
`receipts`, …) with the same column names as the CSV templates, so every source goes
through one validator (`csv_drop.validate_records`) and one loader. Each table loads
all-or-nothing; the watermark only advances when nothing was rejected, so a bad row
is retried on the next sync instead of being skipped for good.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from sqlalchemy.orm import Session

from cfo_agent.connectors.csv_drop import ORDER, SPEC_BY_NAME, _upsert, note_pending, validate_records
from cfo_agent.models import ImportedFile


@dataclass
class ExtractBatch:
    tables: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    watermark: str | None = None
    notes: list[str] = field(default_factory=list)


@runtime_checkable
class SourceExtractor(Protocol):
    def extract(self, since: str | None) -> ExtractBatch: ...

    def test(self) -> dict: ...


def load_batch(
    session: Session,
    batch: ExtractBatch,
    *,
    connector_id: str,
    run_id: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Validate and upsert an extracted batch, masters first. Caller commits."""
    results = []
    totals: dict[str, dict[str, int]] = {}
    pending: dict = {}
    unknown = [t for t in batch.tables if t not in SPEC_BY_NAME]
    for table in sorted((t for t in batch.tables if t in SPEC_BY_NAME), key=lambda t: ORDER[t]):
        spec = SPEC_BY_NAME[table]
        records = batch.tables[table]
        if not records:
            continue
        headers = sorted({k for r in records for k in r})
        rows, errs = validate_records(session, spec, headers, enumerate(records, start=1), pending)
        status = "rejected" if errs else ("would_load" if dry_run else "loaded")
        ins = upd = 0
        if not errs:
            note_pending(pending, spec, rows)
            if not dry_run:
                ins, upd = _upsert(session, spec, rows)
                t = totals.setdefault(spec.model.__tablename__, {"created": 0, "updated": 0})
                t["created"] += ins
                t["updated"] += upd
        if not dry_run:
            digest = hashlib.sha256(json.dumps(records, sort_keys=True, default=str).encode()).hexdigest()
            session.add(ImportedFile(
                connector_id=connector_id, run_id=run_id, filename=f"{connector_id}:{table}", table=table,
                sha256=digest, status=status, rows_inserted=ins, rows_updated=upd, errors_json=errs[:200],
            ))
        results.append({
            "file": f"{connector_id}:{table}", "table": table, "status": status,
            "rows": len(records), "inserted": ins, "updated": upd,
            "errors": errs[:20], "error_count": len(errs),
        })
    return {
        **totals,
        "files": results,
        "unrecognised_files": unknown,
        "loaded": sum(r["status"] == "loaded" for r in results),
        "rejected": sum(r["status"] == "rejected" for r in results),
        "duplicates": 0,
        "notes": batch.notes,
    }
