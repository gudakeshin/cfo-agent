from __future__ import annotations

from fastapi import APIRouter

from cfo_agent.api import (
    alerts,
    chat,
    citations,
    connectors,
    data,
    digests,
    feedback,
    kra,
    memory,
    news,
    runs,
    weighting,
)

api_router = APIRouter(prefix="/api")
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
