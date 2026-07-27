from __future__ import annotations

import time
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from cfo_agent.connectors.catalog import CONNECTORS, ConnectorDef, get_connector
from cfo_agent.connectors.sync import get_erp_seed, run_connector_sync, scheduled_connector_sync
from cfo_agent.db import get_session
from cfo_agent.models import ConnectorConfig, ConnectorSyncRun
from cfo_agent.scheduler import _parse_cron, remove_job, upsert_job

router = APIRouter()


def _ensure_configs(session) -> None:
    existing = {row[0] for row in session.query(ConnectorConfig.id).all()}
    for c in CONNECTORS:
        if c.id not in existing:
            session.add(ConnectorConfig(id=c.id, enabled=False, config_json={}, secret=None))
    session.flush()


def _get_catalog_or_404(connector_id: str) -> ConnectorDef:
    try:
        return get_connector(connector_id)
    except KeyError:
        raise HTTPException(404, detail=f"Unknown connector {connector_id}") from None


def _mask_secret(secret: str | None) -> str | None:
    if not secret:
        return None
    tail = secret[-4:] if len(secret) >= 4 else secret
    return f"••••{tail}"


def _connector_view(catalog: ConnectorDef, cfg: ConnectorConfig) -> dict:
    return {
        "id": catalog.id,
        "vendor": catalog.vendor,
        "display_name": catalog.display_name,
        "auth_type": catalog.auth_type,
        "brand_initials": catalog.brand_initials,
        "brand_color": catalog.brand_color,
        "owned_tables": [m.__tablename__ for m in catalog.owned_models],
        "config_fields": [
            {"key": f.key, "label": f.label, "secret": f.secret} for f in catalog.config_fields
        ],
        "enabled": cfg.enabled,
        "config": cfg.config_json or {},
        "secret_set": bool(cfg.secret),
        "secret_masked": _mask_secret(cfg.secret),
        "cron": cfg.cron,
        "watermark_week": cfg.watermark_week,
        "last_run_at": cfg.last_run_at,
        "last_status": cfg.last_status,
    }


def _run_view(r: ConnectorSyncRun) -> dict:
    return {
        "id": r.id,
        "connector_id": r.connector_id,
        "trigger": r.trigger,
        "mode": r.mode,
        "status": r.status,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
        "rows_pulled": r.rows_pulled_json,
        "error": r.error,
    }


class ConnectorUpdateBody(BaseModel):
    enabled: bool | None = None
    config: dict[str, str] | None = None
    secret: str | None = None  # empty string / omitted preserves the stored value
    cron: str | None = None  # "" clears the schedule; omitted leaves it unchanged


@router.get("/connectors")
def list_connectors():
    session = get_session()
    try:
        _ensure_configs(session)
        session.commit()
        cfgs = {c.id: c for c in session.query(ConnectorConfig).all()}
        return {
            "connectors": [_connector_view(c, cfgs[c.id]) for c in CONNECTORS],
            "erp_seed": get_erp_seed(session),
        }
    finally:
        session.close()


@router.put("/connectors/{connector_id}")
def update_connector(connector_id: str, body: ConnectorUpdateBody):
    catalog = _get_catalog_or_404(connector_id)
    session = get_session()
    try:
        _ensure_configs(session)
        cfg = session.get(ConnectorConfig, connector_id)

        if body.cron is not None:
            if body.cron == "":
                cfg.cron = None
            else:
                try:
                    _parse_cron(body.cron)
                except ValueError:
                    raise HTTPException(422, detail=f"Invalid cron expression: {body.cron!r}") from None
                cfg.cron = body.cron

        if body.enabled is not None:
            cfg.enabled = body.enabled

        if body.config is not None:
            merged = dict(cfg.config_json or {})
            merged.update(body.config)
            cfg.config_json = merged

        if body.secret:  # falsy (None/"") preserves the stored secret
            cfg.secret = body.secret

        cfg.updated_at = datetime.utcnow()
        session.commit()

        job_id = f"connector:{connector_id}"
        if cfg.enabled and cfg.cron:
            upsert_job(job_id, lambda cid=connector_id: scheduled_connector_sync(cid), cfg.cron)
        else:
            remove_job(job_id)

        return _connector_view(catalog, cfg)
    except HTTPException:
        session.rollback()
        raise
    finally:
        session.close()


@router.post("/connectors/{connector_id}/test")
def test_connector(connector_id: str):
    catalog = _get_catalog_or_404(connector_id)
    session = get_session()
    try:
        _ensure_configs(session)
        cfg = session.get(ConnectorConfig, connector_id)
        session.commit()

        cfg_json = cfg.config_json or {}
        missing = [
            f.key
            for f in catalog.config_fields
            if (not cfg.secret if f.secret else not cfg_json.get(f.key))
        ]
        if missing:
            return {"ok": False, "connector_id": connector_id, "missing_fields": missing}
        # Simulated round-trip latency for the demo (no real network call yet)
        time.sleep(0.35)
        return {
            "ok": True,
            "connector_id": connector_id,
            "message": f"{catalog.display_name} connection check passed (simulated).",
            "latency_ms": 350,
        }
    finally:
        session.close()


@router.post("/connectors/{connector_id}/sync")
def sync_connector(connector_id: str, background_tasks: BackgroundTasks, mode: str = "incremental"):
    if mode not in ("incremental", "full"):
        raise HTTPException(422, detail="mode must be 'incremental' or 'full'")
    _get_catalog_or_404(connector_id)
    session = get_session()
    try:
        _ensure_configs(session)
        cfg = session.get(ConnectorConfig, connector_id)
        running = (
            session.query(ConnectorSyncRun)
            .filter_by(connector_id=connector_id, status="running")
            .first()
        )
        if running is not None:
            raise HTTPException(409, detail=f"Sync already running for {connector_id} (run {running.id})")

        run = ConnectorSyncRun(connector_id=connector_id, trigger="manual", mode=mode, status="running")
        session.add(run)
        cfg.last_status = "running"
        session.commit()
        run_id = run.id
        background_tasks.add_task(run_connector_sync, run_id)
        return {"run_id": run_id, "connector_id": connector_id, "mode": mode, "status": "running"}
    except HTTPException:
        session.rollback()
        raise
    finally:
        session.close()


def _run_sequential(run_ids: list[int]) -> None:
    for rid in run_ids:
        run_connector_sync(rid)


@router.post("/connectors/sync-all")
def sync_all_connectors(background_tasks: BackgroundTasks):
    session = get_session()
    try:
        _ensure_configs(session)
        enabled = session.query(ConnectorConfig).filter_by(enabled=True).all()
        run_ids: list[int] = []
        skipped: list[str] = []
        for cfg in enabled:
            running = (
                session.query(ConnectorSyncRun)
                .filter_by(connector_id=cfg.id, status="running")
                .first()
            )
            if running is not None:
                skipped.append(cfg.id)
                continue
            run = ConnectorSyncRun(connector_id=cfg.id, trigger="sync_all", mode="incremental", status="running")
            session.add(run)
            session.flush()
            cfg.last_status = "running"
            run_ids.append(run.id)
        session.commit()
        background_tasks.add_task(_run_sequential, run_ids)
        return {"run_ids": run_ids, "skipped": skipped}
    finally:
        session.close()


@router.get("/connectors/runs/{run_id}")
def get_connector_run(run_id: int):
    session = get_session()
    try:
        r = session.get(ConnectorSyncRun, run_id)
        if r is None:
            raise HTTPException(404, detail=f"Run {run_id} not found")
        return _run_view(r)
    finally:
        session.close()


@router.get("/connectors/{connector_id}/runs")
def list_connector_runs(connector_id: str, limit: int = 20):
    _get_catalog_or_404(connector_id)
    session = get_session()
    try:
        rows = (
            session.query(ConnectorSyncRun)
            .filter_by(connector_id=connector_id)
            .order_by(ConnectorSyncRun.id.desc())
            .limit(limit)
            .all()
        )
        return [_run_view(r) for r in rows]
    finally:
        session.close()
