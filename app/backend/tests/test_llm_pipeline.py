"""LLM pipeline smoke — real Claude API. Run with: pytest -m llm"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cfo_agent.citations import resolve_citation
from cfo_agent.models import Digest, DigestItem, PipelineRun
from cfo_agent.pipeline.orchestrator import run_pipeline
from cfo_agent.weighting import get_active_weighting

pytestmark = pytest.mark.llm


def _key_looks_real(key: str) -> bool:
    if not key or not key.strip():
        return False
    k = key.strip()
    return (
        "test-key" not in k
        and "placeholder" not in k.lower()
        and not k.startswith("sk-ant-...")
    )


def _resolve_real_key(monkeypatch) -> str | None:
    """Prefer a real key from process env or app .env (conftest may set a fake key)."""
    candidates = [os.environ.get("ANTHROPIC_API_KEY", "")]
    env_path = Path(__file__).resolve().parents[2] / ".env"  # app/.env
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.strip().startswith("ANTHROPIC_API_KEY="):
                candidates.append(line.split("=", 1)[1].strip())
                break
    for key in candidates:
        if _key_looks_real(key):
            monkeypatch.setenv("ANTHROPIC_API_KEY", key)
            return key
    return None


@pytest.fixture
def require_api(monkeypatch):
    key = _resolve_real_key(monkeypatch)
    if not key:
        pytest.skip("Real ANTHROPIC_API_KEY required for llm tests")
    from cfo_agent import settings as settings_mod

    settings_mod.get_settings.cache_clear()


def test_pipeline_smoke_completes(require_api, seeded_erp, db_session):
    """One-week Map→Surface run: completes with KRA + resolvable citations."""
    from cfo_agent.db import get_session
    from cfo_agent.memory.blocks import ensure_seed_blocks

    ensure_seed_blocks(db_session)
    db_session.commit()

    week = seeded_erp["weeks"][-3]
    wv = get_active_weighting(db_session)
    run = PipelineRun(week=week, weighting_version_id=wv.id, status="pending")
    db_session.add(run)
    db_session.commit()
    rid = run.id
    wv_id = wv.id

    run_pipeline(rid, week, wv_id)

    session = get_session()
    try:
        run = session.get(PipelineRun, rid)
        assert run is not None
        assert run.status == "completed", f"status={run.status} error={run.error}"
        assert run.suppressed_json is not None
        digest = session.get(Digest, run.digest_id)
        assert digest is not None
        items = (
            session.query(DigestItem)
            .filter_by(digest_id=digest.id)
            .order_by(DigestItem.rank)
            .all()
        )
        assert items, "expected at least one surfaced item"
        for item in items:
            assert item.kra_ids_json, f"item {item.id} missing KRA"
            cites = item.citations_json or {}
            all_ids: list[str] = []
            if isinstance(cites, dict):
                for v in cites.values():
                    if isinstance(v, list):
                        all_ids.extend(v)
            elif isinstance(cites, list):
                all_ids = [c if isinstance(c, str) else c.get("id", "") for c in cites]
            assert all_ids, f"item {item.id} has no citations"
            for cid in all_ids:
                r = resolve_citation(session, cid)
                assert r.ok, f"unresolvable citation {cid}: {r.error}"
    finally:
        session.close()
