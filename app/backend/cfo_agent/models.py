from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
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


class ErpSalesInvoice(Base):
    __tablename__ = "erp_sales_invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("erp_customers.id"))
    invoice_date: Mapped[date] = mapped_column(Date, index=True)
    due_date: Mapped[date] = mapped_column(Date)
    amount_paise: Mapped[int] = mapped_column(Integer)
    segment: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="open")  # open|paid|partial


class ErpReceipt(Base):
    __tablename__ = "erp_receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("erp_sales_invoices.id"))
    receipt_date: Mapped[date] = mapped_column(Date, index=True)
    amount_paise: Mapped[int] = mapped_column(Integer)


class ErpPurchaseInvoice(Base):
    __tablename__ = "erp_purchase_invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    vendor_name: Mapped[str] = mapped_column(String(256))
    invoice_date: Mapped[date] = mapped_column(Date, index=True)
    due_date: Mapped[date] = mapped_column(Date)
    amount_paise: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="open")


class ErpPayment(Base):
    __tablename__ = "erp_payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    purchase_invoice_id: Mapped[int] = mapped_column(ForeignKey("erp_purchase_invoices.id"))
    payment_date: Mapped[date] = mapped_column(Date, index=True)
    amount_paise: Mapped[int] = mapped_column(Integer)


class ErpExpense(Base):
    __tablename__ = "erp_expenses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expense_date: Mapped[date] = mapped_column(Date, index=True)
    cost_center: Mapped[str] = mapped_column(String(64), index=True)
    amount_paise: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(64))


class ErpCashDaily(Base):
    __tablename__ = "erp_cash_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    as_of_date: Mapped[date] = mapped_column(Date, unique=True, index=True)
    balance_paise: Mapped[int] = mapped_column(Integer)


class ErpRevenueDaily(Base):
    __tablename__ = "erp_revenue_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    as_of_date: Mapped[date] = mapped_column(Date, index=True)
    segment: Mapped[str] = mapped_column(String(64), index=True)
    amount_paise: Mapped[int] = mapped_column(Integer)

    __table_args__ = (UniqueConstraint("as_of_date", "segment", name="uq_rev_day_seg"),)


class ErpGstEvent(Base):
    __tablename__ = "erp_gst_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    event_date: Mapped[date] = mapped_column(Date, index=True)
    event_type: Mapped[str] = mapped_column(String(64))  # show_cause_notice|itc_mismatch|…
    amount_paise: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="open")
    description: Mapped[str] = mapped_column(Text, default="")


class ErpCovenant(Base):
    __tablename__ = "erp_covenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    metric: Mapped[str] = mapped_column(String(64))
    threshold_paise: Mapped[int] = mapped_column(Integer)  # raw covenant (e.g. ₹20 Cr)
    buffered_threshold_paise: Mapped[int] = mapped_column(Integer)  # e.g. ₹22 Cr
    operator: Mapped[str] = mapped_column(String(8), default="gte")


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

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
    # Scoping groundwork — default global; full per-user requires identity (Phase-2)
    scope: Mapped[str] = mapped_column(String(64), default="global", index=True)
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
    """Per-connector config. Secrets stored plaintext in local SQLite (demo-grade);
    production needs a vault/KMS — see memory-audit.md ops notes."""

    __tablename__ = "connector_configs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # catalog id
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    cron: Mapped[str | None] = mapped_column(String(64), nullable=True)
    watermark_week: Mapped[str | None] = mapped_column(String(16), nullable=True)
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
