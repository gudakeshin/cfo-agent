"""Regulatory/GST news feed — Anthropic web_search tool + structured extraction.

Flow: web_search (server tool, single messages.create call, possibly multi-round on
pause_turn) → collect grounded result URLs from web_search_tool_result blocks →
llm.parse() the raw findings into NewsBatch → drop any item whose source_url host
isn't among the actual search results (no hallucinated links) → dedup + store + retain
newest 500.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pydantic import BaseModel, Field
from typing_extensions import Literal

from cfo_agent.db import get_session
from cfo_agent.llm import CallLog, LlmBudget, _client, _usage_cost, parse
from cfo_agent.models import AppState, NewsItem, NewsRefreshRun
from cfo_agent.settings import get_settings

logger = logging.getLogger(__name__)

DEFAULT_TOPICS = [
    "GST & CBIC (rates, notifications, circulars, council, e-invoicing, ITC)",
    "SEBI LODR & disclosures",
    "MCA / Companies Act",
    "RBI & FEMA",
    "Ind AS / ICAI",
    "Union Budget & Finance Act",
]

TOPICS_STATE_KEY = "news_topics"
RETENTION_LIMIT = 500
NEWS_MAX_CALLS = 4
MAX_SEARCH_ROUNDS = 3

WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 5,
    "user_location": {"type": "approximate", "country": "IN", "timezone": "Asia/Kolkata"},
}

Category = Literal[
    "rate_change",
    "notification",
    "circular",
    "council_decision",
    "e_invoicing",
    "itc",
    "deadline",
    "sebi",
    "mca",
    "rbi_fema",
    "ind_as",
    "budget",
    "other",
]
Relevance = Literal["high", "medium", "low"]


class NewsBatchItem(BaseModel):
    title: str
    summary: str = ""
    source_url: str
    source_name: str = ""
    published: str | None = None
    category: Category = "other"
    relevance: Relevance = "medium"


class NewsBatch(BaseModel):
    items: list[NewsBatchItem] = Field(default_factory=list)


SEARCH_SYSTEM = (
    "You are a research assistant for an Indian CFO. Use the web_search tool to find "
    "the most recent, credible news on Indian tax/regulatory/finance topics. Prefer "
    "primary sources (CBIC, GST Council, SEBI, MCA, RBI, ICAI, Ministry of Finance, "
    "PIB) and major financial dailies. For every distinct finding, report: a short "
    "title, a 1-2 sentence summary, the exact source URL you found it at, the "
    "publisher/source name, and a publication date if visible. Do not repeat the same "
    "story from multiple sources unless materially different. Only report items you "
    "actually found via web_search — never invent a URL."
)

STRUCTURE_SYSTEM = (
    "Extract structured news items from the research findings below. Each item must "
    "map to exactly one distinct finding, with a title, a 1-2 sentence summary, the "
    "source_url copied verbatim, source_name, a published date string if given (else "
    "omit), a category, and a relevance rating. Only use source_url values that appear "
    "verbatim in the provided 'Actual URLs' list — if a finding's URL is not in that "
    "list, omit the item entirely rather than guessing."
)


# ─── Topics (AppState) ─────────────────────────────────────────────────────────


def get_topics(session: Session) -> list[str]:
    row = session.get(AppState, TOPICS_STATE_KEY)
    if row and row.value_json:
        return list(row.value_json)
    return list(DEFAULT_TOPICS)


def set_topics(session: Session, topics: list[str]) -> list[str]:
    cleaned = [t.strip() for t in topics if t and t.strip()]
    row = session.get(AppState, TOPICS_STATE_KEY)
    if row:
        row.value_json = cleaned
    else:
        row = AppState(key=TOPICS_STATE_KEY, value_json=cleaned)
        session.add(row)
    session.flush()
    return cleaned


# ─── URL grounding helpers ─────────────────────────────────────────────────────


def _host(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001
        return ""
    return netloc.removeprefix("www.")


def collect_result_urls(content_blocks: list[Any]) -> set[str]:
    """Pull every URL Claude's web_search tool actually returned, from
    web_search_tool_result blocks. These are the only URLs we trust."""
    urls: set[str] = set()
    for block in content_blocks:
        if getattr(block, "type", None) != "web_search_tool_result":
            continue
        content = getattr(block, "content", None)
        if not isinstance(content, list):
            continue  # error content (WebSearchToolResultError) — no results
        for result in content:
            url = getattr(result, "url", None)
            if url:
                urls.add(url)
    return urls


def ground_items(
    items: list[NewsBatchItem], result_urls: set[str]
) -> tuple[list[NewsBatchItem], int]:
    """Drop items whose source_url host isn't among the actual web_search results."""
    hosts = {_host(u) for u in result_urls if u}
    kept: list[NewsBatchItem] = []
    dropped = 0
    for item in items:
        host = _host(item.source_url) if item.source_url else ""
        if not host or host not in hosts:
            dropped += 1
            continue
        kept.append(item)
    return kept, dropped


# ─── Dedup + store + retention ─────────────────────────────────────────────────


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def _title_hash(title: str) -> str:
    return hashlib.sha256(_normalize_title(title).encode("utf-8")).hexdigest()


def _apply_retention(session: Session, limit: int = RETENTION_LIMIT) -> None:
    total = session.query(NewsItem).count()
    if total <= limit:
        return
    keep_ids = {
        row[0]
        for row in (
            session.query(NewsItem.id)
            .order_by(NewsItem.fetched_at.desc(), NewsItem.id.desc())
            .limit(limit)
            .all()
        )
    }
    session.query(NewsItem).filter(~NewsItem.id.in_(keep_ids)).delete(synchronize_session=False)
    session.flush()


def store_items(
    session: Session, run: NewsRefreshRun, items: list[NewsBatchItem]
) -> tuple[int, int]:
    """Dedup by url unique OR title_hash, insert new NewsItem rows, apply retention.

    Returns (items_new, items_dropped) — dropped here means "already known" duplicates
    (grounding drops are counted separately by the caller).
    """
    new_count = 0
    dropped_count = 0
    for item in items:
        url = (item.source_url or "").strip()
        if not url:
            dropped_count += 1
            continue
        title_hash = _title_hash(item.title)
        existing = (
            session.query(NewsItem)
            .filter(or_(NewsItem.url == url, NewsItem.title_hash == title_hash))
            .first()
        )
        if existing:
            dropped_count += 1
            continue
        row = NewsItem(
            url=url,
            title=(item.title or "").strip(),
            title_hash=title_hash,
            summary=item.summary or "",
            source_name=item.source_name or _host(url),
            published=item.published,
            category=item.category,
            relevance=item.relevance,
            refresh_run_id=run.id,
        )
        session.add(row)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            dropped_count += 1
            continue
        new_count += 1
    if new_count:
        _apply_retention(session)
    return new_count, dropped_count


# ─── Prompt building ───────────────────────────────────────────────────────────


def _build_search_prompt(topics: list[str], query: str | None) -> str:
    if query:
        topic_line = f'Ad-hoc research query from the CFO: "{query}"'
    else:
        topic_line = "Tracked topics:\n" + "\n".join(f"- {t}" for t in topics)
    return (
        f"{topic_line}\n\n"
        "Use web_search to find the latest developments (favor the last 7 days, but "
        "include anything materially important and recent) relevant to an Indian "
        "company's CFO. For each distinct finding, report: a short title, a 1-2 "
        "sentence summary, the exact source URL, the publisher/source name, and a "
        "publish date if visible. Do not invent URLs — only report what web_search "
        "actually returned."
    )


def _build_structure_prompt(findings_text: str, result_urls: set[str]) -> str:
    urls_block = "\n".join(sorted(result_urls)) if result_urls else "(none)"
    return (
        f"Research findings:\n{findings_text or '(no findings)'}\n\n"
        "Actual URLs returned by web search (use ONLY these, verbatim, as source_url; "
        "omit any finding whose URL is not in this list):\n"
        f"{urls_block}"
    )


def _search_with_tool(client: Any, settings: Any, system: str, user: str, budget: LlmBudget) -> list[Any]:
    """Single web_search-enabled turn, with bounded pause_turn continuation."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
    collected: list[Any] = []
    for _ in range(MAX_SEARCH_ROUNDS):
        if budget.remaining() <= 0:
            break
        t0 = time.time()
        response = client.messages.create(
            model=settings.news_model,
            max_tokens=4096,
            system=system,
            messages=messages,
            tools=[WEB_SEARCH_TOOL],
        )
        duration = int((time.time() - t0) * 1000)
        inp, out, cr, cw, cost = _usage_cost(response.usage)
        budget.calls += 1
        budget.logs.append(
            CallLog(
                purpose="news_search",
                input_tokens=inp,
                output_tokens=out,
                cache_read_tokens=cr,
                cache_write_tokens=cw,
                cost_usd=cost,
                duration_ms=duration,
                model=settings.news_model,
            )
        )
        logger.info("news_search cost=$%.4f tokens_in=%s out=%s", cost, inp, out)
        collected.extend(response.content)
        if getattr(response, "stop_reason", None) != "pause_turn":
            break
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": "Continue searching and then finish your findings."})
    return collected


# ─── Run lifecycle ─────────────────────────────────────────────────────────────


def queue_refresh_run(
    session: Session, query: str | None = None, trigger: str = "manual"
) -> NewsRefreshRun:
    """Create + commit a `running` NewsRefreshRun row without doing any LLM work.
    Used by the API so it can hand a run_id to a BackgroundTask immediately."""
    if query and trigger == "manual":
        trigger = "query"
    run = NewsRefreshRun(trigger=trigger, query=query, status="running")
    session.add(run)
    session.commit()
    return run


def _execute_run(session: Session, run: NewsRefreshRun) -> NewsRefreshRun:
    """Do the actual web_search → structure → ground → store work for an existing run."""
    settings = get_settings()
    try:
        topics = get_topics(session)
        client = _client()
        budget = LlmBudget(max_calls=NEWS_MAX_CALLS)

        search_user = _build_search_prompt(topics, run.query)
        content_blocks = _search_with_tool(client, settings, SEARCH_SYSTEM, search_user, budget)

        findings_text = "\n\n".join(
            block.text
            for block in content_blocks
            if getattr(block, "type", None) == "text" and getattr(block, "text", None)
        )
        result_urls = collect_result_urls(content_blocks)

        structure_user = _build_structure_prompt(findings_text, result_urls)
        # Structure reuses news_model (Sonnet by default) — daily fetches don't need Opus
        batch = parse(
            NewsBatch,
            system=STRUCTURE_SYSTEM,
            user=structure_user,
            purpose="news_structure",
            budget=budget,
            max_tokens=4096,
            model=settings.news_model,
        )

        kept_items, grounding_dropped = ground_items(batch.items, result_urls)
        new_count, dedup_dropped = store_items(session, run, kept_items)

        run.items_found = len(batch.items)
        run.items_new = new_count
        run.items_dropped = grounding_dropped + dedup_dropped
        run.status = "ok"
        run.finished_at = datetime.utcnow()
        session.commit()
        logger.info(
            "News refresh run %s: found=%s new=%s dropped=%s cost=$%.4f",
            run.id,
            run.items_found,
            run.items_new,
            run.items_dropped,
            budget.total_cost(),
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("News refresh run %s failed", run.id)
        session.rollback()
        run = session.get(NewsRefreshRun, run.id)
        if run:
            run.status = "error"
            run.error = str(e)
            run.finished_at = datetime.utcnow()
            session.commit()
    return run


def fetch_news(
    session: Session, query: str | None = None, trigger: str = "manual"
) -> NewsRefreshRun:
    """Create a run and execute it synchronously in the given session. Convenience
    entrypoint for manual/scheduled use and for tests."""
    run = queue_refresh_run(session, query=query, trigger=trigger)
    return _execute_run(session, run)


def run_news_refresh(run_id: int) -> None:
    """Background-task entrypoint: run must already exist (created via
    queue_refresh_run, typically by the /api/news/refresh endpoint)."""
    session = get_session()
    try:
        run = session.get(NewsRefreshRun, run_id)
        if not run:
            logger.warning("run_news_refresh: run %s not found", run_id)
            return
        _execute_run(session, run)
    finally:
        session.close()


def job_news_refresh() -> None:
    """Scheduler entrypoint — tracked-topics refresh, trigger=schedule."""
    session = get_session()
    try:
        fetch_news(session, trigger="schedule")
    except Exception:  # noqa: BLE001
        logger.exception("Scheduled news refresh failed")
    finally:
        session.close()
