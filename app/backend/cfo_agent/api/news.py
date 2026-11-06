from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from cfo_agent.db import get_session
from cfo_agent.models import NewsItem, NewsRefreshRun
from cfo_agent.news import (
    DEFAULT_TOPICS,
    get_topics,
    queue_refresh_run,
    run_news_refresh,
    set_topics,
)

router = APIRouter()


class RefreshBody(BaseModel):
    query: str | None = None


class TopicsBody(BaseModel):
    topics: list[str]


def _news_dict(n: NewsItem) -> dict:
    return {
        "id": n.id,
        "url": n.url,
        "title": n.title,
        "summary": n.summary,
        "source_name": n.source_name,
        "published": n.published,
        "category": n.category,
        "relevance": n.relevance,
        "fetched_at": n.fetched_at,
    }


def _run_dict(r: NewsRefreshRun) -> dict:
    return {
        "id": r.id,
        "trigger": r.trigger,
        "query": r.query,
        "status": r.status,
        "items_found": r.items_found,
        "items_new": r.items_new,
        "items_dropped": r.items_dropped,
        "error": r.error,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
    }


@router.get("/news")
def list_news(category: str | None = None, since: str | None = None, limit: int = 50):
    session = get_session()
    try:
        q = session.query(NewsItem)
        if category:
            q = q.filter(NewsItem.category == category)
        if since:
            try:
                since_dt = datetime.fromisoformat(since)
            except ValueError as e:
                raise HTTPException(422, f"Bad 'since' — expected ISO datetime: {e}") from e
            q = q.filter(NewsItem.fetched_at >= since_dt)
        rows = q.order_by(NewsItem.fetched_at.desc()).limit(max(1, min(limit, 500))).all()
        return [_news_dict(n) for n in rows]
    finally:
        session.close()


@router.get("/news/status")
def news_status():
    from cfo_agent.news import news_disabled_reason

    session = get_session()
    try:
        latest = session.query(NewsRefreshRun).order_by(NewsRefreshRun.id.desc()).first()
        running = session.query(NewsRefreshRun).filter_by(status="running").first()
        total_items = session.query(NewsItem).count()
        return {
            "running": running is not None,
            "running_run_id": running.id if running else None,
            "total_items": total_items,
            "latest_run": _run_dict(latest) if latest else None,
            "disabled_reason": news_disabled_reason(),
        }
    finally:
        session.close()


@router.post("/news/refresh")
def refresh_news(body: RefreshBody | None, background_tasks: BackgroundTasks):
    from cfo_agent.news import news_disabled_reason

    reason = news_disabled_reason()
    if reason:
        raise HTTPException(409, detail=reason)
    session = get_session()
    try:
        running = session.query(NewsRefreshRun).filter_by(status="running").first()
        if running:
            raise HTTPException(409, detail=f"Run {running.id} already running")
        query = body.query.strip() if body and body.query and body.query.strip() else None
        run = queue_refresh_run(session, query=query, trigger="manual")
        rid = run.id
        background_tasks.add_task(run_news_refresh, rid)
        return {"run_id": rid, "status": run.status}
    finally:
        session.close()


@router.get("/news/topics")
def get_news_topics():
    session = get_session()
    try:
        return {"topics": get_topics(session), "defaults": DEFAULT_TOPICS}
    finally:
        session.close()


@router.put("/news/topics")
def put_news_topics(body: TopicsBody):
    session = get_session()
    try:
        topics = set_topics(session, body.topics)
        session.commit()
        return {"topics": topics}
    finally:
        session.close()
