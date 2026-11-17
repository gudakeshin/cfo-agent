"""File-drop connector: round trip, validation, idempotency, corrections, safety."""

from __future__ import annotations

import shutil

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cfo_agent.api import api_router
from cfo_agent.connectors.csv_drop import (
    SPECS,
    export_tables_to_csv,
    parse_date,
    parse_inr_to_paise,
    process_inbox,
    table_for_file,
)
from cfo_agent.models import Base, ErpCashDaily, ErpSalesInvoice, ImportedFile
from cfo_agent.settings import get_settings
from cfo_agent.signals import COMPUTE_FNS


def _wipe_erp(session):
    for spec in reversed(SPECS):
        session.query(spec.model).delete()
    session.commit()


def test_parsers():
    assert parse_inr_to_paise("1,23,456.78") == 12345678
    assert parse_inr_to_paise("₹ 5") == 500
    assert parse_inr_to_paise("(10.50)", allow_negative=True) == -1050
    with pytest.raises(ValueError):
        parse_inr_to_paise("1.234")
    with pytest.raises(ValueError):
        parse_inr_to_paise("-5")
    assert str(parse_date("12-07-2026")) == "2026-07-12"
    assert str(parse_date("12/07/2026")) == "2026-07-12"
    assert table_for_file("sales_invoices_2026-07-12.csv") == "sales_invoices"
    assert table_for_file("receipts.csv") == "receipts"
    assert table_for_file("payroll.csv") is None


def test_round_trip_reproduces_metrics(seeded_erp, db_session, tmp_path):
    """Export the synthetic ERP as CSV, wipe it, import the files: every metric matches."""
    week = seeded_erp["weeks"][-1]
    before = {m: fn(db_session, week).value for m, fn in COMPUTE_FNS.items()}
    export_tables_to_csv(db_session, tmp_path / "inbox")
    _wipe_erp(db_session)
    dry = process_inbox(db_session, tmp_path / "inbox", dry_run=True)
    assert dry["rejected"] == 0, [f for f in dry["files"] if f["status"] == "rejected"]
    assert list((tmp_path / "inbox").glob("*.csv")), "dry run must not move files"
    summary = process_inbox(db_session, tmp_path / "inbox")
    db_session.commit()
    assert summary["rejected"] == 0 and summary["loaded"] == len(SPECS), summary
    after = {m: fn(db_session, week).value for m, fn in COMPUTE_FNS.items()}
    assert after == before
    assert (tmp_path / "inbox" / "processed").is_dir()
    assert not list((tmp_path / "inbox").glob("*.csv"))


def test_bad_file_rejected_whole_with_report(seeded_erp, db_session, tmp_path):
    inbox = tmp_path / "in"
    inbox.mkdir()
    cust = db_session.query(ErpSalesInvoice).first()
    n_before = db_session.query(ErpSalesInvoice).count()
    (inbox / "sales_invoices.csv").write_text(
        "business_key,customer_key,invoice_date,due_date,amount_inr,segment\n"
        "NEW-1,NOPE,2026-07-01,2026-07-31,1000,Retail\n"     # unknown customer
        "NEW-2,X,2026-07-01,2026-06-01,1000,Retail\n"        # due before invoice (+ unknown)
        "NEW-3,X,yesterday,2026-07-31,abc,Retail\n"          # bad date + bad amount
    )
    summary = process_inbox(db_session, inbox)
    db_session.commit()
    assert summary["rejected"] == 1 and summary["loaded"] == 0
    assert db_session.query(ErpSalesInvoice).count() == n_before  # nothing partial
    f = summary["files"][0]
    assert f["error_count"] >= 4
    rejected = list((inbox / "rejected").rglob("sales_invoices.csv.errors.csv"))
    assert rejected and "unknown customer" in rejected[0].read_text()
    assert cust is not None


def test_missing_column_rejected(db_session, tmp_path):
    inbox = tmp_path / "in"
    inbox.mkdir()
    (inbox / "cash_daily.csv").write_text("as_of_date\n2026-07-01\n")
    summary = process_inbox(db_session, inbox)
    assert summary["files"][0]["errors"][0]["column"] == "balance_inr"


def test_same_file_twice_is_not_reloaded_and_corrections_update(seeded_erp, db_session, tmp_path):
    inbox = tmp_path / "in"
    inbox.mkdir()
    content = "as_of_date,balance_inr\n12-07-2026,\"2,10,00,000.00\"\n"
    (inbox / "cash_daily.csv").write_text(content)
    s1 = process_inbox(db_session, inbox)
    db_session.commit()
    row = db_session.query(ErpCashDaily).filter_by(business_key="CASH-2026-07-12").one()
    assert row.balance_paise == 2_10_00_000_00
    assert s1["files"][0]["updated"] == 1 or s1["files"][0]["inserted"] == 1

    (inbox / "cash_daily.csv").write_text(content)  # identical file again
    s2 = process_inbox(db_session, inbox)
    db_session.commit()
    assert s2["duplicates"] == 1

    # A back-dated correction for an old day updates the existing record
    (inbox / "cash_daily_fix.csv").write_text("as_of_date,balance_inr\n2026-07-12,20500000\n")
    s3 = process_inbox(db_session, inbox)
    db_session.commit()
    assert s3["files"][0]["updated"] == 1
    db_session.refresh(row)
    assert row.balance_paise == 2_05_00_000_00
    assert db_session.query(ImportedFile).count() == 3


def test_connector_api_dry_run_and_sync(seeded_erp, db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("CSV_INBOX_DIR", str(tmp_path / "root"))
    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(api_router)
    c = TestClient(app)
    assert c.put("/api/connectors/csv_drop", json={"enabled": True, "config": {"folder": "acme"}}).status_code == 200
    inbox = tmp_path / "root" / "acme"
    inbox.mkdir(parents=True)
    (inbox / "gst_events.csv").write_text(
        "business_key,event_date,event_type,amount_inr,status\nGST-NEW-1,2026-07-10,show_cause_notice,500000,open\n"
    )
    t = c.post("/api/connectors/csv_drop/test").json()
    assert t["ok"] and "1 file(s) waiting" in t["message"]
    assert (inbox / "gst_events.csv").exists()  # dry run moved nothing
    r = c.post("/api/connectors/csv_drop/sync").json()
    run = c.get(f"/api/connectors/runs/{r['run_id']}").json()
    assert run["status"] == "ok", run
    imports = c.get("/api/connectors/csv_drop/imports").json()
    assert imports[0]["status"] == "loaded" and imports[0]["inserted"] == 1
    assert c.get("/api/connectors/templates/receipts.csv").text.startswith("business_key,invoice_key")
    # folder names cannot escape the inbox root
    c.put("/api/connectors/csv_drop", json={"config": {"folder": "../../etc"}})
    assert c.post("/api/connectors/csv_drop/test").json()["ok"] is False
    get_settings.cache_clear()


def test_simulated_connectors_unaffected():
    from cfo_agent.connectors.catalog import OWNERSHIP

    assert all("csv_drop" not in owners for owners in OWNERSHIP.values())
    assert Base.metadata.tables["erp_sales_invoices"].c.amount_paise.type.__class__.__name__ == "BigInteger"
