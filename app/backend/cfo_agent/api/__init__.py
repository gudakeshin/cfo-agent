from __future__ import annotations

from fastapi import APIRouter, Depends

from cfo_agent.auth import authorize

from cfo_agent.api import (
    alerts,
    audit,
    auth,
    chat,
    citations,
    connectors,
    data,
    digests,
    feedback,
    insights,
    kra,
    memory,
    metric_defs,
    news,
    ops,
    runs,
    sharing,
    weighting,
    workflow,
)

# Every /api route passes the central permission policy in cfo_agent.auth.
api_router = APIRouter(prefix="/api", dependencies=[Depends(authorize)])
api_router.include_router(auth.router, tags=["auth"])
api_router.include_router(audit.router, tags=["audit"])
api_router.include_router(data.router, tags=["data"])
api_router.include_router(kra.router, tags=["kra"])
api_router.include_router(weighting.router, tags=["weighting"])
api_router.include_router(digests.router, tags=["digests"])
api_router.include_router(alerts.router, tags=["alerts"])
api_router.include_router(feedback.router, tags=["feedback"])
api_router.include_router(chat.router, tags=["chat"])
api_router.include_router(citations.router, tags=["citations"])
api_router.include_router(memory.router, tags=["memory"])
api_router.include_router(runs.router, tags=["runs"])
api_router.include_router(news.router, tags=["news"])
api_router.include_router(connectors.router, tags=["connectors"])
api_router.include_router(insights.router, tags=["insights"])
api_router.include_router(ops.router, tags=["ops"])
api_router.include_router(sharing.router, tags=["sharing"])
api_router.include_router(metric_defs.router, tags=["metrics"])
api_router.include_router(workflow.router, tags=["workflow"])
