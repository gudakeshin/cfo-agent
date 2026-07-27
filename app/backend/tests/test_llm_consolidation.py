"""LLM-marked tests — real Claude API. Run with: pytest -m llm"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.llm


def _has_real_key() -> bool:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    return bool(key) and "test-key" not in key and not key.startswith("sk-ant-...")


@pytest.fixture
def require_api():
    if not _has_real_key():
        pytest.skip("Real ANTHROPIC_API_KEY required for llm tests")


def test_consolidation_edits_observations(require_api, seeded_erp, db_session):
    from datetime import datetime

    from cfo_agent.memory.blocks import ensure_seed_blocks, get_block
    from cfo_agent.memory.consolidation import run_consolidation
    from cfo_agent.models import Digest, DigestItem, Feedback, WeightingVersion
    from cfo_agent.weighting import get_active_weighting

    ensure_seed_blocks(db_session)
    wv = get_active_weighting(db_session)
    week = seeded_erp["weeks"][-2]
    digest = Digest(
        week=week,
        weighting_version_id=wv.id,
        headline="Test",
        weighting_label=wv.label,
    )
    db_session.add(digest)
    db_session.flush()
    item = DigestItem(
        digest_id=digest.id,
        rank=1,
        title="DSO stretch threatens deal funding",
        what="DSO up 9 days",
        why="Collections lag",
        connects="REL-1",
        action="Call Apex",
        kra_ids_json=["STW-1", "STR-1"],
        citations_json={"what": [f"metric:dso_days:{week}"]},
    )
    db_session.add(item)
    db_session.flush()
    db_session.add(
        Feedback(
            digest_item_id=item.id,
            verdict="acted_on",
            note="too much detail on GST mechanics — keep brief",
            already_knew=False,
        )
    )
    db_session.commit()

    before = get_block(db_session, "cfo_observations").content
    run = run_consolidation(db_session)
    db_session.commit()
    assert run is not None
    after = get_block(db_session, "cfo_observations").content
    # Expect an edit (content change or archival edits recorded)
    assert run.edits_json or after != before
