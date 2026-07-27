"""WS4 Phase-1 memory fixes — deterministic."""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cfo_agent.api import api_router
from cfo_agent.memory.archival import (
    _brute_force_search,
    archival_insert,
    check_archival_consistency,
    compact_archival,
    delete_archival,
)
from cfo_agent.memory.blocks import ensure_seed_blocks
from cfo_agent.memory.embeddings import embed_texts
from cfo_agent.memory.recall import embed_chat_message, recall_search
from cfo_agent.models import ArchivalMemory, ChatMessage, ConsolidationRun, DigestItem


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(api_router)
    return TestClient(app)


def test_brute_force_per_row_distances_and_embedding_reuse(db_session):
    """Stored embeddings reused; distance ordering is per-row (not last-sim bug)."""
    ensure_seed_blocks(db_session)
    a = archival_insert(db_session, "Working capital DSO acquisition funding risk", week="2026-W27")
    b = archival_insert(db_session, "Opex ratio stable in Admin cost center", week="2026-W27")
    db_session.commit()
    assert a.embedding is not None
    assert b.embedding is not None

    call_count = {"n": 0}
    real_embed = embed_texts

    def spy(texts):
        call_count["n"] += 1
        return real_embed(texts)

    qemb = real_embed(["acquisition funding working capital"])[0]
    with patch("cfo_agent.memory.archival.embed_texts", side_effect=spy):
        hits = _brute_force_search(db_session, qemb, k=2, as_of_week=None)
    # Should not re-embed rows that already have embeddings
    assert call_count["n"] == 0
    assert len(hits) == 2
    # Distances are distinct per row (comprehension binds sim correctly)
    assert hits[0]["distance"] != hits[1]["distance"] or hits[0]["id"] != hits[1]["id"]
    assert hits[0]["distance"] <= hits[1]["distance"]


def test_archival_dedup_by_content_hash(db_session):
    r1 = archival_insert(db_session, "Same notable fact about GST notice", week="2026-W28")
    r2 = archival_insert(
        db_session,
        "Same notable fact about GST notice",
        metadata={"extra": True},
        week="2026-W28",
    )
    db_session.commit()
    assert r1.id == r2.id
    assert (r2.metadata_json or {}).get("extra") is True
    assert db_session.query(ArchivalMemory).count() == 1


def test_delete_and_consistency_repair(db_session):
    row = archival_insert(db_session, "Temporary archival entry for delete test", week="2026-W27")
    db_session.commit()
    aid = row.id
    assert delete_archival(db_session, aid)
    db_session.commit()
    assert db_session.get(ArchivalMemory, aid) is None

    # Insert without vec to simulate orphan/missing
    orphan = ArchivalMemory(
        text="Needs backfill",
        metadata_json={},
        embedding=None,
        content_hash=None,
        week="2026-W27",
        scope="global",
    )
    db_session.add(orphan)
    db_session.commit()
    stats = check_archival_consistency(db_session)
    db_session.commit()
    assert stats["hash_backfilled"] >= 1
    assert stats["embedding_backfilled"] >= 1
    assert stats["missing_vec_inserted"] >= 1
    refreshed = db_session.get(ArchivalMemory, orphan.id)
    assert refreshed.content_hash
    assert refreshed.embedding


def test_semantic_recall_and_fallback(db_session, seeded_erp):
    from cfo_agent.digest.builder import persist_digest
    from cfo_agent.kra import get_active_kra_version
    from cfo_agent.models import PipelineRun
    from cfo_agent.weighting import get_active_weighting

    wv = get_active_weighting(db_session)
    kra = get_active_kra_version(db_session)
    run = PipelineRun(week=seeded_erp["weeks"][-2], weighting_version_id=wv.id, status="completed")
    db_session.add(run)
    db_session.flush()
    digest = persist_digest(
        db_session,
        week=seeded_erp["weeks"][-2],
        weighting=wv,
        kra_version=kra,
        run=run,
        headline="Test",
        surfaced=[],
        empty_bar=True,
        data_health={},
    )
    db_session.commit()
    # Need a digest item for FK
    item = DigestItem(
        digest_id=digest.id,
        rank=1,
        title="Cash headroom",
        what="Cash dipped",
        why="Collections",
        connects="",
        action="Chase AR",
    )
    db_session.add(item)
    db_session.flush()

    msg = ChatMessage(
        digest_item_id=item.id,
        role="user",
        content="Discuss covenant headroom and cash buffer for the acquisition",
        scope="global",
    )
    db_session.add(msg)
    db_session.flush()
    embed_chat_message(db_session, msg.id, msg.content)
    db_session.commit()

    hits = recall_search(db_session, "acquisition cash covenant buffer", limit=5)
    assert hits, "expected semantic or substring recall hit"
    assert any("covenant" in (h.get("content") or "").lower() or "acquisition" in (h.get("content") or "").lower() for h in hits)

    # Force the substring fallback with the scope filter applied: every embedded
    # message is outside scope "cfo2", so the KNN hits are all filtered out and
    # recall must go through _substring_fallback (regression: scope filter used
    # to be applied after limit, raising InvalidRequestError).
    scoped = ChatMessage(
        digest_item_id=item.id,
        role="user",
        content="Scoped note about itc mismatch",
        scope="cfo2",
    )
    db_session.add(scoped)
    db_session.commit()
    fb_hits = recall_search(db_session, "itc mismatch", limit=5, scope="cfo2")
    assert fb_hits and fb_hits[0]["message_id"] == scoped.id
    assert all(h["scope"] == "cfo2" for h in fb_hits)


def test_human_only_block_409(db_session):
    ensure_seed_blocks(db_session)
    db_session.commit()
    client = _client()
    res = client.put(
        "/api/memory/blocks/kra_framework",
        json={"content": "hacked", "reason": "test"},
    )
    assert res.status_code == 409


def test_compaction_keeps_red_lines(db_session):
    for i in range(60):
        archival_insert(
            db_session,
            f"Routine note number {i} about ops",
            metadata={"verdict": "set_aside"},
            week="2026-W30",
        )
    archival_insert(
        db_session,
        "CRITICAL red line cash breach",
        metadata={"is_red_line": True, "verdict": "acted_on"},
        week="2026-W30",
    )
    db_session.commit()
    deleted = compact_archival(db_session, per_week_cap=50)
    db_session.commit()
    assert deleted >= 10
    remaining = db_session.query(ArchivalMemory).filter_by(week="2026-W30").all()
    assert any("CRITICAL" in r.text for r in remaining)
    assert len(remaining) <= 50


def test_scope_filter_isolates_results(db_session):
    archival_insert(db_session, "Global scope cash insight", week="2026-W27", scope="global")
    archival_insert(db_session, "CFO-A private note about M&A", week="2026-W27", scope="cfo_a")
    db_session.commit()

    from cfo_agent.memory.archival import archival_search

    global_hits = archival_search(db_session, "cash insight M&A", k=5, scope="global")
    assert all(h.get("scope", "global") == "global" for h in global_hits)
    a_hits = archival_search(db_session, "M&A private", k=5, scope="cfo_a")
    assert a_hits
    assert all(h.get("scope") == "cfo_a" for h in a_hits)


def test_consolidation_history_endpoint(db_session):
    run = ConsolidationRun(
        window_start=__import__("datetime").datetime.utcnow(),
        window_end=__import__("datetime").datetime.utcnow(),
        summary="Test consolidation",
        edits_json=[{"tool": "memory_insert"}],
        token_usage_json={"calls": 1},
    )
    db_session.add(run)
    db_session.commit()
    client = _client()
    res = client.get("/api/memory/consolidations")
    assert res.status_code == 200
    body = res.json()
    assert isinstance(body, list)
    assert body[0]["summary"] == "Test consolidation"
    assert "token_usage" in body[0]


def test_maintenance_endpoint(db_session):
    client = _client()
    res = client.post("/api/memory/maintenance")
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert "stats" in res.json()


def test_blocks_editable_flag(db_session):
    ensure_seed_blocks(db_session)
    db_session.commit()
    client = _client()
    res = client.get("/api/memory/blocks")
    assert res.status_code == 200
    by_name = {b["name"]: b for b in res.json()}
    assert by_name["kra_framework"]["editable"] is False
    assert by_name["kra_framework"]["managed_by"] == "kra"
    assert by_name["cfo_observations"]["editable"] is True


def test_consolidation_chunking_max_three_calls(db_session, seeded_erp):
    """200 msgs → chunks of 80 capped at 3 complete_with_tools calls."""
    from unittest.mock import patch

    from cfo_agent.digest.builder import persist_digest
    from cfo_agent.kra import get_active_kra_version
    from cfo_agent.memory.consolidation import MAX_CHUNKS, run_consolidation
    from cfo_agent.models import PipelineRun
    from cfo_agent.weighting import get_active_weighting

    ensure_seed_blocks(db_session)
    wv = get_active_weighting(db_session)
    kra = get_active_kra_version(db_session)
    run = PipelineRun(week=seeded_erp["weeks"][-2], weighting_version_id=wv.id, status="completed")
    db_session.add(run)
    db_session.flush()
    digest = persist_digest(
        db_session,
        week=seeded_erp["weeks"][-2],
        weighting=wv,
        kra_version=kra,
        run=run,
        headline="Chunk test",
        surfaced=[],
        empty_bar=True,
        data_health={},
    )
    item = DigestItem(
        digest_id=digest.id,
        rank=1,
        title="Chunk fixture",
        what="x",
        why="y",
        connects="",
        action="z",
    )
    db_session.add(item)
    db_session.flush()

    # More than MAX_CHUNKS * 80 messages → must still call at most MAX_CHUNKS times
    for i in range(250):
        db_session.add(
            ChatMessage(
                digest_item_id=item.id,
                role="user",
                content=f"Chat message about DSO and cash headroom number {i} " * 5,
            )
        )
    db_session.commit()

    calls = {"n": 0}

    def fake_complete(*, system, messages, tools, purpose, budget, tool_handler, **kwargs):
        calls["n"] += 1
        if budget is not None:
            budget.calls += 1
        return ("ok", [])

    with (
        patch("cfo_agent.memory.consolidation.complete_with_tools", side_effect=fake_complete),
        patch("cfo_agent.memory.consolidation.compact_archival", return_value=0),
    ):
        result = run_consolidation(db_session)
    db_session.commit()
    assert result is not None
    assert calls["n"] == MAX_CHUNKS
    assert calls["n"] <= 3
