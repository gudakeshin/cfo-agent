"""Deterministic news feed tests — Anthropic + llm.parse fully mocked, no real API calls."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cfo_agent.api import api_router
from cfo_agent.llm import CallLog
from cfo_agent.models import NewsItem, NewsRefreshRun
from cfo_agent.news import (
    DEFAULT_TOPICS,
    NewsBatch,
    NewsBatchItem,
    fetch_news,
    store_items,
)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(api_router)
    return TestClient(app)


# ─── Fake Anthropic plumbing ────────────────────────────────────────────────────


class FakeUsage:
    def __init__(self, input_tokens=100, output_tokens=50):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0


class FakeTextBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class FakeSearchResult:
    type = "web_search_result"

    def __init__(self, url: str, title: str = "Result"):
        self.url = url
        self.title = title
        self.page_age = None
        self.encrypted_content = "enc"


class FakeToolResultBlock:
    type = "web_search_tool_result"

    def __init__(self, results: list[FakeSearchResult]):
        self.content = results
        self.tool_use_id = "tu_1"


class FakeResponse:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.usage = FakeUsage()
        self.stop_reason = stop_reason


class FakeMessages:
    def __init__(self, response=None, error: Exception | None = None):
        self._response = response
        self._error = error
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return self._response


class FakeClient:
    def __init__(self, response=None, error: Exception | None = None):
        self.messages = FakeMessages(response=response, error=error)


def _patch_search(monkeypatch, urls: list[str], findings_text: str = "Found some news.", error=None):
    """Patch cfo_agent.news._client so the web_search call returns fixed result URLs."""
    if error is not None:
        client = FakeClient(error=error)
    else:
        content = [
            FakeTextBlock(findings_text),
            FakeToolResultBlock([FakeSearchResult(u) for u in urls]),
        ]
        client = FakeClient(response=FakeResponse(content))
    monkeypatch.setattr("cfo_agent.news._client", lambda *a: client)
    return client


def _patch_structure(monkeypatch, items: list[NewsBatchItem]):
    """Patch cfo_agent.news.parse (llm.parse) to hand back a fixed NewsBatch."""
    batch = NewsBatch(items=items)

    def _fake_parse(
        schema, *, system, user, purpose, budget=None, max_tokens=4096, thinking=False, model=None, provider=None
    ):
        if budget is not None:
            budget.calls += 1
            budget.logs.append(CallLog(purpose=purpose, cost_usd=0.0, model=model or ""))
        return batch

    monkeypatch.setattr("cfo_agent.news.parse", _fake_parse)


# ─── fetch_news: store + counts ─────────────────────────────────────────────────


def test_fetch_news_stores_items_and_counts(db_session, monkeypatch):
    urls = ["https://cbic.gov.in/notif/1", "https://sebi.gov.in/circ/2"]
    _patch_search(monkeypatch, urls)
    _patch_structure(
        monkeypatch,
        [
            NewsBatchItem(title="GST rate change", source_url=urls[0], category="rate_change"),
            NewsBatchItem(title="SEBI disclosure update", source_url=urls[1], category="sebi"),
        ],
    )

    run = fetch_news(db_session, trigger="manual")
    db_session.commit()

    assert run.status == "ok"
    assert run.items_found == 2
    assert run.items_new == 2
    assert run.items_dropped == 0
    assert db_session.query(NewsItem).count() == 2


# ─── Grounding ──────────────────────────────────────────────────────────────────


def test_fetch_news_drops_fabricated_url(db_session, monkeypatch):
    real_url = "https://cbic.gov.in/notif/1"
    _patch_search(monkeypatch, [real_url])
    _patch_structure(
        monkeypatch,
        [
            NewsBatchItem(title="Real notification", source_url=real_url),
            NewsBatchItem(
                title="Hallucinated story",
                source_url="https://totally-made-up-site.example/article",
            ),
        ],
    )

    run = fetch_news(db_session, trigger="manual")
    db_session.commit()

    assert run.items_found == 2
    assert run.items_new == 1
    assert run.items_dropped == 1
    rows = db_session.query(NewsItem).all()
    assert len(rows) == 1
    assert rows[0].url == real_url


# ─── Dedup ──────────────────────────────────────────────────────────────────────


def test_dedup_by_url_across_runs(db_session, monkeypatch):
    url = "https://cbic.gov.in/notif/1"
    _patch_search(monkeypatch, [url])
    _patch_structure(monkeypatch, [NewsBatchItem(title="GST notice", source_url=url)])

    run1 = fetch_news(db_session, trigger="manual")
    db_session.commit()
    assert run1.items_new == 1

    # Second run finds the same URL again (title changed slightly) — should dedup on url.
    _patch_structure(monkeypatch, [NewsBatchItem(title="GST notice (updated)", source_url=url)])
    run2 = fetch_news(db_session, trigger="manual")
    db_session.commit()

    assert run2.items_new == 0
    assert run2.items_dropped == 1
    assert db_session.query(NewsItem).count() == 1


def test_dedup_by_title_hash_within_batch(db_session, monkeypatch):
    url_a = "https://cbic.gov.in/notif/1"
    url_b = "https://sebi.gov.in/notif/1"
    _patch_search(monkeypatch, [url_a, url_b])
    _patch_structure(
        monkeypatch,
        [
            NewsBatchItem(title="GST Council meets on rate rationalisation", source_url=url_a),
            # Same title (case/whitespace-insensitive), different URL → dedup by title_hash
            NewsBatchItem(
                title="  GST   council  meets on rate rationalisation  ",
                source_url=url_b,
            ),
        ],
    )

    run = fetch_news(db_session, trigger="manual")
    db_session.commit()

    assert run.items_found == 2
    assert run.items_new == 1
    assert run.items_dropped == 1
    assert db_session.query(NewsItem).count() == 1


# ─── Retention ──────────────────────────────────────────────────────────────────


def test_retention_caps_at_500_newest(db_session):
    run = NewsRefreshRun(trigger="manual", status="running")
    db_session.add(run)
    db_session.commit()

    items = [
        NewsBatchItem(title=f"Item number {i}", source_url=f"https://example.com/a/{i}")
        for i in range(510)
    ]
    new_count, dropped = store_items(db_session, run, items)
    db_session.commit()

    assert new_count == 510
    assert dropped == 0
    assert db_session.query(NewsItem).count() == 500
    assert db_session.query(NewsItem).filter_by(url="https://example.com/a/0").first() is None
    assert db_session.query(NewsItem).filter_by(url="https://example.com/a/509").first() is not None


# ─── Topics ─────────────────────────────────────────────────────────────────────


def test_topics_get_defaults_and_put_overrides(db_session):
    client = _client()

    res = client.get("/api/news/topics")
    assert res.status_code == 200
    body = res.json()
    assert body["topics"] == DEFAULT_TOPICS
    assert body["defaults"] == DEFAULT_TOPICS

    custom = ["Custom topic A", "Custom topic B"]
    res = client.put("/api/news/topics", json={"topics": custom})
    assert res.status_code == 200
    assert res.json()["topics"] == custom

    res = client.get("/api/news/topics")
    assert res.json()["topics"] == custom


# ─── Query refresh ──────────────────────────────────────────────────────────────


def test_query_refresh_records_query(db_session, monkeypatch):
    url = "https://rbi.org.in/notif/1"
    _patch_search(monkeypatch, [url])
    _patch_structure(monkeypatch, [NewsBatchItem(title="FEMA update", source_url=url)])

    run = fetch_news(db_session, query="FEMA cross-border lending")
    db_session.commit()

    assert run.query == "FEMA cross-border lending"
    assert run.trigger == "query"


# ─── List filters ───────────────────────────────────────────────────────────────


def test_list_news_category_and_since_filters(db_session):
    now = datetime.utcnow()
    old = NewsItem(
        url="https://a.example/1",
        title="Old SEBI item",
        title_hash="hash-old",
        category="sebi",
        fetched_at=now - timedelta(days=10),
    )
    recent_sebi = NewsItem(
        url="https://a.example/2",
        title="Recent SEBI item",
        title_hash="hash-recent-sebi",
        category="sebi",
        fetched_at=now,
    )
    recent_gst = NewsItem(
        url="https://a.example/3",
        title="Recent GST item",
        title_hash="hash-recent-gst",
        category="rate_change",
        fetched_at=now,
    )
    db_session.add_all([old, recent_sebi, recent_gst])
    db_session.commit()

    client = _client()

    res = client.get("/api/news", params={"category": "sebi"})
    assert res.status_code == 200
    urls = {n["url"] for n in res.json()}
    assert urls == {old.url, recent_sebi.url}

    since = (now - timedelta(days=1)).isoformat()
    res = client.get("/api/news", params={"since": since})
    urls = {n["url"] for n in res.json()}
    assert urls == {recent_sebi.url, recent_gst.url}

    res = client.get("/api/news", params={"category": "sebi", "since": since})
    urls = {n["url"] for n in res.json()}
    assert urls == {recent_sebi.url}


# ─── Refresh endpoint: 409 + background handoff ────────────────────────────────


def test_refresh_returns_409_when_already_running(db_session):
    running = NewsRefreshRun(trigger="manual", status="running")
    db_session.add(running)
    db_session.commit()

    client = _client()
    res = client.post("/api/news/refresh", json={})
    assert res.status_code == 409


def test_refresh_creates_run_and_backgrounds_task(db_session, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr("cfo_agent.api.news.run_news_refresh", lambda run_id: calls.append(run_id))

    client = _client()
    res = client.post("/api/news/refresh", json={"query": "GST e-invoicing threshold"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "running"
    rid = body["run_id"]
    assert calls == [rid]

    row = db_session.get(NewsRefreshRun, rid)
    assert row is not None
    assert row.query == "GST e-invoicing threshold"


# ─── Status endpoint ────────────────────────────────────────────────────────────


def test_status_endpoint(db_session, monkeypatch):
    url = "https://mca.gov.in/circ/1"
    _patch_search(monkeypatch, [url])
    _patch_structure(monkeypatch, [NewsBatchItem(title="MCA circular", source_url=url)])
    fetch_news(db_session, trigger="schedule")
    db_session.commit()

    client = _client()
    res = client.get("/api/news/status")
    assert res.status_code == 200
    body = res.json()
    assert body["running"] is False
    assert body["total_items"] == 1
    assert body["latest_run"]["status"] == "ok"
    assert body["latest_run"]["items_new"] == 1


# ─── Error path ─────────────────────────────────────────────────────────────────


def test_fetch_news_error_path_marks_run_failed(db_session, monkeypatch):
    _patch_search(monkeypatch, [], error=RuntimeError("upstream boom"))

    run = fetch_news(db_session, trigger="manual")
    db_session.commit()

    assert run.status == "error"
    assert "upstream boom" in (run.error or "")
    assert db_session.query(NewsItem).count() == 0


def test_topics_put_strips_blanks(db_session):
    client = _client()
    res = client.put("/api/news/topics", json={"topics": ["  Kept topic  ", "", "   "]})
    assert res.status_code == 200
    assert res.json()["topics"] == ["Kept topic"]
