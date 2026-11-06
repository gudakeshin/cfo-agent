"""Deterministic synthetic ERP generator — ~13 ISO weeks ending last completed Sunday IST.

All monetary amounts are integer paise. ERP tables are written only via SourceAdapter
(demo analog of P0-3: no write path from the pipeline/API into ERP).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from cfo_agent.models import (
    ErpBankAccount,
    ErpCashDaily,
    ErpEntity,
    ErpGlEntry,
    ErpCovenant,
    ErpCustomer,
    ErpExpense,
    ErpGstEvent,
    ErpPayment,
    ErpPurchaseInvoice,
    ErpReceipt,
    ErpRevenueDaily,
    ErpSalesInvoice,
)
from cfo_agent.synthetic import scenarios as sc
from cfo_agent.synthetic.adapter import ERP_TABLES, SourceAdapter, SqlAlchemySourceAdapter
from cfo_agent.timeutil import (
    crore_to_paise,
    last_completed_sunday,
    quarter_weeks_ending,
    rupees_to_paise,
    week_monday,
    week_sunday,
)

SEGMENTS = ["Consumer", "Industrial", "Exports"]
COST_CENTERS = ["Finance", "Sales", "Ops", "Admin", "Plant"]
VENDORS = ["Bharat Steel Co", "Nova Logistics", "Precision Dies", "PowerGrid Spares", "ChemSource India"]

# Anchor customers for S1 DSO story
ANCHOR_CUSTOMERS = [
    ("CUST-APEX", "Apex Retail Pvt Ltd", "Consumer"),
    ("CUST-NEXUS", "Nexus Auto Components", "Industrial"),
    ("CUST-ORION", "Orion Infra Projects", "Industrial"),
]


def wipe_erp(session: Session) -> None:
    """Compatibility helper — prefer SourceAdapter.wipe() for new code."""
    SqlAlchemySourceAdapter(session).wipe()


def generate_erp(
    session: Session,
    *,
    seed: int = 42,
    stale_days: int = 0,
    end_sunday: date | None = None,
    n_weeks: int = 13,
    flags: sc.ScenarioFlags | None = None,
    adapter: SourceAdapter | None = None,
) -> dict[str, Any]:
    """Generate a full quarter of ERP data. Returns metadata summary.

    Writes go through ``adapter`` (default: SqlAlchemySourceAdapter). S5 staleness is
    controlled by ``stale_days``; S1–S4 by ``flags``.
    """
    adapter = adapter or SqlAlchemySourceAdapter(session)
    session = adapter.session
    flags = flags or sc.ScenarioFlags()
    rng = np.random.default_rng(seed)
    end = end_sunday or last_completed_sunday()
    weeks = quarter_weeks_ending(end, n_weeks)
    start = week_monday(weeks[0])
    end_d = week_sunday(weeks[-1])

    adapter.wipe()

    # Customers
    customers: list[ErpCustomer] = []
    for bk, name, seg in ANCHOR_CUSTOMERS:
        c = ErpCustomer(
            business_key=bk,
            name=name,
            segment=seg,
            payment_terms_days=45,
            is_anchor=True,
        )
        adapter.add(c)
        customers.append(c)

    for i in range(12):
        seg = SEGMENTS[i % 3]
        c = ErpCustomer(
            business_key=f"CUST-{100 + i}",
            name=f"Customer {100 + i} {seg}",
            segment=seg,
            payment_terms_days=int(rng.choice([30, 45, 60])),
            is_anchor=False,
        )
        adapter.add(c)
        customers.append(c)
    adapter.flush()

    # Covenant
    adapter.add(
        ErpCovenant(
            business_key="COV-CASH-MIN",
            name="Minimum cash balance",
            metric="cash_balance_cr",
            threshold_paise=crore_to_paise(20),
            buffered_threshold_paise=crore_to_paise(22),
            operator="gte",
        )
    )

    inv_seq = 1
    rcpt_seq = 1
    pin_seq = 1
    pay_seq = 1
    exp_seq = 1
    gst_seq = 1

    # Baseline daily cash ~ ₹28 Cr, drifts toward dip in final week
    cash_paise = crore_to_paise(28.0)

    # Track open invoices for receipts
    open_invoices: list[tuple[ErpSalesInvoice, date, int]] = []  # inv, expected_pay, remaining

    day = start
    week_index_by_date: dict[date, int] = {}
    for wi, wid in enumerate(weeks):
        d0 = week_monday(wid)
        for j in range(7):
            week_index_by_date[d0 + timedelta(days=j)] = wi

    while day <= end_d:
        wi = week_index_by_date[day]
        wid = weeks[wi]
        is_weekday = day.weekday() < 5

        # --- Sales invoices (weekdays) ---
        if is_weekday:
            day_customers: list[ErpCustomer] = []
            # S1: every weekday invoice each anchor (large) so lag stretch moves DSO
            anchors = [c for c in customers if c.is_anchor]
            day_customers.extend(anchors)
            # Plus 1–2 non-anchor invoices
            non_anchors = [c for c in customers if not c.is_anchor]
            for _ in range(int(rng.integers(1, 3))):
                day_customers.append(non_anchors[int(rng.integers(0, len(non_anchors)))])

            for cust in day_customers:
                if cust.is_anchor:
                    base = float(rng.uniform(4e6, 7e6))  # ₹40–70 L — dominate AR
                else:
                    base = float(rng.uniform(5e5, 2e6))
                if flags.segment_growth and cust.segment == sc.growth_segment() and wi >= 6:
                    base *= 1.0 + (sc.growth_segment_qoq_boost() - 1.0) * ((wi - 6) / 6.0)
                amount = rupees_to_paise(base)
                lag = (
                    sc.anchor_payment_lag_days(wi, enabled=flags.dso_drift)
                    if cust.is_anchor
                    else int(cust.payment_terms_days + rng.integers(-3, 5))
                )
                due = day + timedelta(days=int(cust.payment_terms_days))
                expected_pay = day + timedelta(days=lag)
                bk = f"INV-2026-{inv_seq:05d}"
                inv = ErpSalesInvoice(
                    business_key=bk,
                    customer_id=cust.id,
                    invoice_date=day,
                    due_date=due,
                    amount_paise=amount,
                    segment=cust.segment,
                    status="open",
                )
                adapter.add(inv)
                adapter.flush()
                open_invoices.append([inv, expected_pay, amount])  # type: ignore[arg-type]
                inv_seq += 1

            # Purchase invoices
            if rng.random() < 0.7:
                vendor = VENDORS[int(rng.integers(0, len(VENDORS)))]
                amount = rupees_to_paise(float(rng.uniform(5e5, 2.5e6)))
                terms = 45
                pin = ErpPurchaseInvoice(
                    business_key=f"PIN-2026-{pin_seq:05d}",
                    vendor_name=vendor,
                    invoice_date=day,
                    due_date=day + timedelta(days=terms),
                    amount_paise=amount,
                    status="open",
                )
                adapter.add(pin)
                adapter.flush()
                # Schedule payment ~ terms ± noise
                pay_day = day + timedelta(days=int(terms + rng.integers(-5, 10)))
                # store payment intent on object via parallel list
                pin._pay_day = pay_day  # type: ignore[attr-defined]
                pin._pay_amount = amount  # type: ignore[attr-defined]
                pin_seq += 1

            # Expenses by cost center
            for cc in COST_CENTERS:
                base_exp = {"Finance": 1.2e5, "Sales": 2.5e5, "Ops": 4e5, "Admin": 1.5e5, "Plant": 6e5}[cc]
                # mild variance; Ops overrun late quarter for Operator face
                mult = 1.0 + float(rng.normal(0, 0.08))
                if cc == "Ops" and wi >= 8:
                    mult *= 1.12
                exp = ErpExpense(
                    business_key=f"EXP-2026-{exp_seq:05d}",
                    expense_date=day,
                    cost_center=cc,
                    amount_paise=rupees_to_paise(base_exp * mult / 5),  # daily share
                    category="opex",
                )
                adapter.add(exp)
                exp_seq += 1

        # --- Receipts for due invoices ---
        still_open: list = []
        stale_cutoff = sc.apply_staleness_cutoff(end_d, stale_days)
        for inv, expected_pay, remaining in open_invoices:
            if remaining <= 0:
                continue
            # S1: as lag stretches, push out unpaid anchor payments to current lag
            cust = session.get(ErpCustomer, inv.customer_id)
            if cust and cust.is_anchor and flags.dso_drift:
                current_lag = sc.anchor_payment_lag_days(wi, enabled=True)
                stretched = inv.invoice_date + timedelta(days=current_lag)
                if stretched > expected_pay:
                    expected_pay = stretched
            if day >= expected_pay and (stale_cutoff is None or day <= stale_cutoff):
                # pay in full (sometimes partial for non-anchors)
                if cust and cust.is_anchor:
                    pay_amt = remaining
                else:
                    pay_amt = remaining if rng.random() > 0.15 else int(remaining * 0.6)
                pay_amt = min(pay_amt, remaining)
                if pay_amt > 0:
                    adapter.add(
                        ErpReceipt(
                            business_key=f"RCP-2026-{rcpt_seq:05d}",
                            invoice_id=inv.id,
                            receipt_date=day,
                            amount_paise=pay_amt,
                        )
                    )
                    rcpt_seq += 1
                    remaining -= pay_amt
                    cash_paise += pay_amt
                    if remaining <= 0:
                        inv.status = "paid"
                    else:
                        inv.status = "partial"
            if remaining > 0:
                still_open.append([inv, expected_pay, remaining])
        open_invoices = still_open

        # --- AP payments ---
        # Pay purchase invoices whose _pay_day has arrived
        for pin in session.query(ErpPurchaseInvoice).filter_by(status="open").all():
            pay_day = getattr(pin, "_pay_day", None)
            if pay_day is None:
                # reconstruct from due_date for rows without attr (after flush re-query)
                pay_day = pin.due_date
            if day >= pay_day and (stale_cutoff is None or day <= stale_cutoff):
                adapter.add(
                    ErpPayment(
                        business_key=f"PAY-2026-{pay_seq:05d}",
                        purchase_invoice_id=pin.id,
                        payment_date=day,
                        amount_paise=pin.amount_paise,
                    )
                )
                pay_seq += 1
                pin.status = "paid"
                cash_paise -= pin.amount_paise

        # --- Revenue daily by segment ---
        for seg in SEGMENTS:
            base = {"Consumer": 3.5e6, "Industrial": 4.2e6, "Exports": 2.1e6}[seg]
            if flags.segment_growth and seg == sc.growth_segment() and wi >= 6:
                base *= 1.0 + (sc.growth_segment_qoq_boost() - 1.0) * ((wi - 6) / 6.0)
            # weekend quieter
            if not is_weekday:
                base *= 0.25
            noise = float(rng.normal(1.0, 0.05))
            adapter.add(
                ErpRevenueDaily(
                    business_key=f"REV-{day.isoformat()}-{seg[:3].upper()}",
                    as_of_date=day,
                    segment=seg,
                    amount_paise=rupees_to_paise(base * noise),
                )
            )

        # --- Cash daily path ---
        # Drift: gradual decline, then S3 dip in week 13
        target = crore_to_paise(26.0) - int(wi * crore_to_paise(0.25))
        if flags.cash_dip and wi == sc.cash_dip_week_index():
            # Pull toward 21.4 Cr over the week
            target = sc.cash_dip_balance_paise()
        # mean-revert toward target with noise
        cash_paise = int(cash_paise * 0.85 + target * 0.15 + rng.normal(0, crore_to_paise(0.05)))
        if flags.cash_dip and wi == sc.cash_dip_week_index() and day == end_d:
            cash_paise = sc.cash_dip_balance_paise()

        adapter.add(
            ErpCashDaily(
                business_key=f"CASH-{day.isoformat()}",
                as_of_date=day,
                balance_paise=max(cash_paise, crore_to_paise(15)),
            )
        )

        # --- GST events ---
        # Minor ITC mismatches occasionally
        if is_weekday and rng.random() < 0.03:
            adapter.add(
                ErpGstEvent(
                    business_key=f"GST-2026-{gst_seq:05d}",
                    event_date=day,
                    event_type="itc_mismatch",
                    amount_paise=rupees_to_paise(float(rng.uniform(5e4, 3e5))),
                    status="open",
                    description="GSTR-2B ITC mismatch — review needed",
                )
            )
            gst_seq += 1

        # S2: show-cause in week 12 (first weekday of that week)
        if (
            flags.gst_notice
            and wi == sc.gst_notice_week_index()
            and day == week_monday(wid)
        ):
            adapter.add(
                ErpGstEvent(
                    business_key=f"GST-SCN-2026-{gst_seq:05d}",
                    event_date=day,
                    event_type="show_cause_notice",
                    amount_paise=sc.gst_notice_amount_paise(),
                    status="open",
                    description="Show-cause notice — alleged ITC irregularity ₹3.2 Cr",
                )
            )
            gst_seq += 1

        day += timedelta(days=1)

    adapter.flush()

    # Fix AP payments for pins that still have unpaid status from attr loss —
    # second pass: pay anything with due_date <= end and still open (except last 10 days keep some open)
    for pin in session.query(ErpPurchaseInvoice).filter_by(status="open").all():
        if pin.due_date <= end_d - timedelta(days=10):
            if stale_cutoff and pin.due_date > stale_cutoff:
                continue
            adapter.add(
                ErpPayment(
                    business_key=f"PAY-2026-{pay_seq:05d}",
                    purchase_invoice_id=pin.id,
                    payment_date=min(pin.due_date, end_d if not stale_cutoff else stale_cutoff),
                    amount_paise=pin.amount_paise,
                )
            )
            pay_seq += 1
            pin.status = "paid"
            # cash already approximated; skip adjusting historical cash

    adapter.flush()

    _generate_masters_and_gl(adapter, session, seed=seed, start=start, end_d=end_d)
    adapter.flush()

    counts = {
        "customers": session.query(ErpCustomer).count(),
        "sales_invoices": session.query(ErpSalesInvoice).count(),
        "receipts": session.query(ErpReceipt).count(),
        "purchase_invoices": session.query(ErpPurchaseInvoice).count(),
        "payments": session.query(ErpPayment).count(),
        "expenses": session.query(ErpExpense).count(),
        "cash_days": session.query(ErpCashDaily).count(),
        "revenue_days": session.query(ErpRevenueDaily).count(),
        "gst_events": session.query(ErpGstEvent).count(),
        "weeks": weeks,
        "start": start.isoformat(),
        "end": end_d.isoformat(),
        "seed": seed,
        "stale_days": stale_days,
        "scenarios": sc.scenario_catalog(),
        "scenario_flags": {
            sc.S1_DSO_DETERIORATION: flags.dso_drift,
            sc.S2_GST_SHOW_CAUSE: flags.gst_notice,
            sc.S3_CASH_DIP: flags.cash_dip,
            sc.S4_SEGMENT_GROWTH: flags.segment_growth,
            sc.S5_STALENESS: stale_days > 0,
        },
    }
    return counts


def _generate_masters_and_gl(adapter: SourceAdapter, session: Session, *, seed: int, start: date, end_d: date) -> None:
    """Entity + bank-account masters and a monthly general ledger summarised from the
    generated revenue and expenses. Uses its own RNG so the transactional data above
    is unchanged. Post-close adjustment lines land a few days after each period end,
    which is what the close-cycle metric measures (it stretches in the last month)."""
    from sqlalchemy import func

    from cfo_agent.models import ErpExpense as _Exp, ErpRevenueDaily as _Rev

    rng = np.random.default_rng(seed + 7919)
    adapter.add(ErpEntity(business_key="MAIN", name="Group operating company", functional_currency="INR"))
    adapter.add(ErpBankAccount(business_key="PRIMARY", entity="MAIN", bank_name="Operating bank",
                               label="Main current account", currency="INR"))
    # month ends inside the window
    months: list[date] = []
    d = date(start.year, start.month, 1)
    while d <= end_d:
        nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        months.append(nxt - timedelta(days=1))
        d = nxt
    seq = 1
    for i, pe in enumerate(months):
        ps = date(pe.year, pe.month, 1)
        rev = session.query(func.coalesce(func.sum(_Rev.amount_paise), 0)).filter(
            _Rev.as_of_date >= ps, _Rev.as_of_date <= pe).scalar()
        opex = session.query(func.coalesce(func.sum(_Exp.amount_paise), 0)).filter(
            _Exp.expense_date >= ps, _Exp.expense_date <= pe).scalar()
        if not rev:
            continue
        cogs = int(int(rev) * float(rng.uniform(0.58, 0.63)))
        lines = [
            ("4000", "Revenue from operations", "revenue", -int(rev), pe),
            ("5000", "Cost of materials consumed", "cogs", cogs, pe),
            ("6000", "Operating expenses", "opex", int(opex), pe),
        ]
        # Post-close accrual true-ups: 3-5 days after period end, 6-8 in the last month
        lag = int(rng.integers(6, 9)) if i == len(months) - 1 else int(rng.integers(3, 6))
        adj_day = pe + timedelta(days=lag)
        if adj_day <= end_d:
            lines.append(("6100", "Accrual true-up", "opex", int(int(opex) * 0.01), adj_day))
        for code, name, typ, amt, posted in lines:
            adapter.add(ErpGlEntry(
                business_key=f"GL-{pe.isoformat()}-{seq:04d}", entity="MAIN", posting_date=posted,
                period_end=pe, account_code=code, account_name=name, account_type=typ,
                amount_paise=amt, source_ref=f"JV-{pe:%Y%m}",
            ))
            seq += 1


# Re-export for imports
scenarios = sc
