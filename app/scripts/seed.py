#!/usr/bin/env python3
"""Seed database: ERP data, KRA/weighting versions, memory blocks, embedding model.

Optional: --with-digest (default when a real ANTHROPIC_API_KEY is set) runs the
pipeline for the week two before the latest quarter week so the inbox isn't empty.
Use --no-digest to skip. Use --with-digest to force (fails if key is a placeholder).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

PLACEHOLDER_KEY_MARKERS = (
    "sk-ant-...",
    "sk-ant-seed-placeholder",
    "sk-ant-test-key",
    "placeholder",
    "not-for-api",
)


EDITABLE_BLOCKS = {
    "cfo_observations": (
        "No observations recorded yet.",
        2000,
    ),
    "quarter_narrative": (
        "",
        2000,
    ),
}


def _load_dotenv() -> None:
    env_path = BACKEND.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _is_real_api_key(key: str | None) -> bool:
    if not key or not key.strip():
        return False
    k = key.strip().lower()
    return not any(m in k for m in PLACEHOLDER_KEY_MARKERS)


def seed_memory_blocks(session) -> None:
    from cfo_agent.kra import framework_to_prompt_block, get_active_framework
    from cfo_agent.models import MemoryBlock, MemoryBlockRevision
    from cfo_agent.weighting import seed_weighting_from_yaml, weighting_to_prompt_block

    fw = get_active_framework()
    active_w = seed_weighting_from_yaml(session)

    human_blocks = {
        "kra_framework": (framework_to_prompt_block(fw), 50_000),
        "priority_weighting": (weighting_to_prompt_block(active_w.content_json), 10_000),
    }
    for name, (content, limit) in {**human_blocks, **EDITABLE_BLOCKS}.items():
        row = session.get(MemoryBlock, name)
        if row is None:
            row = MemoryBlock(name=name, content=content, char_limit=limit)
            session.add(row)
            session.flush()
            session.add(
                MemoryBlockRevision(
                    block_name=name,
                    version=1,
                    content=content,
                    edited_by="seed",
                    reason="Initial seed",
                )
            )
        else:
            if name in human_blocks:
                row.content = content
                row.char_limit = limit


def ensure_embeddings_downloaded() -> None:
    """Pull fastembed ONNX model (~90 MB) once at seed time."""
    print("Downloading / verifying embedding model (fastembed all-MiniLM-L6-v2)…")
    from cfo_agent.memory.embeddings import embed_texts, get_embedding_dim

    vecs = embed_texts(["cfo agent seed warmup"])
    assert len(vecs) == 1 and len(vecs[0]) == get_embedding_dim()
    print(f"  Embedding dim={get_embedding_dim()} OK")


def generate_seed_digest(weeks: list[str]) -> None:
    """Generate digest for week two before latest (e.g. W26 when quarter ends W28)."""
    from cfo_agent.db import get_session
    from cfo_agent.models import Digest, PipelineRun
    from cfo_agent.pipeline.orchestrator import run_pipeline
    from cfo_agent.weighting import get_active_weighting

    if len(weeks) < 3:
        print(f"  Not enough weeks ({len(weeks)}) to pick a seed digest week; skipping.")
        return

    week = weeks[-3]  # two before latest
    session = get_session()
    try:
        existing = session.query(Digest).filter_by(week=week).first()
        if existing:
            print(f"  Digest for {week} already exists (id={existing.id}); skipping generate.")
            return
        wv = get_active_weighting(session)
        run = PipelineRun(
            week=week,
            weighting_version_id=wv.id,
            status="pending",
        )
        session.add(run)
        session.commit()
        rid = run.id
        print(f"  Generating digest for {week} (run_id={rid}) via Claude API…")
    finally:
        session.close()

    run_pipeline(rid, week, None)

    session = get_session()
    try:
        run = session.get(PipelineRun, rid)
        digest = session.query(Digest).filter_by(week=week).order_by(Digest.id.desc()).first()
        if run and run.status == "completed" and digest:
            print(f"  Seed digest ready: {week} id={digest.id} headline={digest.headline[:80]!r}")
        else:
            status = run.status if run else "missing"
            err = run.error if run else "no run"
            print(f"  WARNING: seed digest failed (status={status}): {err}")
            sys.exit(1)
    finally:
        session.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Seed CFO agent DB")
    parser.add_argument(
        "--with-digest",
        action="store_true",
        default=None,
        help="Pre-generate digest for week[-3] (requires real ANTHROPIC_API_KEY)",
    )
    parser.add_argument(
        "--no-digest",
        action="store_true",
        help="Skip pre-generating a digest even if a real API key is set",
    )
    args = parser.parse_args(argv)

    _load_dotenv()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "WARNING: ANTHROPIC_API_KEY not set. Seed will still load data; "
            "set .env before running the API."
        )
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-seed-placeholder-not-for-api-calls"

    key = os.environ.get("ANTHROPIC_API_KEY", "")
    real_key = _is_real_api_key(key)

    if args.no_digest:
        with_digest = False
    elif args.with_digest:
        with_digest = True
        if not real_key:
            print(
                "ERROR: --with-digest requires a real ANTHROPIC_API_KEY in .env "
                "(placeholder keys are not accepted)."
            )
            sys.exit(1)
    else:
        # Default: on when real key present
        with_digest = real_key

    from cfo_agent import settings as settings_mod

    settings_mod.get_settings.cache_clear()

    from cfo_agent.db import get_session, init_db
    from cfo_agent.kra import upsert_kra_version
    from cfo_agent.models import AppState
    from cfo_agent.synthetic.generator import generate_erp
    from cfo_agent.weighting import seed_weighting_from_yaml

    init_db()
    session = get_session()
    weeks: list[str] = []
    try:
        upsert_kra_version(session)
        seed_weighting_from_yaml(session)
        print("Generating ERP data (seed=42)…")
        meta = generate_erp(session, seed=42, stale_days=0)
        weeks = list(meta["weeks"])
        print(f"  Weeks: {weeks[0]} → {weeks[-1]}")
        print(f"  Invoices: {meta['sales_invoices']}, Receipts: {meta['receipts']}")
        seed_memory_blocks(session)

        if not session.get(AppState, "replay_clock_week"):
            session.add(AppState(key="replay_clock_week", value_json=None))

        session.commit()
        print("DB seed complete.")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    try:
        ensure_embeddings_downloaded()
    except Exception as e:
        print(f"Embedding warmup deferred (will download on first use): {e}")

    if with_digest:
        generate_seed_digest(weeks)
    else:
        if not real_key:
            print(
                "Skipping seed digest (no real ANTHROPIC_API_KEY). "
                "Pass --with-digest after setting .env, or generate from the UI."
            )
        else:
            print("Skipping seed digest (--no-digest).")

    print("Done.")


if __name__ == "__main__":
    main()
