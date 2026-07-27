from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from cfo_agent.api import api_router
from cfo_agent.db import init_db
from cfo_agent.kra import upsert_kra_version
from cfo_agent.memory.blocks import ensure_seed_blocks
from cfo_agent.scheduler import start_scheduler, upsert_job
from cfo_agent.settings import get_settings
from cfo_agent.weighting import seed_weighting_from_yaml

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _mount_frontend(app: FastAPI):
    try:
        dist = get_settings().frontend_dist
    except Exception:
        return
    if dist.is_dir() and (dist / "index.html").exists():
        assets = dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/{full_path:path}")
        def spa(full_path: str):
            if full_path.startswith("api"):
                return {"detail": "Not found"}
            candidate = dist / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")


def _recover_stale_runs(session) -> None:
    """Mark any ConnectorSyncRun / NewsRefreshRun still 'running' as interrupted."""
    from cfo_agent.models import ConnectorConfig, ConnectorSyncRun, NewsRefreshRun

    now = datetime.utcnow()
    for run in session.query(ConnectorSyncRun).filter_by(status="running").all():
        run.status = "error"
        run.error = "interrupted by restart"
        run.finished_at = now
        cfg = session.get(ConnectorConfig, run.connector_id)
        if cfg and cfg.last_status == "running":
            cfg.last_status = "error"
    for run in session.query(NewsRefreshRun).filter_by(status="running").all():
        run.status = "error"
        run.error = "interrupted by restart"
        run.finished_at = now
    session.flush()


def _register_connector_jobs(session) -> None:
    from cfo_agent.connectors.sync import scheduled_connector_sync
    from cfo_agent.models import ConnectorConfig

    for cfg in session.query(ConnectorConfig).filter_by(enabled=True).all():
        if cfg.cron:
            upsert_job(
                f"connector:{cfg.id}",
                lambda cid=cfg.id: scheduled_connector_sync(cid),
                cfg.cron,
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("Starting CFO Agent — model=%s", settings.anthropic_model)
    init_db()
    from cfo_agent.db import get_session

    session = get_session()
    try:
        upsert_kra_version(session)  # seed-only when table empty
        seed_weighting_from_yaml(session)
        ensure_seed_blocks(session)
        _recover_stale_runs(session)
        try:
            from cfo_agent.memory.archival import check_archival_consistency

            check_archival_consistency(session)
        except Exception as e:  # noqa: BLE001
            logger.warning("Memory consistency pass failed: %s", e)
        session.commit()
    finally:
        session.close()
    _mount_frontend(app)
    start_scheduler()
    # Register enabled connector cron jobs after scheduler starts
    session = get_session()
    try:
        _register_connector_jobs(session)
    except Exception as e:  # noqa: BLE001
        logger.warning("Connector job registration failed: %s", e)
    finally:
        session.close()
    yield


app = FastAPI(title="CFO Agentic Intelligence", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router)


@app.get("/api/health")
def health():
    return {"ok": True}
