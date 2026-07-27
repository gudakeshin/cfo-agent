"""Simulated ERP connector sync engine (WS1).

Each connector "pulls" from its own private, deterministic snapshot of the same
synthetic dataset the app uses (a scratch in-memory SQLite DB seeded via
``generate_erp``), then copies its owned rows into the real app DB. This lets
the demo show multi-source ingestion (SAP, Oracle, Dynamics, Salesforce,
Anaplan, SAP SAC) without needing real connector credentials.

# Concurrent /data/generate during connector sync could race — accepted for demo.
# Real connectors would watermark on modified-timestamps; synthetic uses week-monotonic data.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import create_engine, func
from sqlalchemy.orm import Session, sessionmaker

from cfo_agent.connectors.catalog import ConnectorDef, get_connector
from cfo_agent.db import get_session
from cfo_agent.models import (
    AppState,
    Base,
    ConnectorConfig,
    ConnectorSyncRun,
    ErpCashDaily,
    ErpCustomer,
    ErpExpense,
    ErpGstEvent,
    ErpPayment,
    ErpPlanMetric,
    ErpPurchaseInvoice,
    ErpReceipt,
    ErpRevenueDaily,
    ErpSalesInvoice,
)
from cfo_agent.synthetic.adapter import SubsetSourceAdapter
from cfo_agent.synthetic.generator import generate_erp
from cfo_agent.timeutil import iso_week_id, week_sunday

logger = logging.getLogger(__name__)

DEFAULT_ERP_SEED = 42

# FK relationships *within* the ERP model surface: child model -> [(fk column, parent model)]
FK_MAP: dict[type, list[tuple[str, type]]] = {
    ErpSalesInvoice: [("customer_id", ErpCustomer)],
    ErpReceipt: [("invoice_id", ErpSalesInvoice)],
    ErpPayment: [("purchase_invoice_id", ErpPurchaseInvoice)],
}

# Which date column identifies a row's ISO week, per model. Models without an
# entry here (ErpCustomer, ErpCovenant) are small, day-zero master/reference
# data with no date column — synced in full on first-ever run / full mode,
# and skipped on subsequent incremental runs (nothing "new" to pull).
ROW_DATE_FIELD: dict[type, str] = {
    ErpSalesInvoice: "invoice_date",
    ErpReceipt: "receipt_date",
    ErpPurchaseInvoice: "invoice_date",
    ErpPayment: "payment_date",
    ErpExpense: "expense_date",
    ErpCashDaily: "as_of_date",
    ErpRevenueDaily: "as_of_date",
    ErpGstEvent: "event_date",
}

# Deterministic plan/analytics factors applied to actuals aggregates — a stand-in
# for a real Anaplan/SAC model's budget vs. forecast logic.
PLAN_FACTORS: dict[tuple[str, str], float] = {
    ("anaplan", "plan"): 1.08,
    ("sap_sac", "analytics"): 0.97,
}


def row_week(row: Any) -> str | None:
    """ISO week id for a row, based on whichever date column its model has.

    Returns None for date-less master tables (ErpCustomer, ErpCovenant).
    """
    field = ROW_DATE_FIELD.get(type(row))
    if field is None:
        return None
    val = getattr(row, field, None)
    return iso_week_id(val) if val else None


def get_erp_seed(session: Session) -> int:
    row = session.get(AppState, "erp_seed")
    if row is None or row.value_json is None:
        return DEFAULT_ERP_SEED
    return int(row.value_json)


def _topo_sort_owned(models: list[type]) -> list[type]:
    """Order a connector's owned models so in-connector FK parents sync first."""
    ordered: list[type] = []
    seen: set[type] = set()

    def visit(m: type) -> None:
        if m in seen:
            return
        seen.add(m)
        for _fk_col, parent in FK_MAP.get(m, []):
            if parent in models:
                visit(parent)
        ordered.append(m)

    for m in models:
        visit(m)
    return ordered


def _has_external_referrer(model: type, owned_models: list[type]) -> bool:
    """True if some model NOT owned by this connector holds a live FK into ``model``."""
    for child, fks in FK_MAP.items():
        for _fk_col, parent in fks:
            if parent is model and child not in owned_models:
                return True
    return False


def _extract_fields(row: Any, model: type) -> dict[str, Any]:
    fk_cols = {fk_col for fk_col, _ in FK_MAP.get(model, [])}
    cols = [c for c in model.__table__.columns.keys() if c != "id" and c not in fk_cols]
    return {c: getattr(row, c) for c in cols}


def _get_or_provision(
    real_session: Session,
    scratch_session: Session,
    model: type,
    scratch_id: int,
    referenced_masters: dict[str, int],
) -> int:
    """Resolve a scratch-DB FK id to the corresponding real-DB row id by business_key,
    auto-provisioning a copy from the scratch snapshot if the row doesn't exist yet
    (e.g. a customer master row that its owning connector hasn't synced yet)."""
    scratch_row = scratch_session.get(model, scratch_id)
    real_row = real_session.query(model).filter_by(business_key=scratch_row.business_key).first()
    if real_row is not None:
        return real_row.id
    data = _extract_fields(scratch_row, model)
    for fk_col, parent_model in FK_MAP.get(model, []):
        parent_scratch_id = getattr(scratch_row, fk_col)
        data[fk_col] = _get_or_provision(
            real_session, scratch_session, parent_model, parent_scratch_id, referenced_masters
        )
    obj = model(**data)
    real_session.add(obj)
    real_session.flush()
    referenced_masters[model.__tablename__] = referenced_masters.get(model.__tablename__, 0) + 1
    return obj.id


def _upsert_by_business_key(real_session: Session, model: type, data: dict[str, Any]) -> tuple[Any, bool]:
    existing = real_session.query(model).filter_by(business_key=data["business_key"]).first()
    if existing is not None:
        for k, v in data.items():
            setattr(existing, k, v)
        return existing, False
    obj = model(**data)
    real_session.add(obj)
    return obj, True


def _rows_for_sync(
    scratch_session: Session, model: type, mode: str, watermark_week: str | None
) -> tuple[list[Any], list[Any]]:
    """Returns (rows_to_sync, all_scratch_rows_for_this_model)."""
    all_rows = scratch_session.query(model).all()
    full_pass = mode == "full" or watermark_week is None
    has_date = model in ROW_DATE_FIELD
    if full_pass:
        return all_rows, all_rows
    if not has_date:
        # Date-less master table already captured on an earlier full/first run.
        return [], all_rows
    rows = [r for r in all_rows if (wk := row_week(r)) and wk > watermark_week]
    return rows, all_rows


def _sync_model(
    real_session: Session,
    scratch_session: Session,
    model: type,
    mode: str,
    watermark_week: str | None,
    referenced_masters: dict[str, int],
    rows_pulled: dict[str, Any],
) -> set[str]:
    """Copy one owned model's rows scratch -> real DB. Returns the full set of
    business_keys present in the scratch snapshot (used for full-mode delete-absent)."""
    rows, all_rows = _rows_for_sync(scratch_session, model, mode, watermark_week)
    created = updated = 0
    for row in rows:
        data = _extract_fields(row, model)
        for fk_col, parent_model in FK_MAP.get(model, []):
            parent_scratch_id = getattr(row, fk_col)
            data[fk_col] = _get_or_provision(
                real_session, scratch_session, parent_model, parent_scratch_id, referenced_masters
            )
        _obj, is_new = _upsert_by_business_key(real_session, model, data)
        if is_new:
            created += 1
        else:
            updated += 1
    real_session.flush()
    rows_pulled[model.__tablename__] = {
        "scanned": len(rows),
        "created": created,
        "updated": updated,
    }
    return {r.business_key for r in all_rows}


def _sync_plan_metrics(
    real_session: Session,
    scratch_session: Session,
    connector_id: str,
    kind: str,
    mode: str,
    watermark_week: str | None,
    weeks: list[str],
) -> dict[str, Any]:
    """Derive plan/analytics rows for Anaplan/SAP SAC from scratch-DB aggregates.

    ErpPlanMetric has no scratch-DB counterpart (generate_erp never writes it) —
    these rows are computed here as actuals x a deterministic factor, the demo
    stand-in for a real EPM model's budget/forecast logic.
    """
    full_pass = mode == "full" or watermark_week is None
    sync_weeks = weeks if full_pass else [w for w in weeks if w > watermark_week]
    factor = PLAN_FACTORS[(connector_id, kind)]
    revenue_metric = "revenue_plan" if kind == "plan" else "revenue_analytics"
    cash_metric = "cash_plan" if kind == "plan" else "cash_analytics"

    created = updated = 0
    scratch_keys: set[str] = set()
    for week in sync_weeks:
        end = week_sunday(week)
        start = end - timedelta(days=6)
        seg_totals = (
            scratch_session.query(ErpRevenueDaily.segment, func.sum(ErpRevenueDaily.amount_paise))
            .filter(ErpRevenueDaily.as_of_date >= start, ErpRevenueDaily.as_of_date <= end)
            .group_by(ErpRevenueDaily.segment)
            .all()
        )
        for segment, total_paise in seg_totals:
            bk = f"{connector_id}-{revenue_metric}-{week}-{segment}"
            scratch_keys.add(bk)
            data = dict(
                business_key=bk,
                week=week,
                metric_id=revenue_metric,
                segment=segment,
                kind=kind,
                value=float(total_paise or 0) * factor,
                source_connector=connector_id,
            )
            _obj, is_new = _upsert_by_business_key(real_session, ErpPlanMetric, data)
            created += int(is_new)
            updated += int(not is_new)

        avg_cash = (
            scratch_session.query(func.avg(ErpCashDaily.balance_paise))
            .filter(ErpCashDaily.as_of_date >= start, ErpCashDaily.as_of_date <= end)
            .scalar()
        )
        if avg_cash is not None:
            bk = f"{connector_id}-{cash_metric}-{week}"
            scratch_keys.add(bk)
            data = dict(
                business_key=bk,
                week=week,
                metric_id=cash_metric,
                segment="",
                kind=kind,
                value=float(avg_cash) * factor,
                source_connector=connector_id,
            )
            _obj, is_new = _upsert_by_business_key(real_session, ErpPlanMetric, data)
            created += int(is_new)
            updated += int(not is_new)

    real_session.flush()

    if mode == "full":
        q = real_session.query(ErpPlanMetric).filter_by(source_connector=connector_id, kind=kind)
        keys = list(scratch_keys)
        deleted = q.filter(~ErpPlanMetric.business_key.in_(keys)).delete(synchronize_session=False) if keys else q.delete(synchronize_session=False)
        real_session.flush()
    else:
        deleted = 0

    return {
        "erp_plan_metrics": {
            "scanned": len(sync_weeks),
            "created": created,
            "updated": updated,
            "deleted": deleted,
        }
    }


def _sync_generic_tables(
    real_session: Session,
    scratch_session: Session,
    connector: ConnectorDef,
    mode: str,
    watermark_week: str | None,
) -> tuple[dict[str, Any], dict[str, int]]:
    ordered_models = _topo_sort_owned(connector.owned_models)
    rows_pulled: dict[str, Any] = {}
    referenced_masters: dict[str, int] = {}
    scratch_keys_by_model: dict[type, set[str]] = {}

    if mode == "full":
        # Wipe+reinsert is only safe for owned models nothing outside this
        # connector still holds a live FK into (see SubsetSourceAdapter docstring).
        safe_wipe_models = [m for m in ordered_models if not _has_external_referrer(m, connector.owned_models)]
        if safe_wipe_models:
            SubsetSourceAdapter(real_session, safe_wipe_models).wipe()
    else:
        safe_wipe_models = []

    for model in ordered_models:
        keys = _sync_model(real_session, scratch_session, model, mode, watermark_week, referenced_masters, rows_pulled)
        scratch_keys_by_model[model] = keys

    if mode == "full":
        for model in reversed(ordered_models):
            if model in safe_wipe_models:
                continue  # already wiped + fully reinserted above
            keys = list(scratch_keys_by_model.get(model, set()))
            q = real_session.query(model)
            deleted = q.filter(~model.business_key.in_(keys)).delete(synchronize_session=False) if keys else q.delete(synchronize_session=False)
            if deleted:
                rows_pulled.setdefault(model.__tablename__, {})["deleted"] = deleted
        real_session.flush()

    return rows_pulled, referenced_masters


def run_connector_sync(run_id: int) -> None:
    """Execute one connector sync run end-to-end: scratch generation, copy, watermark."""
    session = get_session()
    try:
        run = session.get(ConnectorSyncRun, run_id)
        if run is None:
            logger.warning("run_connector_sync: run %s not found", run_id)
            return
        cfg = session.get(ConnectorConfig, run.connector_id)
        if cfg is None:
            run.status = "error"
            run.error = f"No ConnectorConfig for {run.connector_id}"
            run.finished_at = datetime.utcnow()
            session.commit()
            return

        try:
            connector = get_connector(run.connector_id)
        except KeyError:
            run.status = "error"
            run.error = f"Unknown connector id {run.connector_id}"
            run.finished_at = datetime.utcnow()
            cfg.last_status = "error"
            session.commit()
            return

        try:
            base_seed = get_erp_seed(session)
            # A nonzero seed_offset deliberately desyncs this connector's data
            # from the live ERP dataset — a demo-only knob to show drift; do
            # not use in the base scenario walkthrough.
            seed_offset = int((cfg.config_json or {}).get("seed_offset", 0) or 0)
            seed = base_seed + seed_offset

            scratch_engine = create_engine("sqlite:///:memory:")
            Base.metadata.create_all(bind=scratch_engine)
            ScratchSession = sessionmaker(bind=scratch_engine, autoflush=False, autocommit=False)
            scratch_session = ScratchSession()
            try:
                meta = generate_erp(scratch_session, seed=seed)
                scratch_session.commit()

                mode = run.mode
                watermark_week = cfg.watermark_week

                if connector.plan_kind:
                    rows_pulled = _sync_plan_metrics(
                        session, scratch_session, connector.id, connector.plan_kind, mode, watermark_week, meta["weeks"]
                    )
                else:
                    rows_pulled, referenced_masters = _sync_generic_tables(
                        session, scratch_session, connector, mode, watermark_week
                    )
                    if referenced_masters:
                        rows_pulled["referenced_masters"] = referenced_masters

                max_week = meta["weeks"][-1]
                cfg.watermark_week = max_week
                cfg.last_run_at = datetime.utcnow()
                cfg.last_status = "ok"
                run.status = "ok"
                run.rows_pulled_json = rows_pulled
                run.finished_at = datetime.utcnow()
                session.commit()
            finally:
                scratch_session.close()
                scratch_engine.dispose()
        except Exception as e:  # noqa: BLE001
            session.rollback()
            run = session.get(ConnectorSyncRun, run_id)
            cfg = session.get(ConnectorConfig, run.connector_id) if run else None
            if run is not None:
                run.status = "error"
                run.error = str(e)
                run.finished_at = datetime.utcnow()
            if cfg is not None:
                cfg.last_status = "error"
            session.commit()
            logger.exception("Connector sync failed run_id=%s", run_id)
    finally:
        session.close()


def scheduled_connector_sync(connector_id: str) -> int | None:
    """Cron entry point — create a run (skip if one is already running) and execute it."""
    session = get_session()
    try:
        running = (
            session.query(ConnectorSyncRun)
            .filter_by(connector_id=connector_id, status="running")
            .first()
        )
        if running is not None:
            logger.info("Connector %s already syncing (run %s) — skip scheduled trigger", connector_id, running.id)
            return None
        cfg = session.get(ConnectorConfig, connector_id)
        if cfg is None or not cfg.enabled:
            logger.info("Connector %s not enabled — skip scheduled trigger", connector_id)
            return None
        run = ConnectorSyncRun(connector_id=connector_id, trigger="schedule", mode="incremental", status="running")
        session.add(run)
        cfg.last_status = "running"
        session.commit()
        run_id = run.id
    finally:
        session.close()
    run_connector_sync(run_id)
    return run_id
