"""File-drop connector: load ERP exports (SAP, Oracle, Tally, anything that can
write CSV) from an inbox folder.

Folder layout (under CSV_INBOX_DIR/<folder>/):
    sales_invoices.csv, sales_invoices_2026-07-12.csv, …   ← drop files here
    processed/<UTC timestamp>/…                           ← loaded files are moved here
    rejected/<UTC timestamp>/<file> + <file>.errors.csv   ← files with any bad row

A file's table is the longest table name its file name starts with (so
``sales_invoices_2026-07-12.csv`` or ``sales_invoices_north.csv`` → ``sales_invoices``). Each file is validated in
full and loaded all-or-nothing; rows are upserted by ``business_key`` so a re-sent
correction (even for an old date) updates the existing record. Identical files
(same SHA-256) are never loaded twice. Amounts are rupees, converted exactly to
integer paise. Dates may be ISO (2026-07-12) or Indian (12-07-2026, 12/07/2026).
The source system is only ever read.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session

from cfo_agent.models import (
    ErpBankAccount,
    ErpCashDaily,
    ErpCreditNote,
    ErpEntity,
    ErpFxRate,
    ErpGlEntry,
    ErpPlanMetric,
    ErpCovenant,
    ErpCustomer,
    ErpExpense,
    ErpGstEvent,
    ErpPayment,
    ErpPurchaseInvoice,
    ErpReceipt,
    ErpRevenueDaily,
    ErpSalesInvoice,
    ImportedFile,
)

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_ERRORS_PER_FILE = 200


# ── parsing helpers ──────────────────────────────────────────────────────────

class RowError(ValueError):
    pass


def parse_date(v: str) -> date:
    v = (v or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    raise RowError(f"'{v}' is not a date (use YYYY-MM-DD or DD-MM-YYYY)")


def parse_inr_to_paise(v: str, *, allow_negative: bool = False) -> int:
    raw = (v or "").strip().replace(",", "").replace("₹", "").replace("INR", "").strip()
    if raw.startswith("(") and raw.endswith(")"):  # accounting negative
        raw = "-" + raw[1:-1]
    try:
        d = Decimal(raw)
    except InvalidOperation:
        raise RowError(f"'{v}' is not an amount") from None
    if d.as_tuple().exponent < -2:  # type: ignore[operator]
        raise RowError(f"'{v}' has more than 2 decimal places")
    if d < 0 and not allow_negative:
        raise RowError(f"'{v}' is negative")
    return int(d * 100)


def parse_signed_paise(v: str) -> int:
    return parse_inr_to_paise(v, allow_negative=True)


def parse_ccy(v: str) -> str:
    s = (v or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", s):
        raise RowError(f"'{v}' is not a 3-letter currency code")
    return s


def parse_rate(v: str) -> float:
    try:
        r = float(str(v).strip().replace(",", ""))
    except ValueError:
        raise RowError(f"'{v}' is not a rate") from None
    if r <= 0:
        raise RowError("rate must be positive")
    return r


def parse_float(v: str) -> float:
    try:
        return float(str(v).strip().replace(",", ""))
    except ValueError:
        raise RowError(f"'{v}' is not a number") from None


ACCOUNT_TYPES = {"revenue", "cogs", "opex", "other_income", "finance_cost", "tax", "asset", "liability", "equity"}


def parse_account_type(v: str) -> str:
    s = (v or "").strip().lower()
    if s not in ACCOUNT_TYPES:
        raise RowError(f"'{v}' is not one of {', '.join(sorted(ACCOUNT_TYPES))}")
    return s


def parse_int(v: str) -> int:
    try:
        return int(str(v).strip())
    except ValueError:
        raise RowError(f"'{v}' is not a whole number") from None


def parse_bool(v: str) -> bool:
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y"):
        return True
    if s in ("0", "false", "no", "n", ""):
        return False
    raise RowError(f"'{v}' is not yes/no")


def text(maxlen: int) -> Callable[[str], str]:
    def f(v: str) -> str:
        s = (v or "").strip()
        if len(s) > maxlen:
            raise RowError(f"longer than {maxlen} characters")
        return s

    return f


# ── table specs ──────────────────────────────────────────────────────────────

@dataclass
class Col:
    name: str  # CSV header
    attr: str  # model attribute (or a *_key handled by `refs`)
    parse: Callable[[str], Any]
    required: bool = True
    default: Any = None


@dataclass
class TableSpec:
    name: str
    model: type
    cols: list[Col]
    # CSV column → (model attr, referenced model): business key resolved to an id
    refs: dict[str, tuple[str, type]] = field(default_factory=dict)
    key_from: Callable[[dict], str] | None = None  # derive business_key when absent
    check: Callable[[dict], None] | None = None
    # Multi-currency rows: (date attr, [amount attrs]). Amounts in a non-INR
    # `currency` are converted to INR with `fx_rate` (or the FX-rate table).
    fx: tuple[str, list[str]] | None = None
    fx_stored: bool = True  # model has currency/fx_rate columns
    fixed: dict[str, Any] = field(default_factory=dict)  # constant attrs (e.g. plan kind)


def _check_invoice(r: dict) -> None:
    if r["due_date"] < r["invoice_date"]:
        raise RowError("due_date is before invoice_date")


def _check_week(w: str) -> None:
    if not re.fullmatch(r"\d{4}-W\d{2}", w):
        raise RowError(f"week '{w}' must look like 2026-W28")


SPECS: list[TableSpec] = [  # load order: masters before transactions
    TableSpec("entities", ErpEntity, [
        Col("business_key", "business_key", text(32)),
        Col("name", "name", text(256)),
        Col("functional_currency", "functional_currency", parse_ccy, False, "INR"),
        Col("consolidate", "consolidate", parse_bool, False, True),
    ]),
    TableSpec("bank_accounts", ErpBankAccount, [
        Col("business_key", "business_key", text(64)),
        Col("entity", "entity", text(32), False, "MAIN"),
        Col("bank_name", "bank_name", text(128), False, ""),
        Col("label", "label", text(128), False, ""),
        Col("currency", "currency", parse_ccy, False, "INR"),
        Col("is_restricted", "is_restricted", parse_bool, False, False),
    ]),
    TableSpec("fx_rates", ErpFxRate, [
        Col("business_key", "business_key", text(64), False),
        Col("rate_date", "rate_date", parse_date),
        Col("currency", "currency", parse_ccy),
        Col("rate_to_inr", "rate_to_inr", parse_rate),
    ], key_from=lambda r: f"FX-{r['rate_date'].isoformat()}-{r['currency']}"),
    TableSpec("customers", ErpCustomer, [
        Col("business_key", "business_key", text(64)),
        Col("name", "name", text(256)),
        Col("segment", "segment", text(64)),
        Col("payment_terms_days", "payment_terms_days", parse_int, False, 45),
        Col("is_anchor", "is_anchor", parse_bool, False, False),
        Col("entity", "entity", text(32), False, "MAIN"),
        Col("is_intercompany", "is_intercompany", parse_bool, False, False),
    ]),
    TableSpec("covenants", ErpCovenant, [
        Col("business_key", "business_key", text(64)),
        Col("name", "name", text(128)),
        Col("metric", "metric", text(64)),
        Col("threshold_inr", "threshold_paise", parse_inr_to_paise),
        Col("buffered_threshold_inr", "buffered_threshold_paise", parse_inr_to_paise),
        Col("operator", "operator", text(8), False, "gte"),
        Col("entity", "entity", text(32), False, "MAIN"),
    ]),
    TableSpec("sales_invoices", ErpSalesInvoice, [
        Col("business_key", "business_key", text(64)),
        Col("customer_key", "customer_id", text(64)),
        Col("invoice_date", "invoice_date", parse_date),
        Col("due_date", "due_date", parse_date),
        Col("amount_inr", "amount_paise", parse_inr_to_paise),
        Col("segment", "segment", text(64)),
        Col("status", "status", text(32), False, "open"),
        Col("entity", "entity", text(32), False, "MAIN"),
        Col("currency", "currency", parse_ccy, False, "INR"),
        Col("fx_rate", "fx_rate", parse_rate, False),
    ], refs={"customer_key": ("customer_id", ErpCustomer)}, check=_check_invoice,
        fx=("invoice_date", ["amount_paise"])),
    TableSpec("receipts", ErpReceipt, [
        Col("business_key", "business_key", text(64)),
        Col("invoice_key", "invoice_id", text(64)),
        Col("receipt_date", "receipt_date", parse_date),
        Col("amount_inr", "amount_paise", parse_inr_to_paise),
        Col("currency", "currency", parse_ccy, False, "INR"),
        Col("fx_rate", "fx_rate", parse_rate, False),
    ], refs={"invoice_key": ("invoice_id", ErpSalesInvoice)}, fx=("receipt_date", ["amount_paise"])),
    TableSpec("credit_notes", ErpCreditNote, [
        Col("business_key", "business_key", text(64)),
        Col("invoice_key", "invoice_id", text(64)),
        Col("note_date", "note_date", parse_date),
        Col("amount_inr", "amount_paise", parse_inr_to_paise),
        Col("reason", "reason", text(256), False, ""),
        Col("currency", "currency", parse_ccy, False, "INR"),
        Col("fx_rate", "fx_rate", parse_rate, False),
    ], refs={"invoice_key": ("invoice_id", ErpSalesInvoice)}, fx=("note_date", ["amount_paise"])),
    TableSpec("purchase_invoices", ErpPurchaseInvoice, [
        Col("business_key", "business_key", text(64)),
        Col("vendor_name", "vendor_name", text(256)),
        Col("invoice_date", "invoice_date", parse_date),
        Col("due_date", "due_date", parse_date),
        Col("amount_inr", "amount_paise", parse_inr_to_paise),
        Col("status", "status", text(32), False, "open"),
        Col("entity", "entity", text(32), False, "MAIN"),
        Col("currency", "currency", parse_ccy, False, "INR"),
        Col("fx_rate", "fx_rate", parse_rate, False),
        Col("is_intercompany", "is_intercompany", parse_bool, False, False),
    ], check=_check_invoice, fx=("invoice_date", ["amount_paise"])),
    TableSpec("payments", ErpPayment, [
        Col("business_key", "business_key", text(64)),
        Col("purchase_invoice_key", "purchase_invoice_id", text(64)),
        Col("payment_date", "payment_date", parse_date),
        Col("amount_inr", "amount_paise", parse_inr_to_paise),
        Col("currency", "currency", parse_ccy, False, "INR"),
        Col("fx_rate", "fx_rate", parse_rate, False),
    ], refs={"purchase_invoice_key": ("purchase_invoice_id", ErpPurchaseInvoice)}, fx=("payment_date", ["amount_paise"])),
    TableSpec("expenses", ErpExpense, [
        Col("business_key", "business_key", text(64)),
        Col("expense_date", "expense_date", parse_date),
        Col("cost_center", "cost_center", text(64)),
        Col("amount_inr", "amount_paise", parse_inr_to_paise),
        Col("category", "category", text(64), False, "general"),
        Col("entity", "entity", text(32), False, "MAIN"),
        Col("currency", "currency", parse_ccy, False, "INR"),
        Col("fx_rate", "fx_rate", parse_rate, False),
    ], fx=("expense_date", ["amount_paise"])),
    TableSpec("cash_daily", ErpCashDaily, [
        Col("business_key", "business_key", text(64), False),
        Col("as_of_date", "as_of_date", parse_date),
        Col("account_key", "account_key", text(64), False, "PRIMARY"),
        Col("balance_inr", "balance_paise", parse_signed_paise),
        Col("currency", "currency", parse_ccy, False),
        Col("fx_rate", "fx_rate", parse_rate, False),
    ], key_from=lambda r: (f"CASH-{r['as_of_date'].isoformat()}" if r.get("account_key", "PRIMARY") == "PRIMARY"
                           else f"CASH-{r['as_of_date'].isoformat()}-{r['account_key']}"),
        fx=("as_of_date", ["balance_paise"]), fx_stored=False),
    TableSpec("revenue_daily", ErpRevenueDaily, [
        Col("business_key", "business_key", text(64), False),
        Col("as_of_date", "as_of_date", parse_date),
        Col("segment", "segment", text(64)),
        Col("amount_inr", "amount_paise", parse_signed_paise),
        Col("entity", "entity", text(32), False, "MAIN"),
    ], key_from=lambda r: (f"REV-{r['as_of_date'].isoformat()}-{r['segment']}" if r.get("entity", "MAIN") == "MAIN"
                           else f"REV-{r['as_of_date'].isoformat()}-{r['segment']}-{r['entity']}")),
    TableSpec("gst_events", ErpGstEvent, [
        Col("business_key", "business_key", text(64)),
        Col("event_date", "event_date", parse_date),
        Col("event_type", "event_type", text(64)),
        Col("amount_inr", "amount_paise", parse_inr_to_paise, False, 0),
        Col("status", "status", text(32), False, "open"),
        Col("description", "description", text(2000), False, ""),
        Col("entity", "entity", text(32), False, "MAIN"),
    ]),
    TableSpec("gl_entries", ErpGlEntry, [
        Col("business_key", "business_key", text(64)),
        Col("posting_date", "posting_date", parse_date),
        Col("period_end", "period_end", parse_date),
        Col("account_code", "account_code", text(32)),
        Col("account_name", "account_name", text(128), False, ""),
        Col("account_type", "account_type", parse_account_type),
        Col("amount_inr", "amount_paise", parse_signed_paise),
        Col("source_ref", "source_ref", text(128), False, ""),
        Col("entity", "entity", text(32), False, "MAIN"),
        Col("currency", "currency", parse_ccy, False, "INR"),
        Col("fx_rate", "fx_rate", parse_rate, False),
    ], fx=("posting_date", ["amount_paise"])),
    TableSpec("plan_metrics", ErpPlanMetric, [
        Col("business_key", "business_key", text(64), False),
        Col("week", "week", text(16)),
        Col("metric_id", "metric_id", text(64)),
        Col("segment", "segment", text(64), False, ""),
        Col("value", "value", parse_float),
    ], key_from=lambda r: f"PLAN-{r['week']}-{r['metric_id']}-{r.get('segment') or 'ALL'}",
        check=lambda r: _check_week(r["week"]), fixed={"kind": "plan", "source_connector": "upload"}),
]
SPEC_BY_NAME = {s.name: s for s in SPECS}
ORDER = {s.name: i for i, s in enumerate(SPECS)}

def table_for_file(name: str) -> str | None:
    """`<table>.csv` or `<table>_<anything>.csv` (longest matching table name wins)."""
    lower = name.lower()
    if not lower.endswith(".csv"):
        return None
    stem = lower[:-4]
    matches = [t for t in SPEC_BY_NAME if stem == t or stem.startswith(t + "_")]
    return max(matches, key=len) if matches else None


def template_header(table: str) -> list[str]:
    return [c.name for c in SPEC_BY_NAME[table].cols]


# ── validation & load ────────────────────────────────────────────────────────

@dataclass
class FileResult:
    filename: str
    table: str
    status: str  # loaded|rejected|duplicate|would_load
    rows: int = 0
    inserted: int = 0
    updated: int = 0
    errors: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "file": self.filename,
            "table": self.table,
            "status": self.status,
            "rows": self.rows,
            "inserted": self.inserted,
            "updated": self.updated,
            "errors": self.errors[:20],
            "error_count": len(self.errors),
        }


def _validate(
    session: Session,
    spec: TableSpec,
    content: str,
    filename: str,
    pending_keys: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """Parse a CSV file's rows; see `validate_records`."""
    reader = csv.DictReader(io.StringIO(content))
    headers = [h.strip() for h in (reader.fieldnames or [])]
    reader.fieldnames = headers
    return validate_records(session, spec, headers, enumerate(reader, start=2), pending_keys)


def _fx_lookup(session: Session, ccy: str, on: date, pending: dict | None) -> float | None:
    """Latest rate for `ccy` on or up to 7 days before `on` (weekends, holidays)."""
    from datetime import timedelta

    row = (
        session.query(ErpFxRate)
        .filter(ErpFxRate.currency == ccy, ErpFxRate.rate_date <= on, ErpFxRate.rate_date >= on - timedelta(days=7))
        .order_by(ErpFxRate.rate_date.desc())
        .first()
    )
    best = (row.rate_date, row.rate_to_inr) if row else None
    for d, c, r in (pending or {}).get("fx", []):
        if c == ccy and on - timedelta(days=7) <= d <= on and (best is None or d > best[0]):
            best = (d, r)
    return best[1] if best else None


def validate_records(
    session: Session,
    spec: TableSpec,
    headers: list[str],
    records,
    pending_keys: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """Validate (line, {column: text}) records for one table — used by the file drop
    and by every extractor (Tally, read-only SQL). Returns (clean rows, errors). Keys
    referenced by rows are resolved against the DB, or — in a dry run, where nothing
    is written — against keys of earlier valid batches (`pending_keys`)."""
    errors: list[dict] = []
    missing = [c.name for c in spec.cols if c.required and c.name not in headers]
    if missing:
        return [], [{"line": 1, "column": ",".join(missing), "error": "missing required column(s)"}]

    ref_cache: dict[tuple[type, str], int | None] = {}

    def resolve(model: type, key: str) -> int | None:
        k = (model, key)
        if k not in ref_cache:
            row = session.query(model.id).filter_by(business_key=key).one_or_none()
            if row:
                ref_cache[k] = row[0]
            elif pending_keys and key in pending_keys.get(model, set()):
                ref_cache[k] = -1  # will exist once the batch loads
            else:
                ref_cache[k] = None
        return ref_cache[k]

    accounts: dict[str, str] | None = None
    rows: list[dict] = []
    seen: set[str] = set()
    for line, raw in records:
        if len(errors) >= MAX_ERRORS_PER_FILE:
            break
        if not any(str(v or "").strip() for v in raw.values()):
            continue  # blank line
        rec: dict[str, Any] = {}
        ok = True
        for c in spec.cols:
            v = raw.get(c.name)
            if v is None or str(v).strip() == "":
                if c.required:
                    errors.append({"line": line, "column": c.name, "error": "required"})
                    ok = False
                elif c.default is not None:
                    rec[c.attr] = c.default
                continue
            try:
                rec[c.attr] = c.parse(str(v))
            except RowError as e:
                errors.append({"line": line, "column": c.name, "error": str(e)})
                ok = False
        if not ok:
            continue
        for csv_col, (attr, model) in spec.refs.items():
            ref_id = resolve(model, rec[attr])
            if ref_id is None:
                errors.append({"line": line, "column": csv_col, "error": f"unknown {model.__tablename__.removeprefix('erp_')} '{rec[attr]}'"})
                ok = False
            else:
                rec[attr] = ref_id
        if ok and spec.fx:
            date_attr, amount_attrs = spec.fx
            ccy = rec.get("currency")
            if ccy is None and spec.model is ErpCashDaily:
                if accounts is None:
                    accounts = {a.business_key: a.currency for a in session.query(ErpBankAccount).all()}
                ccy = accounts.get(rec.get("account_key", "PRIMARY"), "INR")
            ccy = ccy or "INR"
            rate = rec.get("fx_rate")
            if ccy == "INR":
                rate = 1.0
            elif rate is None:
                rate = _fx_lookup(session, ccy, rec[date_attr], pending_keys)
                if rate is None:
                    errors.append({"line": line, "column": "fx_rate",
                                   "error": f"no {ccy}→INR rate within 7 days of {rec[date_attr].isoformat()} (add fx_rate or an fx_rates file)"})
                    ok = False
            if ok:
                for a in amount_attrs:
                    rec[a] = int(round(rec[a] * rate))
                if spec.fx_stored:
                    rec["currency"], rec["fx_rate"] = ccy, float(rate)
                else:
                    rec.pop("currency", None)
                    rec.pop("fx_rate", None)
        if ok and spec.check:
            try:
                spec.check(rec)
            except RowError as e:
                errors.append({"line": line, "column": "", "error": str(e)})
                ok = False
        if not ok:
            continue
        rec.update(spec.fixed)
        if not rec.get("business_key"):
            rec["business_key"] = spec.key_from(rec) if spec.key_from else None
        if rec["business_key"] in seen:
            errors.append({"line": line, "column": "business_key", "error": f"duplicate '{rec['business_key']}' in file"})
            continue
        seen.add(rec["business_key"])
        rows.append(rec)
    return rows, errors


def note_pending(pending: dict, spec: TableSpec, rows: list[dict]) -> None:
    pending.setdefault(spec.model, set()).update(r["business_key"] for r in rows)
    if spec.model is ErpFxRate:
        pending.setdefault("fx", []).extend((r["rate_date"], r["currency"], r["rate_to_inr"]) for r in rows)


def _upsert(session: Session, spec: TableSpec, rows: list[dict]) -> tuple[int, int]:
    model = spec.model
    keys = [r["business_key"] for r in rows]
    existing = {}
    for i in range(0, len(keys), 500):
        for obj in session.query(model).filter(model.business_key.in_(keys[i:i + 500])).all():
            existing[obj.business_key] = obj
    ins = upd = 0
    for r in rows:
        obj = existing.get(r["business_key"])
        if obj is None:
            session.add(model(**r))
            ins += 1
        else:
            changed = False
            for k, v in r.items():
                if getattr(obj, k) != v:
                    setattr(obj, k, v)
                    changed = True
            upd += int(changed)
    session.flush()
    return ins, upd


def _move(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    shutil.move(str(src), dest)
    return dest


def process_inbox(
    session: Session,
    inbox: Path,
    *,
    connector_id: str = "csv_drop",
    run_id: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Validate and load every CSV waiting in `inbox`. Returns a run summary.
    The caller commits; files are moved only after a successful flush of their rows."""
    inbox.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    results: list[FileResult] = []
    unknown: list[str] = []
    files = []
    for p in sorted(inbox.iterdir()):
        if not p.is_file() or not p.name.lower().endswith(".csv"):
            continue
        t = table_for_file(p.name)
        if t is None:
            unknown.append(p.name)
            continue
        files.append((ORDER[t], p.name, p, t))
    files.sort()

    totals: dict[str, dict[str, int]] = {}
    pending_keys: dict = {}
    for _, _, path, table in files:
        spec = SPEC_BY_NAME[table]
        sha = ""
        if path.stat().st_size > MAX_FILE_BYTES:
            fr = FileResult(path.name, table, "rejected", errors=[{"line": 0, "column": "", "error": "file larger than 50 MB"}])
        else:
            data = path.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            dup = session.query(ImportedFile).filter_by(connector_id=connector_id, sha256=sha, status="loaded").first()
            if dup:
                fr = FileResult(path.name, table, "duplicate")
            else:
                try:
                    content = data.decode("utf-8-sig")
                except UnicodeDecodeError:
                    content = data.decode("cp1252", errors="replace")  # Excel "CSV" on Windows
                rows, errs = _validate(session, spec, content, path.name, pending_keys if dry_run else None)
                if not errs:
                    note_pending(pending_keys, spec, rows)
                fr = FileResult(path.name, table, "rejected" if errs else "loaded", rows=len(rows) + len(errs), errors=errs)
                if not errs and not dry_run:
                    # All-or-nothing per file: every row was validated above, so the
                    # file is either rejected untouched or upserted in full.
                    fr.inserted, fr.updated = _upsert(session, spec, rows)
                    t = totals.setdefault(spec.model.__tablename__, {"created": 0, "updated": 0})
                    t["created"] += fr.inserted
                    t["updated"] += fr.updated
                elif not errs and dry_run:
                    fr.status = "would_load"
            if not dry_run:
                session.add(ImportedFile(
                    connector_id=connector_id, run_id=run_id, filename=path.name, table=table,
                    sha256=sha, status=fr.status, rows_inserted=fr.inserted,
                    rows_updated=fr.updated, errors_json=fr.errors[:MAX_ERRORS_PER_FILE],
                ))
        if not dry_run:
            if fr.status == "rejected":
                dest = _move(path, inbox / "rejected" / stamp)
                with (dest.parent / (dest.name + ".errors.csv")).open("w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=["line", "column", "error"])
                    w.writeheader()
                    w.writerows(fr.errors)
            else:
                _move(path, inbox / "processed" / stamp)
        results.append(fr)

    return {
        **totals,
        "files": [r.summary() for r in results],
        "unrecognised_files": unknown,
        "loaded": sum(r.status == "loaded" for r in results),
        "rejected": sum(r.status == "rejected" for r in results),
        "duplicates": sum(r.status == "duplicate" for r in results),
    }


def export_tables_to_csv(session: Session, out_dir: Path) -> list[Path]:
    """Write the current ERP tables in drop format (templates, demos, round-trip tests)."""
    from cfo_agent.timeutil import format_inr  # noqa: F401  (kept for parity of formats)

    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    id_to_key: dict[type, dict[int, str]] = {}
    for spec in SPECS:
        rows = session.query(spec.model).filter_by(**spec.fixed).order_by(spec.model.id).all()
        id_to_key[spec.model] = {r.id: r.business_key for r in rows}
        path = out_dir / f"{spec.name}.csv"
        ref_by_attr = {attr: (csv_col, model) for csv_col, (attr, model) in spec.refs.items()}
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([c.name for c in spec.cols])
            for r in rows:
                out = []
                for c in spec.cols:
                    v = getattr(r, c.attr, None)
                    if c.attr in ref_by_attr:
                        v = id_to_key[ref_by_attr[c.attr][1]].get(v, "")
                    elif c.attr.endswith("_paise"):
                        v = f"{Decimal(v) / 100:.2f}"
                    elif isinstance(v, date):
                        v = v.isoformat()
                    elif isinstance(v, bool):
                        v = "yes" if v else "no"
                    out.append("" if v is None else v)
                w.writerow(out)
        written.append(path)
    return written
