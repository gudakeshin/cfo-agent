"""Multi-entity / multi-currency data model and the Tally + read-only SQL extractors."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import httpx
import pytest

from cfo_agent.connectors.csv_drop import SPEC_BY_NAME, process_inbox, validate_records
from cfo_agent.connectors.extractor import ExtractBatch, load_batch
from cfo_agent.connectors.sql_source import SqlReadOnlyExtractor, validate_query
from cfo_agent.connectors.tally import TallyExtractor
from cfo_agent.models import (
    ConnectorConfig,
    ConnectorSyncRun,
    ErpBankAccount,
    ErpCashDaily,
    ErpCreditNote,
    ErpCustomer,
    ErpReceipt,
    ErpSalesInvoice,
)
from cfo_agent.signals import compute_ar_overdue, compute_cash, compute_close_cycle, compute_dso, entity_scope
from cfo_agent.timeutil import iso_week_id


def _write(inbox, name, text):
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / name).write_text(text, encoding="utf-8")


# ── currencies ──────────────────────────────────────────────────────────────

def test_foreign_currency_invoice_converted_with_fx_table(db_session, tmp_path):
    inbox = tmp_path / "in"
    _write(inbox, "customers.csv", "business_key,name,segment\nC-US,Acme Inc,Exports\n")
    _write(inbox, "fx_rates.csv", "rate_date,currency,rate_to_inr\n2026-07-10,USD,83.25\n")
    _write(inbox, "sales_invoices.csv",
           "business_key,customer_key,invoice_date,due_date,amount_inr,segment,currency\n"
           "INV-US-1,C-US,2026-07-12,2026-08-11,1000.00,Exports,USD\n")
    # dry run resolves the rate from the fx_rates file in the same drop
    dry = process_inbox(db_session, inbox, dry_run=True)
    assert dry["rejected"] == 0, dry
    res = process_inbox(db_session, inbox)
    db_session.commit()
    assert res["rejected"] == 0
    inv = db_session.query(ErpSalesInvoice).filter_by(business_key="INV-US-1").one()
    assert inv.currency == "USD" and inv.fx_rate == 83.25
    assert inv.amount_paise == 8_325_000  # $1,000 × 83.25 = ₹83,250


def test_foreign_currency_without_rate_is_rejected(db_session):
    spec = SPEC_BY_NAME["expenses"]
    rows, errs = validate_records(db_session, spec, ["business_key", "expense_date", "cost_center", "amount_inr", "currency"],
                                  [(2, {"business_key": "E1", "expense_date": "2026-07-01", "cost_center": "Ops",
                                        "amount_inr": "10", "currency": "EUR"})])
    assert not rows and "no EUR→INR rate" in errs[0]["error"]


# ── credit notes, intercompany, accounts, entities ──────────────────────────

def test_credit_note_reduces_open_receivable(seeded_erp, db_session):
    from cfo_agent.signals import open_receivables

    end = date.fromisoformat(seeded_erp["end"])
    before = dict((i.id, rem) for i, rem in open_receivables(db_session, end))
    inv = (db_session.query(ErpSalesInvoice).filter(ErpSalesInvoice.status != "paid")
           .order_by(ErpSalesInvoice.amount_paise.desc()).first())
    db_session.add(ErpCreditNote(business_key="CN-1", invoice_id=inv.id, note_date=inv.invoice_date,
                                 amount_paise=inv.amount_paise // 2, reason="price dispute"))
    db_session.flush()
    after = dict((i.id, rem) for i, rem in open_receivables(db_session, end))
    assert after.get(inv.id, 0) == before[inv.id] - inv.amount_paise // 2
    assert compute_dso(db_session, seeded_erp["weeks"][-1]).available


def test_intercompany_customers_are_excluded(seeded_erp, db_session):
    week = seeded_erp["weeks"][-1]
    before = compute_ar_overdue(db_session, week)
    anchor = db_session.query(ErpCustomer).filter_by(business_key="CUST-APEX").one()
    anchor.is_intercompany = True
    db_session.flush()
    after = compute_ar_overdue(db_session, week)
    apex_ids = {i.business_key for i in db_session.query(ErpSalesInvoice).filter_by(customer_id=anchor.id)}
    assert not any(c.split(":")[-1] in apex_ids for c in after.lineage)
    assert after.value != before.value


def test_cash_sums_accounts_excludes_restricted_and_scopes_entity(seeded_erp, db_session):
    week = seeded_erp["weeks"][-1]
    end = date.fromisoformat(seeded_erp["end"])
    base = compute_cash(db_session, week).value
    db_session.add_all([
        ErpBankAccount(business_key="SUB-USD", entity="SUB1", currency="USD", label="USD account"),
        ErpBankAccount(business_key="ESCROW", entity="MAIN", is_restricted=True, label="Escrow"),
        ErpCashDaily(business_key="C-SUB", as_of_date=end - timedelta(days=2), account_key="SUB-USD", balance_paise=5 * 10**9),
        ErpCashDaily(business_key="C-ESC", as_of_date=end, account_key="ESCROW", balance_paise=9 * 10**9),
    ])
    db_session.flush()
    total = compute_cash(db_session, week)
    assert total.value == pytest.approx(base + 5.0, abs=0.01)  # ₹5 Cr in SUB1, escrow excluded
    assert len(total.lineage) == 2
    with entity_scope("SUB1"):
        assert compute_cash(db_session, week).value == pytest.approx(5.0)


def test_close_cycle_comes_from_the_general_ledger(seeded_erp, db_session):
    r = compute_close_cycle(db_session, seeded_erp["weeks"][-1])
    assert r.lineage and r.lineage[0].startswith("erp:erp_gl_entries:")
    assert 3 <= r.value <= 8


# ── Tally ───────────────────────────────────────────────────────────────────

DEBTORS = """<ENVELOPE><BODY><DATA><COLLECTION>
<LEDGER NAME="Apex Retail"><PARENT>Sundry Debtors</PARENT><BILLCREDITPERIOD>30 Days</BILLCREDITPERIOD></LEDGER>
</COLLECTION></DATA></BODY></ENVELOPE>"""

VOUCHERS = """<ENVELOPE><BODY><DATA><COLLECTION>
<VOUCHER VCHTYPE="Sales"><DATE>20260701</DATE><VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
 <VOUCHERNUMBER>S-101</VOUCHERNUMBER><PARTYLEDGERNAME>Apex Retail</PARTYLEDGERNAME><ALTERID>41</ALTERID>
 <ALLLEDGERENTRIES.LIST><LEDGERNAME>Apex Retail</LEDGERNAME><AMOUNT>-1,18,000.00</AMOUNT>
  <BILLALLOCATIONS.LIST><NAME>S-101</NAME><BILLTYPE>New Ref</BILLTYPE><AMOUNT>-118000.00</AMOUNT></BILLALLOCATIONS.LIST>
 </ALLLEDGERENTRIES.LIST>
 <ALLLEDGERENTRIES.LIST><LEDGERNAME>Sales</LEDGERNAME><AMOUNT>100000.00</AMOUNT></ALLLEDGERENTRIES.LIST>
</VOUCHER>
<VOUCHER VCHTYPE="Receipt"><DATE>20260715</DATE><VOUCHERTYPENAME>Receipt</VOUCHERTYPENAME>
 <VOUCHERNUMBER>R-9</VOUCHERNUMBER><PARTYLEDGERNAME>Apex Retail</PARTYLEDGERNAME><ALTERID>57</ALTERID>
 <ALLLEDGERENTRIES.LIST><LEDGERNAME>Apex Retail</LEDGERNAME><AMOUNT>50000.00</AMOUNT>
  <BILLALLOCATIONS.LIST><NAME>S-101</NAME><BILLTYPE>Agst Ref</BILLTYPE><AMOUNT>50000.00</AMOUNT></BILLALLOCATIONS.LIST>
 </ALLLEDGERENTRIES.LIST>
</VOUCHER>
<VOUCHER VCHTYPE="Sales"><DATE>20260716</DATE><VOUCHERTYPENAME>Sales</VOUCHERTYPENAME><ISCANCELLED>Yes</ISCANCELLED>
 <VOUCHERNUMBER>S-102</VOUCHERNUMBER><PARTYLEDGERNAME>Apex Retail</PARTYLEDGERNAME><ALTERID>58</ALTERID>&#4;
</VOUCHER>
</COLLECTION></DATA></BODY></ENVELOPE>"""


def _tally_transport(seen: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        seen.append(body)
        assert "<TALLYREQUEST>Export</TALLYREQUEST>" in body  # never Import
        if "<ID>CfoDebtors</ID>" in body:
            return httpx.Response(200, text=DEBTORS)
        if "<ID>CfoCompanies</ID>" in body:
            return httpx.Response(200, text="<ENVELOPE><BODY><DATA><COLLECTION><COMPANY NAME='Demo Co'/></COLLECTION></DATA></BODY></ENVELOPE>")
        return httpx.Response(200, text=VOUCHERS)
    return httpx.MockTransport(handler)


def test_tally_extract_and_load(db_session):
    seen: list[str] = []
    ex = TallyExtractor("http://tally:9000", "Demo Co", transport=_tally_transport(seen))
    assert ex.test()["ok"]
    batch = ex.extract("40")
    assert "$AlterID &gt; 40" in seen[-1]
    assert batch.watermark == "58"
    inv = batch.tables["sales_invoices"]
    assert inv == [{"business_key": "TS-S-101", "customer_key": "Apex Retail", "invoice_date": "2026-07-01",
                    "due_date": "2026-07-31", "amount_inr": "118000.00", "segment": "Sundry Debtors", "entity": "MAIN"}]
    assert batch.tables["receipts"][0]["invoice_key"] == "TS-S-101"
    res = load_batch(db_session, batch, connector_id="tally")
    db_session.commit()
    assert res["rejected"] == 0
    rec = db_session.query(ErpReceipt).one()
    assert rec.amount_paise == 5_000_000
    assert db_session.query(ErpSalesInvoice).one().amount_paise == 11_800_000


def test_extractor_sync_advances_watermark_only_on_success(db_session, monkeypatch):
    from cfo_agent.connectors import sync

    batches = [
        ExtractBatch(tables={"receipts": [{"business_key": "R1", "invoice_key": "NOPE", "receipt_date": "2026-07-01", "amount_inr": "5"}]}, watermark="99"),
        ExtractBatch(tables={"customers": [{"business_key": "C1", "name": "New Co", "segment": "X"}]}, watermark="120"),
    ]

    class Fake:
        def extract(self, since):
            return batches.pop(0)

    monkeypatch.setattr(sync, "build_extractor", lambda cfg: Fake())
    db_session.add(ConnectorConfig(id="tally", enabled=True, config_json={"url": "http://x"}, watermark="50"))
    db_session.commit()
    for expected_status, expected_wm in (("error", "50"), ("ok", "120")):
        run = ConnectorSyncRun(connector_id="tally", trigger="manual", status="running")
        db_session.add(run)
        db_session.commit()
        sync.run_connector_sync(run.id)
        db_session.expire_all()
        assert db_session.get(ConnectorSyncRun, run.id).status == expected_status
        assert db_session.get(ConnectorConfig, "tally").watermark == expected_wm


# ── read-only SQL ───────────────────────────────────────────────────────────

def test_sql_query_validation():
    assert validate_query("SELECT 1 -- ok\n;") == "SELECT 1"
    for bad in ("DELETE FROM x", "SELECT 1; DROP TABLE x", "WITH a AS (SELECT 1) INSERT INTO t SELECT * FROM a",
                "SELECT * INTO new_t FROM x", "update t set a=1"):
        with pytest.raises(ValueError):
            validate_query(bad)


def test_sql_extractor_reads_incrementally_and_is_read_only(db_session, tmp_path):
    src = tmp_path / "erp.db"
    con = sqlite3.connect(src)
    con.executescript("""
        CREATE TABLE debtors (code TEXT, name TEXT, seg TEXT, updated_at TEXT);
        INSERT INTO debtors VALUES ('C1','Bharat Foods','Consumer','2026-07-01T10:00:00'),
                                   ('C2','Sagar Metals','Industrial','2026-07-05T09:00:00');
    """)
    con.commit()
    con.close()
    q = {"customers": "SELECT code AS business_key, name, seg AS segment, updated_at AS _modified_at "
                      "FROM debtors WHERE updated_at > :since"}
    ex = SqlReadOnlyExtractor(f"sqlite:///{src}", q)
    assert ex.test()["ok"]
    b1 = ex.extract(None)
    assert len(b1.tables["customers"]) == 2 and b1.watermark == "2026-07-05T09:00:00"
    assert "_modified_at" not in b1.tables["customers"][0]
    assert load_batch(db_session, b1, connector_id="sql_readonly")["rejected"] == 0
    assert ex.extract(b1.watermark).tables["customers"] == []
    # the session refuses writes even if a query slipped past validation
    from sqlalchemy import text as sqltext

    eng = ex._engine()
    with ex._connect(eng) as conn, pytest.raises(Exception):
        conn.execute(sqltext("INSERT INTO debtors VALUES ('X','x','x','x')"))
    eng.dispose()


def test_iso_week_helper_is_consistent():
    assert iso_week_id(date(2026, 7, 12)).startswith("2026-W")
