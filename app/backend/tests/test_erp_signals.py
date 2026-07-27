"""Synthetic ERP + signals as-of-week tests."""

from datetime import timedelta

from cfo_agent.models import ErpCashDaily, ErpGstEvent, ErpReceipt, ErpSalesInvoice
from cfo_agent.signals import compute_all_signals, compute_cash, compute_dso
from cfo_agent.synthetic.generator import generate_erp
from cfo_agent.timeutil import crore_to_paise, paise_to_crore, week_sunday
from cfo_agent.validation import check_data_health


def test_seed42_reproducible(db_session):
    m1 = generate_erp(db_session, seed=42)
    db_session.commit()
    c1 = db_session.query(ErpSalesInvoice).count()
    r1 = db_session.query(ErpReceipt).count()

    m2 = generate_erp(db_session, seed=42)
    db_session.commit()
    c2 = db_session.query(ErpSalesInvoice).count()
    r2 = db_session.query(ErpReceipt).count()

    assert c1 == c2 == m1["sales_invoices"] == m2["sales_invoices"]
    assert r1 == r2
    assert m1["weeks"] == m2["weeks"]


def test_dso_drift(seeded_erp, db_session):
    weeks = seeded_erp["weeks"]
    early = weeks[6]  # week 7 — drift begins
    late = weeks[-1]
    compute_all_signals(db_session, early)
    db_session.commit()
    dso_early = compute_dso(db_session, early).value
    compute_all_signals(db_session, late)
    db_session.commit()
    dso_late = compute_dso(db_session, late).value
    assert dso_late - dso_early >= 8, f"DSO drift {dso_early} → {dso_late}"


def test_cash_min_below_22cr(seeded_erp, db_session):
    weeks = seeded_erp["weeks"]
    cash = compute_cash(db_session, weeks[-1])
    assert cash.value < 22.0, cash.value
    # Also check raw daily min
    end = week_sunday(weeks[-1])
    row = (
        db_session.query(ErpCashDaily)
        .filter(ErpCashDaily.as_of_date == end)
        .one()
    )
    assert paise_to_crore(row.balance_paise) < 22.0


def test_gst_notice_week12(seeded_erp, db_session):
    weeks = seeded_erp["weeks"]
    w12 = weeks[11]
    end = week_sunday(w12)
    start = end - timedelta(days=6)
    notices = (
        db_session.query(ErpGstEvent)
        .filter(
            ErpGstEvent.event_type == "show_cause_notice",
            ErpGstEvent.event_date >= start,
            ErpGstEvent.event_date <= end,
        )
        .all()
    )
    assert len(notices) >= 1
    assert notices[0].amount_paise == crore_to_paise(3.2)


def test_staleness_flips_health(db_session):
    generate_erp(db_session, seed=42, stale_days=5)
    db_session.commit()
    weeks = db_session.execute  # noqa — use from meta
    from cfo_agent.models import ErpCashDaily
    from cfo_agent.timeutil import iso_week_id

    end = db_session.query(ErpCashDaily.as_of_date).order_by(ErpCashDaily.as_of_date.desc()).limit(1).scalar()
    week = iso_week_id(end)
    health = check_data_health(db_session, week=week, expected_end=end, stale_threshold_days=3)
    assert health.stale is True
    assert health.ok is True  # stale is warning, not hard failure


def test_as_of_excludes_future(seeded_erp, db_session):
    weeks = seeded_erp["weeks"]
    w_early = weeks[5]
    w_late = weeks[6]
    end_early = week_sunday(w_early)

    # Count invoices as of early week vs all
    early_inv = (
        db_session.query(ErpSalesInvoice)
        .filter(ErpSalesInvoice.invoice_date <= end_early)
        .count()
    )
    all_inv = db_session.query(ErpSalesInvoice).count()
    assert early_inv < all_inv

    # Signals for early week should not cite late invoices
    sigs = compute_all_signals(db_session, w_early)
    db_session.commit()
    for s in sigs:
        for cid in s.lineage_json:
            if cid.startswith("erp:erp_sales_invoices:"):
                bk = cid.split(":")[-1]
                inv = (
                    db_session.query(ErpSalesInvoice)
                    .filter_by(business_key=bk)
                    .one()
                )
                assert inv.invoice_date <= end_early


def test_every_grounded_face_has_signal(seeded_erp, db_session):
    weeks = seeded_erp["weeks"]
    sigs = compute_all_signals(db_session, weeks[-1])
    db_session.commit()
    faces = set()
    from cfo_agent.kra import get_active_framework

    fw = get_active_framework()
    for s in sigs:
        for kid in s.kra_ids_json:
            faces.add(fw.kras[kid].face)
    assert "steward" in faces
    assert "operator" in faces
    assert "strategist" in faces
    assert "catalyst" in faces
