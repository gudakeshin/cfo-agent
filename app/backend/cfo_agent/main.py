from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from cfo_agent.api import api_router
from cfo_agent.db import init_db
from cfo_agent.kra import upsert_kra_version
from cfo_agent.memory.blocks import ensure_seed_blocks
from cfo_agent.scheduler import start_scheduler, upsert_job
from cfo_agent.settings import get_settings
from cfo_agent.weighting import seed_weighting_from_yaml

from cfo_agent.observability import configure_logging, new_request_id, request_id_var

configure_logging()
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

        root = dist.resolve()

        @app.get("/{full_path:path}")
        def spa(full_path: str):
            if full_path.startswith("api"):
                return JSONResponse({"detail": "Not found"}, status_code=404)
            if full_path:
                candidate = (root / full_path).resolve()
                # Never serve anything outside the built UI folder (path traversal).
                if candidate.is_relative_to(root) and candidate.is_file():
                    return FileResponse(candidate)
            return FileResponse(root / "index.html")


STALE_AFTER = timedelta(minutes=30)


def _recover_stale_runs(session, *, older_than: timedelta = STALE_AFTER) -> int:
    """Mark runs stuck in 'running'/'pending' for longer than `older_than` as
    interrupted, so their week is not blocked forever. The age threshold keeps a
    restarting worker from failing a run another API replica is still executing."""
    from cfo_agent.models import ConnectorConfig, ConnectorSyncRun, NewsRefreshRun, PipelineRun

    now = datetime.utcnow()
    cutoff = now - older_than
    n = 0
    for run in (
        session.query(PipelineRun)
        .filter(PipelineRun.status.in_(["pending", "running"]), PipelineRun.created_at < cutoff)
        .all()
    ):
        run.status = "failed"
        run.error = "interrupted (no progress for 30 minutes)"
        run.completed_at = now
        n += 1
    for run in (
        session.query(ConnectorSyncRun)
        .filter(ConnectorSyncRun.status == "running", ConnectorSyncRun.started_at < cutoff)
        .all()
    ):
        run.status = "error"
        run.error = "interrupted (no progress for 30 minutes)"
        run.finished_at = now
        cfg = session.get(ConnectorConfig, run.connector_id)
        if cfg and cfg.last_status == "running":
            cfg.last_status = "error"
        n += 1
    for run in (
        session.query(NewsRefreshRun)
        .filter(NewsRefreshRun.status == "running", NewsRefreshRun.started_at < cutoff)
        .all()
    ):
        run.status = "error"
        run.error = "interrupted (no progress for 30 minutes)"
        run.finished_at = now
        n += 1
    session.flush()
    return n


def job_recover_stale_runs() -> None:
    from cfo_agent.db import get_session

    session = get_session()
    try:
        n = _recover_stale_runs(session)
        session.commit()
        if n:
            logger.warning("Recovered %d stale run(s)", n)
    finally:
        session.close()


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
    from cfo_agent.security import encrypt_plaintext_secrets, validate_deployment_settings

    problems = validate_deployment_settings()
    if problems:
        raise RuntimeError("Unsafe configuration:\n- " + "\n- ".join(problems))
    logger.info(
        "Starting CFO Agent — model=%s demo_mode=%s auth=%s scheduler=%s",
        settings.anthropic_model,
        settings.demo_mode,
        settings.auth_mode,
        settings.run_scheduler,
    )
    init_db()
    from cfo_agent.db import get_session
    from cfo_agent.tenancy import for_each_tenant

    def _startup_one():
        session = get_session()
        try:
            upsert_kra_version(session)  # seed-only when table empty
            seed_weighting_from_yaml(session)
            ensure_seed_blocks(session)
            if settings.run_scheduler:
                # Only the process that runs jobs may declare in-flight runs dead.
                _recover_stale_runs(session)
            encrypt_plaintext_secrets(session)
            try:
                from cfo_agent.memory.archival import check_archival_consistency

                check_archival_consistency(session)
            except Exception as e:  # noqa: BLE001
                logger.warning("Memory consistency pass failed: %s", e)
            session.commit()
        finally:
            session.close()

    for_each_tenant(_startup_one)
    _mount_frontend(app)
    if settings.run_scheduler:
        start_scheduler()

        # Register enabled connector cron jobs after scheduler starts
        def _register_one():
            session = get_session()
            try:
                _register_connector_jobs(session)
            except Exception as e:  # noqa: BLE001
                logger.warning("Connector job registration failed: %s", e)
            finally:
                session.close()

        for_each_tenant(_register_one)
    yield


app = FastAPI(title="CFO Agentic Intelligence", version="0.1.0", lifespan=lifespan)

_origins = get_settings().cors_origin_list
if _origins:
    # Explicit allow-list only; never "*" together with credentials.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "X-Requested-With"],
    )


if get_settings().auth_mode == "oidc":
    from starlette.middleware.sessions import SessionMiddleware

    # Holds only the OIDC state/nonce during the sign-in round trip.
    app.add_middleware(
        SessionMiddleware,
        secret_key=get_settings().secret_key or "",
        session_cookie="cfo_oidc",
        same_site="lax",
        https_only=get_settings().public_base_url.startswith("https://"),
        max_age=600,
    )


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Request ID, access log line and audit trail for every API request."""
    from cfo_agent.audit import classify, record_event
    from cfo_agent.auth import current_user

    rid = new_request_id(request.headers.get("x-request-id"))
    request.state.request_id = rid
    token = request_id_var.set(rid)
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["X-Request-ID"] = rid
    path = request.url.path
    if path.startswith("/api/"):
        ms = int((time.perf_counter() - t0) * 1000)
        logging.getLogger("cfo_agent.access").info(
            "%s %s %s %dms [%s]", request.method, path, response.status_code, ms, rid
        )
        action = classify(request.method, path, response.status_code)
        if action:
            user = getattr(request.state, "user", None) or current_user(request)
            record_event(request, action=action, status=response.status_code, user=user)
    return response


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'",
    )
    return response


app.include_router(api_router)


@app.get("/api/health")
def health():
    return {"ok": True}


# Registered last so it is the outermost middleware: audit, auth and request-ID
# middleware all run inside the tenant's context.
@app.middleware("http")
async def route_tenant(request, call_next):
    """Multi-tenant mode: pick the tenant (its database) from the Host header."""
    from cfo_agent.tenancy import is_multi_tenant, tenant_context, tenant_for_host

    if not is_multi_tenant():
        return await call_next(request)
    tenant = tenant_for_host(request.headers.get("host"))
    if tenant is None:
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": "Unknown host"}, status_code=404)
    with tenant_context(tenant.id):
        return await call_next(request)
