"""Deterministic memory block + archival tests."""

from cfo_agent.digest.builder import persist_digest
from cfo_agent.kra import upsert_kra_version
from cfo_agent.memory.archival import archival_insert, archival_search
from cfo_agent.memory.blocks import (
    MemoryError,
    ensure_seed_blocks,
    memory_insert,
    memory_replace,
    revision_history,
    set_block_content,
)
from cfo_agent.models import ArchivalMemory, Feedback, PipelineRun
from cfo_agent.pipeline.schemas import SectionBlock, SynthesizeItemOutput
from cfo_agent.pipeline.step4_filter import FilterItem
from cfo_agent.pipeline.step6_surface import SurfacedItem
from cfo_agent.weighting import get_active_weighting


def test_block_char_limit_and_revisions(db_session):
    ensure_seed_blocks(db_session)
    db_session.commit()
    set_block_content(
        db_session,
        "cfo_observations",
        "Prefers brief GST notes.",
        edited_by="agent",
        reason="test",
    )
    db_session.commit()
    hist = revision_history(db_session, "cfo_observations")
    assert len(hist) >= 2
    try:
        set_block_content(
            db_session,
            "cfo_observations",
            "x" * 3000,
            edited_by="agent",
            reason="overflow",
        )
        assert False, "should have raised"
    except MemoryError as e:
        assert "char_limit" in str(e)


def test_human_only_blocks_reject_agent(db_session):
    ensure_seed_blocks(db_session)
    db_session.commit()
    try:
        memory_replace(
            db_session,
            "kra_framework",
            "KRA",
            "X",
            edited_by="agent",
        )
        assert False
    except MemoryError:
        pass


def test_archival_semantic_search(db_session):
    ensure_seed_blocks(db_session)
    archival_insert(
        db_session,
        "DSO deterioration threatens acquisition funding under REL-1 working capital thread.",
        metadata={"type": "test"},
        week="2026-W27",
    )
    archival_insert(
        db_session,
        "Opex ratio stable in Admin cost center this week.",
        metadata={"type": "test"},
        week="2026-W27",
    )
    db_session.commit()
    hits = archival_search(db_session, "acquisition funding working capital DSO", k=2)
    assert hits, "expected search hits"
    assert "DSO" in hits[0]["text"] or "acquisition" in hits[0]["text"].lower()


def test_memory_insert_replace(db_session):
    ensure_seed_blocks(db_session)
    memory_insert(db_session, "quarter_narrative", "W27: DSO drift surfaced.")
    db_session.commit()
    memory_replace(
        db_session,
        "quarter_narrative",
        "W27: DSO drift surfaced.",
        "W27: DSO drift surfaced; CFO acted on collections.",
    )
    db_session.commit()
    from cfo_agent.memory.blocks import get_block

    assert "acted on collections" in get_block(db_session, "quarter_narrative").content


def test_archival_digest_item_id_link(db_session):
    """Archival rows created after persist_digest carry digest_item_id for consolidation."""
    ensure_seed_blocks(db_session)
    wv = get_active_weighting(db_session)
    kra = upsert_kra_version(db_session)
    run = PipelineRun(
        week="2026-W26",
        weighting_version_id=wv.id,
        status="completed",
        current_step="done",
    )
    db_session.add(run)
    db_session.flush()

    syn = SynthesizeItemOutput(
        title="DSO drift threatens deal funding",
        what=SectionBlock(text="DSO rose 9 days.", citations=["metric:dso:2026-W26"]),
        why=SectionBlock(text="Anchor lag.", citations=["metric:dso:2026-W26"]),
        connects=SectionBlock(text="REL-1.", citations=[]),
        action=SectionBlock(text="Push collections.", citations=[]),
    )
    fi = FilterItem(
        weighted=None,
        is_red_line=False,
        priority_score=2.5,
        kra_ids=["STW-1", "STR-1"],
        signal_ids=["dso:2026-W26"],
        score_decomp={"face_weight": 0.35, "rank_boost": 1.5, "severity": 2.0},
    )
    surfaced = [
        SurfacedItem(
            filter_item=fi,
            synthesis=syn,
            citations={"what": ["metric:dso:2026-W26"], "why": [], "connects": [], "action": []},
            all_citation_ids=["metric:dso:2026-W26"],
        )
    ]
    digest = persist_digest(
        db_session,
        week="2026-W26",
        weighting=wv,
        kra_version=kra,
        run=run,
        headline="Test headline",
        surfaced=surfaced,
        empty_bar=False,
        data_health={"ok": True},
    )
    items_by_rank = {di.rank: di for di in digest.items}
    for rank, s in enumerate(surfaced, start=1):
        di = items_by_rank[rank]
        archival_insert(
            db_session,
            f"[2026-W26] {s.synthesis.title}: {s.synthesis.what.text}",
            metadata={
                "digest_id": digest.id,
                "digest_item_id": di.id,
                "digest_item_rank": rank,
                "type": "digest_item",
            },
            week="2026-W26",
        )
    db_session.commit()

    row = db_session.query(ArchivalMemory).filter_by(week="2026-W26").one()
    assert row.metadata_json.get("digest_item_id") == digest.items[0].id

    # Same patch path consolidation uses when feedback lands
    fb = Feedback(
        digest_item_id=digest.items[0].id,
        verdict="acted_on",
        note="",
        already_knew=False,
    )
    db_session.add(fb)
    db_session.flush()
    meta = dict(row.metadata_json or {})
    if meta.get("digest_item_id") == digest.items[0].id:
        meta["verdict"] = fb.verdict
        meta["already_knew"] = fb.already_knew
        meta["feedback_note"] = fb.note
        row.metadata_json = meta
    db_session.commit()
    db_session.refresh(row)
    assert row.metadata_json.get("verdict") == "acted_on"
