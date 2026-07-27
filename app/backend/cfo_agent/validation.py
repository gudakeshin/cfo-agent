"""Feed freshness / integrity → DataHealth."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from cfo_agent.models import ErpCashDaily, ErpReceipt, ErpSalesInvoice
from cfo_agent.timeutil import today_ist, week_sunday


@dataclass
class DataHealth:
    ok: bool
    hard_failure: bool = False
    stale: bool = False
    stale_days: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    latest_receipt_date: str | None = None
    latest_cash_date: str | None = None
    invoice_count: int = 0
    as_of_week: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def check_data_health(
    session: Session,
    *,
    week: str | None = None,
    expected_end: date | None = None,
    stale_threshold_days: int = 3,
) -> DataHealth:
    """Validate ERP feed. Hard failures abort pipeline; staleness warns."""
    inv_count = session.query(ErpSalesInvoice).count()
    if inv_count == 0:
        return DataHealth(
            ok=False,
            hard_failure=True,
            errors=["No ERP data present — run data generate / seed.py"],
            invoice_count=0,
            as_of_week=week,
        )

    latest_receipt = session.query(func.max(ErpReceipt.receipt_date)).scalar()
    latest_cash = session.query(func.max(ErpCashDaily.as_of_date)).scalar()

    errors: list[str] = []
    warnings: list[str] = []
    hard = False
    stale = False
    stale_days = 0

    if latest_cash is None:
        errors.append("No cash daily records")
        hard = True

    ref = expected_end or (week_sunday(week) if week else today_ist())
    if latest_receipt is not None:
        gap = (ref - latest_receipt).days
        if gap > stale_threshold_days:
            stale = True
            stale_days = gap
            warnings.append(
                f"Receipts appear stale: latest {latest_receipt.isoformat()}, "
                f"{gap} days behind reference {ref.isoformat()}"
            )
    else:
        warnings.append("No receipts found")
        stale = True

    if latest_cash is not None and week:
        week_end = week_sunday(week)
        if latest_cash < week_end - timedelta(days=2):
            # For historical weeks this is fine if cash exists through week end
            if latest_cash < week_end:
                # Check cash exists on week end
                on_end = (
                    session.query(ErpCashDaily)
                    .filter(ErpCashDaily.as_of_date == week_end)
                    .first()
                )
                if not on_end:
                    warnings.append(f"No cash balance on week end {week_end.isoformat()}")

    ok = not hard
    return DataHealth(
        ok=ok,
        hard_failure=hard,
        stale=stale,
        stale_days=stale_days,
        warnings=warnings,
        errors=errors,
        latest_receipt_date=latest_receipt.isoformat() if latest_receipt else None,
        latest_cash_date=latest_cash.isoformat() if latest_cash else None,
        invoice_count=inv_count,
        as_of_week=week,
    )
