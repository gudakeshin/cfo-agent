from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ─── ERP (generator write-only; pipeline/API read-only) ───────────────────────


class ErpCustomer(Base):
    __tablename__ = "erp_customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(256))
    segment: Mapped[str] = mapped_column(String(64))
    payment_terms_days: Mapped[int] = mapped_column(Integer, default=45)
    is_anchor: Mapped[bool] = mapped_column(Boolean, default=False)
    entity: Mapped[str] = mapped_column(String(32), default="MAIN", server_default="MAIN", index=True)
    is_intercompany: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class ErpSalesInvoice(Base):
    __tablename__ = "erp_sales_invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("erp_customers.id"))
    invoice_date: Mapped[date] = mapped_column(Date, index=True)
    due_date: Mapped[date] = mapped_column(Date)
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    segment: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="open")  # open|paid|partial
    entity: Mapped[str] = mapped_column(String(32), default="MAIN", server_default="MAIN", index=True)
    currency: Mapped[str] = mapped_column(String(3), default="INR", server_default="INR")
    # INR per unit of `currency` on the transaction date; amount_paise is always INR
    fx_rate: Mapped[float] = mapped_column(Float, default=1.0, server_default="1")


class ErpReceipt(Base):
    __tablename__ = "erp_receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("erp_sales_invoices.id"))
    receipt_date: Mapped[date] = mapped_column(Date, index=True)
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="INR", server_default="INR")
    # INR per unit of `currency` on the transaction date; amount_paise is always INR
    fx_rate: Mapped[float] = mapped_column(Float, default=1.0, server_default="1")


class ErpPurchaseInvoice(Base):
    __tablename__ = "erp_purchase_invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    vendor_name: Mapped[str] = mapped_column(String(256))
    invoice_date: Mapped[date] = mapped_column(Date, index=True)
    due_date: Mapped[date] = mapped_column(Date)
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32), default="open")
    entity: Mapped[str] = mapped_column(String(32), default="MAIN", server_default="MAIN", index=True)
    currency: Mapped[str] = mapped_column(String(3), default="INR", server_default="INR")
    # INR per unit of `currency` on the transaction date; amount_paise is always INR
    fx_rate: Mapped[float] = mapped_column(Float, default=1.0, server_default="1")
    is_intercompany: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class ErpPayment(Base):
    __tablename__ = "erp_payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    purchase_invoice_id: Mapped[int] = mapped_column(ForeignKey("erp_purchase_invoices.id"))
    payment_date: Mapped[date] = mapped_column(Date, index=True)
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="INR", server_default="INR")
    # INR per unit of `currency` on the transaction date; amount_paise is always INR
    fx_rate: Mapped[float] = mapped_column(Float, default=1.0, server_default="1")


class ErpExpense(Base):
    __tablename__ = "erp_expenses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expense_date: Mapped[date] = mapped_column(Date, index=True)
    cost_center: Mapped[str] = mapped_column(String(64), index=True)
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    category: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str] = mapped_column(String(32), default="MAIN", server_default="MAIN", index=True)
    currency: Mapped[str] = mapped_column(String(3), default="INR", server_default="INR")
    # INR per unit of `currency` on the transaction date; amount_paise is always INR
    fx_rate: Mapped[float] = mapped_column(Float, default=1.0, server_default="1")


class ErpCashDaily(Base):
    __tablename__ = "erp_cash_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    as_of_date: Mapped[date] = mapped_column(Date, index=True)
    # One row per bank account per day; balances are converted to INR on load
    account_key: Mapped[str] = mapped_column(String(64), default="PRIMARY", server_default="PRIMARY", index=True)
    balance_paise: Mapped[int] = mapped_column(BigInteger)

    __table_args__ = (UniqueConstraint("as_of_date", "account_key", name="uq_cash_day_account"),)


class ErpRevenueDaily(Base):
    __tablename__ = "erp_revenue_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    as_of_date: Mapped[date] = mapped_column(Date, index=True)
    segment: Mapped[str] = mapped_column(String(64), index=True)
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    entity: Mapped[str] = mapped_column(String(32), default="MAIN", server_default="MAIN", index=True)

    __table_args__ = (UniqueConstraint("as_of_date", "segment", "entity", name="uq_rev_day_seg_entity"),)


class ErpGstEvent(Base):
    __tablename__ = "erp_gst_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    event_date: Mapped[date] = mapped_column(Date, index=True)
    event_type: Mapped[str] = mapped_column(String(64))  # show_cause_notice|itc_mismatch|…
    amount_paise: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(32), default="open")
    description: Mapped[str] = mapped_column(Text, default="")
    entity: Mapped[str] = mapped_column(String(32), default="MAIN", server_default="MAIN", index=True)


class ErpCovenant(Base):
    __tablename__ = "erp_covenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    metric: Mapped[str] = mapped_column(String(64))
    threshold_paise: Mapped[int] = mapped_column(BigInteger)  # raw covenant (e.g. ₹20 Cr)
    buffered_threshold_paise: Mapped[int] = mapped_column(BigInteger)  # e.g. ₹22 Cr
    operator: Mapped[str] = mapped_column(String(8), default="gte")
    entity: Mapped[str] = mapped_column(String(32), default="MAIN", server_default="MAIN", index=True)


class ErpEntity(Base):
    """A legal entity in the group. Every figure is reported in INR (the group's
    reporting currency); `functional_currency` is informational."""

    __tablename__ = "erp_entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(256))
    functional_currency: Mapped[str] = mapped_column(String(3), default="INR")
    # False for dormant/holding entities kept out of the consolidated view
    consolidate: Mapped[bool] = mapped_column(Boolean, default=True)


class ErpBankAccount(Base):
    __tablename__ = "erp_bank_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    entity: Mapped[str] = mapped_column(String(32), default="MAIN", index=True)
    bank_name: Mapped[str] = mapped_column(String(128), default="")
    label: Mapped[str] = mapped_column(String(128), default="")
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    # Restricted cash (escrow, margin money, DSRA) is excluded from available cash
    is_restricted: Mapped[bool] = mapped_column(Boolean, default=False)


class ErpFxRate(Base):
    __tablename__ = "erp_fx_rates"
    __table_args__ = (UniqueConstraint("rate_date", "currency", name="uq_fx_day_ccy"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    rate_date: Mapped[date] = mapped_column(Date, index=True)
    currency: Mapped[str] = mapped_column(String(3), index=True)
    rate_to_inr: Mapped[float] = mapped_column(Float)


class ErpCreditNote(Base):
    """Credit notes issued against sales invoices; they reduce the open receivable."""

    __tablename__ = "erp_credit_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("erp_sales_invoices.id"), index=True)
    note_date: Mapped[date] = mapped_column(Date, index=True)
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(String(256), default="")
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    fx_rate: Mapped[float] = mapped_column(Float, default=1.0)


class ErpInventoryBalance(Base):
    """Inventory on hand, one row per category (raw materials, WIP, finished goods…) per
    snapshot date. The latest row on or before a date is the balance as of that date."""

    __tablename__ = "erp_inventory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    as_of_date: Mapped[date] = mapped_column(Date, index=True)
    category: Mapped[str] = mapped_column(String(64), default="total")
    entity: Mapped[str] = mapped_column(String(32), default="MAIN", index=True)
    value_paise: Mapped[int] = mapped_column(BigInteger)


class ErpDebtBalance(Base):
    """Borrowings by facility on a snapshot date. `principal_due_90d_paise` is the
    scheduled principal falling due in the following 90 days (for debt-service cover)."""

    __tablename__ = "erp_debt"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    as_of_date: Mapped[date] = mapped_column(Date, index=True)
    facility: Mapped[str] = mapped_column(String(64), index=True)
    lender: Mapped[str] = mapped_column(String(128), default="")
    entity: Mapped[str] = mapped_column(String(32), default="MAIN", index=True)
    outstanding_paise: Mapped[int] = mapped_column(BigInteger)
    interest_rate_pct: Mapped[float] = mapped_column(Float, default=0.0)
    principal_due_90d_paise: Mapped[int] = mapped_column(BigInteger, default=0)


class ErpGlEntry(Base):
    """General-ledger lines (trial-balance movements). amount_paise is signed:
    debit positive, credit negative. account_type drives declarative metrics."""

    __tablename__ = "erp_gl_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    entity: Mapped[str] = mapped_column(String(32), default="MAIN", index=True)
    posting_date: Mapped[date] = mapped_column(Date, index=True)
    # Accounting period the line belongs to (its period-end date); posting_date
    # after period_end is a post-close adjustment, which measures the close cycle.
    period_end: Mapped[date] = mapped_column(Date, index=True)
    account_code: Mapped[str] = mapped_column(String(32), index=True)
    account_name: Mapped[str] = mapped_column(String(128), default="")
    # revenue|cogs|opex|other_income|finance_cost|tax|asset|liability|equity
    account_type: Mapped[str] = mapped_column(String(32), index=True)
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    source_ref: Mapped[str] = mapped_column(String(128), default="")
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    fx_rate: Mapped[float] = mapped_column(Float, default=1.0)


class ErpPlanMetric(Base):
    """Display-only plan/analytics metrics from Anaplan / SAP SAC — not used by signals.py."""

    __tablename__ = "erp_plan_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    week: Mapped[str] = mapped_column(String(16), index=True)
    metric_id: Mapped[str] = mapped_column(String(64), index=True)
    segment: Mapped[str] = mapped_column(String(64), default="")
    kind: Mapped[str] = mapped_column(String(32))  # plan|analytics
    value: Mapped[float] = mapped_column(Float)
    source_connector: Mapped[str] = mapped_column(String(64), index=True)


# ─── Framework / weighting versions ───────────────────────────────────────────


class KraVersion(Base):
    __tablename__ = "kra_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[str] = mapped_column(String(32), unique=True)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    content_yaml: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WeightingVersion(Base):
    __tablename__ = "weighting_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(256))
    content_json: Mapped[dict] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ─── Signals / pipeline ───────────────────────────────────────────────────────


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    week: Mapped[str] = mapped_column(String(16), index=True)  # ISO 2026-W28
    metric_id: Mapped[str] = mapped_column(String(64), index=True)
    value: Mapped[float] = mapped_column(Float)
    baseline: Mapped[float | None] = mapped_column(Float, nullable=True)
    delta_wow: Mapped[float | None] = mapped_column(Float, nullable=True)
    severity: Mapped[float] = mapped_column(Float, default=1.0)
    kra_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    lineage_json: Mapped[list] = mapped_column(JSON, default=list)
    # Plan vs actual, covenant proximity, forecast and anomaly context (analytics.py)
    context_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"
    # At most one active run per week, enforced by the database so it holds across
    # API replicas (partial unique index; SQLite and PostgreSQL both support it).
    __table_args__ = (
        Index(
            "uq_pipeline_run_active_week",
            "week",
            unique=True,
            sqlite_where=text("status IN ('pending', 'running')"),
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    week: Mapped[str] = mapped_column(String(16), index=True)
    weighting_version_id: Mapped[int] = mapped_column(ForeignKey("weighting_versions.id"))
    kra_version_id: Mapped[int | None] = mapped_column(ForeignKey("kra_versions.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    # pending|running|completed|failed|data_unavailable
    current_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    step_timings_json: Mapped[dict] = mapped_column(JSON, default=dict)
    token_usage_json: Mapped[dict] = mapped_column(JSON, default=dict)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    suppressed_json: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_health_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    digest_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Digest(Base):
    __tablename__ = "digests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    week: Mapped[str] = mapped_column(String(16), index=True)
    weighting_version_id: Mapped[int] = mapped_column(ForeignKey("weighting_versions.id"))
    kra_version_id: Mapped[int | None] = mapped_column(ForeignKey("kra_versions.id"), nullable=True)
    pipeline_run_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_runs.id"), nullable=True)
    headline: Mapped[str] = mapped_column(Text, default="")
    weighting_label: Mapped[str] = mapped_column(String(256), default="")
    data_health_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    empty_bar: Mapped[bool] = mapped_column(Boolean, default=False)
    email_status: Mapped[str] = mapped_column(String(32), default="pending")
    # pending|sent|outbox|failed
    email_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    items: Mapped[list["DigestItem"]] = relationship(back_populates="digest")


class DigestItem(Base):
    __tablename__ = "digest_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    digest_id: Mapped[int] = mapped_column(ForeignKey("digests.id"), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(512))
    what: Mapped[str] = mapped_column(Text, default="")
    why: Mapped[str] = mapped_column(Text, default="")
    connects: Mapped[str] = mapped_column(Text, default="")
    action: Mapped[str] = mapped_column(Text, default="")
    kra_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    signal_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    citations_json: Mapped[dict] = mapped_column(JSON, default=dict)
    is_red_line: Mapped[bool] = mapped_column(Boolean, default=False)
    priority_score: Mapped[float] = mapped_column(Float, default=0.0)
    score_decomp_json: Mapped[dict] = mapped_column(JSON, default=dict)
    relationship_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    # Cited records frozen at generation time so audits stay reproducible
    evidence_snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # {"unverified_figures": [...], "fallback": bool, "notes": [...]}
    verification_json: Mapped[dict] = mapped_column(JSON, default=dict)

    digest: Mapped["Digest"] = relationship(back_populates="items")


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[str] = mapped_column(String(64), index=True)
    episode_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    week: Mapped[str] = mapped_column(String(16), index=True)
    as_of_date: Mapped[date] = mapped_column(Date)
    title: Mapped[str] = mapped_column(String(512))
    description: Mapped[str] = mapped_column(Text, default="")
    evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)
    synthesis_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    # active|acked|closed
    email_status: Mapped[str] = mapped_column(String(32), default="pending")
    email_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (UniqueConstraint("digest_item_id", name="uq_feedback_item"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    digest_item_id: Mapped[int] = mapped_column(ForeignKey("digest_items.id"), index=True)
    verdict: Mapped[str] = mapped_column(String(32))  # acted_on|useful|set_aside
    note: Mapped[str] = mapped_column(Text, default="")
    already_knew: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    digest_item_id: Mapped[int] = mapped_column(ForeignKey("digest_items.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user|assistant|tool
    content: Mapped[str] = mapped_column(Text)
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Assistant turns: citation IDs used, and the gate's verdict on the answer
    citations_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    verification_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Scoping groundwork — default global; full per-user requires identity (Phase-2)
    scope: Mapped[str] = mapped_column(String(64), default="global", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AskMessage(Base):
    """The CFO's own conversation about a whole briefing (not one insight). Private to
    the signed-in user: history is read and cleared per `user_email`."""

    __tablename__ = "ask_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    digest_id: Mapped[int | None] = mapped_column(ForeignKey("digests.id"), nullable=True, index=True)
    user_email: Mapped[str] = mapped_column(String(256), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user|assistant
    content: Mapped[str] = mapped_column(Text)
    citations_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    verification_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ─── Memory ───────────────────────────────────────────────────────────────────


class MemoryBlock(Base):
    __tablename__ = "memory_blocks"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[str] = mapped_column(Text, default="")
    char_limit: Mapped[int] = mapped_column(Integer, default=2000)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MemoryBlockRevision(Base):
    __tablename__ = "memory_block_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    block_name: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    edited_by: Mapped[str] = mapped_column(String(64))  # agent|human|seed
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("block_name", "version", name="uq_block_ver"),)


class ArchivalMemory(Base):
    __tablename__ = "archival_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    embedding: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    week: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    # Scoping groundwork — default global; full per-user requires identity (Phase-2)
    scope: Mapped[str] = mapped_column(String(64), default="global", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ConsolidationRun(Base):
    __tablename__ = "consolidation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime)
    window_end: Mapped[datetime] = mapped_column(DateTime)
    summary: Mapped[str] = mapped_column(Text, default="")
    edits_json: Mapped[list] = mapped_column(JSON, default=list)
    token_usage_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ─── ERP connectors (simulated sync) ──────────────────────────────────────────


class ConnectorConfig(Base):
    """Per-connector config. `secret` is encrypted at rest (enc:v1: Fernet token, key
    derived from SECRET_KEY — see cfo_agent/security.py); plaintext only in demo mode
    when no SECRET_KEY is set."""

    __tablename__ = "connector_configs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # catalog id
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    cron: Mapped[str | None] = mapped_column(String(64), nullable=True)
    watermark_week: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Extractor connectors (Tally, read-only SQL): opaque high-water mark such as a
    # Tally AlterID or the source's max modified timestamp
    watermark: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String(32), default="never_run")
    # never_run|ok|error|running
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ConnectorSyncRun(Base):
    __tablename__ = "connector_sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connector_id: Mapped[str] = mapped_column(String(64), index=True)
    trigger: Mapped[str] = mapped_column(String(32))  # manual|schedule|sync_all
    mode: Mapped[str] = mapped_column(String(32), default="incremental")  # full|incremental
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    # running|ok|error
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rows_pulled_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


# ─── Regulatory news ──────────────────────────────────────────────────────────


class NewsItem(Base):
    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String(1024), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(512))
    title_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    source_name: Mapped[str] = mapped_column(String(256), default="")
    published: Mapped[str | None] = mapped_column(String(64), nullable=True)
    category: Mapped[str] = mapped_column(String(64), default="other", index=True)
    relevance: Mapped[str] = mapped_column(String(32), default="medium")  # high|medium|low
    refresh_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("news_refresh_runs.id"), nullable=True
    )
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class NewsRefreshRun(Base):
    __tablename__ = "news_refresh_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger: Mapped[str] = mapped_column(String(32))  # manual|schedule|query
    query: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    # running|ok|error
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    items_found: Mapped[int] = mapped_column(Integer, default=0)
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    items_dropped: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AppState(Base):
    """Key-value store for replay clock, erp_seed, news_topics, misc runtime state."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[Any] = mapped_column(JSON)


class AuditEvent(Base):
    """Who did what, when: every change, every sign-in, every denied request and
    every view of a digest, insight conversation or evidence record."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    user_email: Mapped[str] = mapped_column(String(256), index=True)
    role: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(32), index=True)  # change|view|sign_in|sign_out|denied|sign_in_failed
    method: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(String(512))
    status: Mapped[int] = mapped_column(Integer)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ImportedFile(Base):
    """One file taken from a file-drop connector's inbox. The content hash makes
    imports idempotent: the same file dropped twice is never loaded twice."""

    __tablename__ = "imported_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connector_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    filename: Mapped[str] = mapped_column(String(256))
    table: Mapped[str] = mapped_column(String(64))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16))  # loaded|rejected|duplicate
    rows_inserted: Mapped[int] = mapped_column(Integer, default=0)
    rows_updated: Mapped[int] = mapped_column(Integer, default=0)
    errors_json: Mapped[list] = mapped_column(JSON, default=list)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class SchedulerLease(Base):
    """Single-row lease so that exactly one worker runs scheduled jobs, however many
    worker replicas are deployed. Renewed every minute; a lapsed lease is taken over."""

    __tablename__ = "scheduler_lease"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    job_runs_json: Mapped[dict] = mapped_column(JSON, default=dict)  # job id -> last run ISO time


class Job(Base):
    """A unit of background work waiting for, or being run by, a worker (see jobqueue.py).
    Lives in the tenant's own database, so a client's queue, history and limits are theirs."""

    __tablename__ = "jobs"
    __table_args__ = (
        # One active job per dedupe key: a slow sweep never piles up behind itself.
        Index(
            "uq_job_active_dedupe",
            "dedupe_key",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running') AND dedupe_key IS NOT NULL"),
            postgresql_where=text("status IN ('queued', 'running') AND dedupe_key IS NOT NULL"),
        ),
        Index("ix_jobs_claim", "status", "run_after"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # queued | running | succeeded | dead (failed on every attempt, or not retryable)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    dedupe_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class OpsAlert(Base):
    """An operator alert and its state, so a condition alerts once, repeats on a timer while it
    lasts, and says so when it clears (see ops_alerts.py)."""

    __tablename__ = "ops_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(96), index=True)
    severity: Mapped[str] = mapped_column(String(12))  # warn | critical
    title: Mapped[str] = mapped_column(String(256))
    detail: Mapped[str] = mapped_column(Text, default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notify_count: Mapped[int] = mapped_column(Integer, default=0)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class JobWorker(Base):
    """A running job executor, for the ops view ("is anything there to run my jobs?")."""

    __tablename__ = "job_workers"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    threads: Mapped[int] = mapped_column(Integer, default=1)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ─── Sharing, custom metrics, learning, actions, briefs ───────────────────────


class ShareLink(Base):
    """A read-only, expiring link to one digest for someone without an account
    (e.g. a board member). The URL carries a signed token naming this row; revoking
    the row kills the link even before it expires."""

    __tablename__ = "share_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    digest_id: Mapped[int] = mapped_column(ForeignKey("digests.id"), index=True)
    created_by: Mapped[str] = mapped_column(String(256))
    note: Mapped[str] = mapped_column(String(256), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    views: Mapped[int] = mapped_column(Integer, default=0)
    last_viewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MetricDefinition(Base):
    """A declarative metric (formula over ERP aggregates) authored without code.
    Each save is a new row; the latest active row per metric_id is in force."""

    __tablename__ = "metric_definitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    metric_id: Mapped[str] = mapped_column(String(64), index=True)
    definition_json: Mapped[dict] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_by: Mapped[str] = mapped_column(String(256), default="seed")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WeightingProposal(Base):
    """A change to the weighting learned from CFO feedback. Never applied until a
    delegate approves it; approval creates a normal weighting version."""

    __tablename__ = "weighting_proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    base_weighting_id: Mapped[int] = mapped_column(ForeignKey("weighting_versions.id"))
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # pending|approved|rejected|superseded
    multipliers_json: Mapped[dict] = mapped_column(JSON, default=dict)  # kra_id -> multiplier
    evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)  # kra_id -> counts, rate
    decided_by: Mapped[str | None] = mapped_column(String(256), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    applied_weighting_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ActionItem(Base):
    """Follow-up the CFO's office committed to, usually from an insight."""

    __tablename__ = "action_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    digest_item_id: Mapped[int | None] = mapped_column(ForeignKey("digest_items.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(512))
    detail: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(256), default="")
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    # open|in_progress|done|dropped
    kra_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    metric_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MeetingBrief(Base):
    """A one-page, citation-checked brief prepared for a specific meeting."""

    __tablename__ = "meeting_briefs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(256))
    meeting_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    audience: Mapped[str] = mapped_column(String(256), default="")
    week: Mapped[str] = mapped_column(String(16))
    request_json: Mapped[dict] = mapped_column(JSON, default=dict)
    content_json: Mapped[dict] = mapped_column(JSON, default=dict)
    verification_json: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence_snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
