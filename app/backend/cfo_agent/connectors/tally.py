"""Tally Prime / Tally.ERP 9 connector (read-only, over Tally's XML HTTP server).

Tally exposes company data on its XML server (Help › Settings › Connectivity:
"TallyPrime acts as Server", default port 9000). We send TDL *Collection* export
requests and read:

  * Sundry Debtors ledgers                  → customers
  * Sales vouchers                          → sales_invoices
  * Receipt vouchers (bill-wise "Agst Ref") → receipts
  * Credit Note vouchers (bill-wise)        → credit_notes
  * Purchase vouchers                       → purchase_invoices
  * Payment vouchers (bill-wise "Agst Ref") → payments

Vouchers are pulled incrementally by Tally's `ALTERID`, which increases on every
create or alteration; the highest AlterID seen is the watermark. Deleted vouchers
leave no trace in an incremental pull, so schedule a periodic full sync (mode=full).
Nothing is ever written to Tally: only Export requests are sent.

Field mapping assumptions (confirm against the client's company before go-live):
  * The bill reference (BILLALLOCATIONS NAME) of a sales/purchase voucher is the
    invoice number, and receipts/payments/credit notes allocate against it.
  * Credit period comes from the bill allocation's BILLCREDITPERIOD (e.g. "45 Days"),
    else the party ledger's, else `default_terms_days`.
  * Amounts are rupees; Tally signs debits negative, so absolute values are used.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from xml.sax.saxutils import escape

import defusedxml.ElementTree as ET
import httpx

from cfo_agent.connectors.extractor import ExtractBatch

logger = logging.getLogger(__name__)

VOUCHER_TABLES = {
    "Sales": "sales_invoices",
    "Receipt": "receipts",
    "Credit Note": "credit_notes",
    "Purchase": "purchase_invoices",
    "Payment": "payments",
}
_BAD_XML_REF = re.compile(r"&#(?:x[0-9a-fA-F]+|\d+);")


def _collection_request(company: str, name: str, type_: str, fetch: str, filter_formula: str | None) -> str:
    filt = ""
    system = ""
    if filter_formula:
        filt = "<FILTER>CfoFilter</FILTER>"
        system = f'<SYSTEM TYPE="Formulae" NAME="CfoFilter">{escape(filter_formula)}</SYSTEM>'
    company_var = f"<SVCURRENTCOMPANY>{escape(company)}</SVCURRENTCOMPANY>" if company else ""
    return (
        "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
        f"<TYPE>Collection</TYPE><ID>{name}</ID></HEADER><BODY><DESC><STATICVARIABLES>"
        f"<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>{company_var}</STATICVARIABLES>"
        f'<TDL><TDLMESSAGE><COLLECTION NAME="{name}" ISMODIFY="No"><TYPE>{type_}</TYPE>'
        f"<FETCH>{fetch}</FETCH>{filt}</COLLECTION>{system}</TDLMESSAGE></TDL>"
        "</DESC></BODY></ENVELOPE>"
    )


def _parse(xml_text: str):
    # Tally emits control-character references (&#4;) that are not legal XML 1.0.
    cleaned = _BAD_XML_REF.sub(lambda m: "" if _is_ctrl(m.group(0)) else m.group(0), xml_text)
    return ET.fromstring(cleaned)


def _is_ctrl(ref: str) -> bool:
    body = ref[2:-1]
    n = int(body[1:], 16) if body.startswith("x") else int(body)
    return n < 32 and n not in (9, 10, 13)


def _t(el, tag: str) -> str:
    c = el.find(tag)
    return (c.text or "").strip() if c is not None and c.text else ""


def _tally_date(v: str) -> str:
    return datetime.strptime(v.strip(), "%Y%m%d").date().isoformat()


def _amount(v: str) -> str:
    v = (v or "0").replace(",", "").strip()
    # "-1,25,000.00" or "1250.00 Dr"; foreign amounts look like "$100 @ ₹83/$ = ₹8300"
    if "=" in v:
        v = v.split("=")[-1]
    v = re.sub(r"[^0-9.\-]", "", v)
    return f"{abs(float(v or 0)):.2f}"


def _days(v: str) -> int | None:
    m = re.search(r"(\d+)", v or "")
    return int(m.group(1)) if m else None


def _entries(voucher):
    return voucher.findall("ALLLEDGERENTRIES.LIST") + voucher.findall("LEDGERENTRIES.LIST")


class TallyExtractor:
    def __init__(self, url: str, company: str = "", *, default_terms_days: int = 45,
                 entity: str = "MAIN", timeout: float = 60.0, transport: httpx.BaseTransport | None = None):
        if not url.startswith(("http://", "https://")):
            raise ValueError("Tally URL must start with http:// or https://")
        self.url = url.rstrip("/")
        self.company = company
        self.default_terms = default_terms_days
        self.entity = entity
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def _post(self, body: str):
        r = self._client.post(self.url, content=body.encode("utf-8"),
                              headers={"Content-Type": "text/xml; charset=utf-8"})
        r.raise_for_status()
        root = _parse(r.text)
        err = root.find(".//LINEERROR")
        if err is not None and err.text:
            raise RuntimeError(f"Tally: {err.text.strip()}")
        return root

    def test(self) -> dict:
        root = self._post(_collection_request(self.company, "CfoCompanies", "Company", "NAME", None))
        names = [_t(c, "NAME") or c.get("NAME", "") for c in root.iter("COMPANY")]
        if self.company and self.company not in names:
            return {"ok": False, "message": f"Connected, but company '{self.company}' is not open in Tally ({', '.join(names) or 'none open'})"}
        return {"ok": True, "message": f"Connected to Tally — companies open: {', '.join(names) or 'none'}"}

    def extract(self, since: str | None) -> ExtractBatch:
        since_id = int(since) if since and since.isdigit() else 0
        batch = ExtractBatch(tables={t: [] for t in ("customers", *VOUCHER_TABLES.values())})

        # Masters: every debtor ledger (small; refreshed every sync)
        root = self._post(_collection_request(
            self.company, "CfoDebtors", "Ledger", "NAME,PARENT,BILLCREDITPERIOD,ALTERID",
            '$Parent = "Sundry Debtors" OR $$IsBelongsTo:$$GroupSundryDebtors',
        ))
        terms: dict[str, int] = {}
        for led in root.iter("LEDGER"):
            name = led.get("NAME") or _t(led, "NAME")
            if not name:
                continue
            days = _days(_t(led, "BILLCREDITPERIOD")) or self.default_terms
            terms[name] = days
            batch.tables["customers"].append({
                "business_key": name[:64], "name": name, "segment": _t(led, "PARENT") or "Sundry Debtors",
                "payment_terms_days": str(days), "entity": self.entity,
            })

        root = self._post(_collection_request(
            self.company, "CfoVouchers", "Voucher",
            "DATE,VOUCHERTYPENAME,VOUCHERNUMBER,PARTYLEDGERNAME,ALTERID,GUID,ISCANCELLED,ISOPTIONAL,"
            "ALLLEDGERENTRIES.LIST,LEDGERENTRIES.LIST",
            f"$AlterID > {since_id}",
        ))
        max_id = since_id
        skipped = 0
        for v in root.iter("VOUCHER"):
            vtype = _t(v, "VOUCHERTYPENAME") or v.get("VCHTYPE", "")
            alter = int(_t(v, "ALTERID") or 0)
            max_id = max(max_id, alter)
            table = VOUCHER_TABLES.get(vtype)
            if table is None or _t(v, "ISCANCELLED") == "Yes" or _t(v, "ISOPTIONAL") == "Yes":
                skipped += table is not None
                continue
            vno = _t(v, "VOUCHERNUMBER") or _t(v, "GUID")
            vdate = _tally_date(_t(v, "DATE"))
            party = _t(v, "PARTYLEDGERNAME")
            party_entries = [e for e in _entries(v) if _t(e, "LEDGERNAME") == party] or _entries(v)[:1]
            bills = [b for e in party_entries for b in e.findall("BILLALLOCATIONS.LIST")]
            party_amt = _amount(_t(party_entries[0], "AMOUNT")) if party_entries else "0.00"

            if table in ("sales_invoices", "purchase_invoices"):
                bill = next((b for b in bills if _t(b, "BILLTYPE") in ("New Ref", "")), None)
                ref = (_t(bill, "NAME") if bill is not None else "") or vno
                days = (_days(_t(bill, "BILLCREDITPERIOD")) if bill is not None else None) or terms.get(party, self.default_terms)
                due = (date.fromisoformat(vdate) + timedelta(days=days)).isoformat()
                if table == "sales_invoices":
                    batch.tables[table].append({
                        "business_key": f"TS-{ref}"[:64], "customer_key": party[:64], "invoice_date": vdate,
                        "due_date": due, "amount_inr": party_amt, "segment": "Sundry Debtors", "entity": self.entity,
                    })
                else:
                    batch.tables[table].append({
                        "business_key": f"TP-{ref}"[:64], "vendor_name": party, "invoice_date": vdate,
                        "due_date": due, "amount_inr": party_amt, "entity": self.entity,
                    })
                continue

            # Receipts, payments, credit notes: one row per bill allocated against
            prefix, key_col, date_col = {
                "receipts": ("TS-", "invoice_key", "receipt_date"),
                "credit_notes": ("TS-", "invoice_key", "note_date"),
                "payments": ("TP-", "purchase_invoice_key", "payment_date"),
            }[table]
            agst = [b for b in bills if _t(b, "BILLTYPE") == "Agst Ref"]
            if not agst:
                skipped += 1  # on-account or advance: not tied to an invoice
                continue
            for i, b in enumerate(agst, start=1):
                row = {
                    "business_key": f"T{table[0].upper()}-{vno}-{i}"[:64],
                    key_col: f"{prefix}{_t(b, 'NAME')}"[:64],
                    date_col: vdate,
                    "amount_inr": _amount(_t(b, "AMOUNT")),
                }
                if table == "credit_notes":
                    row["reason"] = "Tally credit note"
                batch.tables[table].append(row)

        batch.watermark = str(max_id)
        if skipped:
            batch.notes.append(f"{skipped} voucher(s) skipped (cancelled, optional, or not allocated to a bill)")
        return batch
