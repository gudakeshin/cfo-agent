"""Scenario injectors for the synthetic ERP quarter.

Named story lines (demo script / plan S1–S5):

| ID | Story | Mechanism |
|----|-------|-----------|
| S1 | DSO deterioration | Anchor payment lag 45→75 days from week 7 |
| S2 | GST show-cause notice | ₹3.2 Cr notice lands week 12 |
| S3 | Cash covenant early warning | Cash dips to ₹21.4 Cr in week 13 (< ₹22 Cr buffer) |
| S4 | Segment growth | Industrial +18% QoQ (weightings rank differently) |
| S5 | Injectable staleness | ``stale_days`` truncates recent receipts |
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

# Stable scenario IDs for docs, tests, and demo scripting
S1_DSO_DETERIORATION: Final = "S1"
S2_GST_SHOW_CAUSE: Final = "S2"
S3_CASH_DIP: Final = "S3"
S4_SEGMENT_GROWTH: Final = "S4"
S5_STALENESS: Final = "S5"

SCENARIO_IDS: Final = (
    S1_DSO_DETERIORATION,
    S2_GST_SHOW_CAUSE,
    S3_CASH_DIP,
    S4_SEGMENT_GROWTH,
    S5_STALENESS,
)


@dataclass
class ScenarioFlags:
    """Toggle story lines S1–S4. S5 is controlled by ``stale_days`` on generate."""

    dso_drift: bool = True  # S1
    gst_notice: bool = True  # S2
    cash_dip: bool = True  # S3
    segment_growth: bool = True  # S4


def scenario_catalog() -> dict[str, str]:
    """Human-readable map of scenario IDs → short descriptions."""
    return {
        S1_DSO_DETERIORATION: "DSO deterioration: anchor lag 45→75 days from week 7",
        S2_GST_SHOW_CAUSE: "GST show-cause notice ₹3.2 Cr in week 12",
        S3_CASH_DIP: "Cash dips to ₹21.4 Cr in week 13 (buffered covenant ₹22 Cr)",
        S4_SEGMENT_GROWTH: "Industrial segment +18% QoQ",
        S5_STALENESS: "Injectable staleness via stale_days (truncates recent receipts)",
    }


def anchor_payment_lag_days(week_index: int, *, enabled: bool = True) -> int:
    """S1: stretch anchor payment lag from ~45 to ~75 days starting week index 6."""
    if not enabled:
        return 45
    if week_index < 6:
        return 45
    progress = (week_index - 6) / 6.0
    return int(round(45 + progress * 30))


def gst_notice_week_index() -> int:
    """S2: zero-based week index for the GST show-cause notice."""
    return 11


def gst_notice_amount_paise() -> int:
    """S2: ₹3.2 Cr in paise."""
    return int(3.2 * 100 * 10_000_000)


def cash_dip_week_index() -> int:
    """S3: zero-based week index for the cash dip."""
    return 12


def cash_dip_balance_paise() -> int:
    """S3: ₹21.4 Cr in paise — breaches ₹22 Cr buffer, not ₹20 Cr raw covenant."""
    return int(21.4 * 100 * 10_000_000)


def growth_segment() -> str:
    """S4: segment that receives the QoQ growth boost."""
    return "Industrial"


def growth_segment_qoq_boost() -> float:
    """S4: ~+18% QoQ multiplier at full ramp."""
    return 1.18


def apply_staleness_cutoff(end_date: date, stale_days: int) -> date | None:
    """S5: truncate activity after end_date − stale_days when stale_days > 0."""
    if stale_days <= 0:
        return None
    return end_date - timedelta(days=stale_days)
