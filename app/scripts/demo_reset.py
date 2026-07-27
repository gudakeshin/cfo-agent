#!/usr/bin/env python3
"""Reset demo state: wipe dependent tables, regenerate ERP, reset memory to seed blocks."""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

# Load .env
env_path = BACKEND.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-key-deterministic-suite-only")

from cfo_agent.db import get_session, init_db
from cfo_agent.kra import framework_to_prompt_block, get_active_framework, upsert_kra_version
from cfo_agent.models import (
    Alert,
    AppState,
    ArchivalMemory,
    ChatMessage,
    ConsolidationRun,
    Digest,
    DigestItem,
    Feedback,
    MemoryBlock,
    MemoryBlockRevision,
    PipelineRun,
    Signal,
    WeightingVersion,
)
from cfo_agent.settings import OUTBOX_DIR, get_settings
from cfo_agent.synthetic.generator import generate_erp
from cfo_agent.weighting import create_weighting_version, load_weighting_yaml, weighting_to_prompt_block

get_settings.cache_clear()


def wipe_outbox() -> None:
    if OUTBOX_DIR.is_dir():
        for p in OUTBOX_DIR.glob("*.html"):
            p.unlink()


def reset_memory(session) -> None:
    session.query(MemoryBlockRevision).delete()
    session.query(MemoryBlock).delete()
    session.query(ArchivalMemory).delete()
    session.query(ConsolidationRun).delete()
    session.flush()
    # Clear vec table
    from sqlalchemy import text

    try:
        session.execute(text("DELETE FROM archival_vec"))
    except Exception:
        pass

    fw = get_active_framework()
    # Active weighting content
    w = session.query(WeightingVersion).filter_by(is_active=True).first()
    content = w.content_json if w else load_weighting_yaml()

    blocks = {
        "kra_framework": (framework_to_prompt_block(fw), 50_000),
        "priority_weighting": (weighting_to_prompt_block(content), 10_000),
        "cfo_observations": ("No observations recorded yet.", 2000),
        "quarter_narrative": ("", 2000),
    }
    for name, (text_c, limit) in blocks.items():
        session.add(MemoryBlock(name=name, content=text_c, char_limit=limit))
        session.flush()
        session.add(
            MemoryBlockRevision(
                block_name=name,
                version=1,
                content=text_c,
                edited_by="seed",
                reason="demo_reset",
            )
        )


def main(argv: list[str] | None = None) -> None:
    import argparse

    import cfo_agent.db as dbmod

    parser = argparse.ArgumentParser(description="Reset CFO agent demo state")
    parser.add_argument(
        "--with-digest",
        action="store_true",
        help="Pre-generate seed digest (requires real ANTHROPIC_API_KEY)",
    )
    parser.add_argument("--no-digest", action="store_true", help="Skip seed digest")
    args = parser.parse_args(argv)

    dbmod._engine = None
    dbmod._SessionLocal = None
    get_settings.cache_clear()

    init_db()
    session = get_session()
    weeks: list[str] = []
    try:
        for model in (
            Feedback,
            ChatMessage,
            DigestItem,
            Digest,
            Alert,
            Signal,
            PipelineRun,
            ConsolidationRun,
        ):
            session.query(model).delete()

        # Reset weighting to acquisition seed (keep versions history cleared for clean demo)
        session.query(WeightingVersion).delete()
        session.flush()
        upsert_kra_version(session)
        create_weighting_version(session, load_weighting_yaml(), activate=True)

        print("Regenerating ERP (seed=42)…")
        meta = generate_erp(session, seed=42, stale_days=0)
        weeks = list(meta["weeks"])
        reset_memory(session)

        # Clear replay clock + redline check
        for key in ("replay_clock_week", "last_redline_check"):
            row = session.get(AppState, key)
            if row:
                row.value_json = None
            else:
                session.add(AppState(key=key, value_json=None))

        session.commit()
        wipe_outbox()
        print(f"Reset complete. Weeks {weeks[0]} → {weeks[-1]}")
        print("Memory blocks restored to seed. Outbox cleared.")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    # Reuse seed.py digest helper (same default: on when real API key present)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import seed as seed_mod

    key = os.environ.get("ANTHROPIC_API_KEY", "")
    real_key = seed_mod._is_real_api_key(key)
    if args.no_digest:
        with_digest = False
    elif args.with_digest:
        with_digest = True
        if not real_key:
            print("ERROR: --with-digest requires a real ANTHROPIC_API_KEY.")
            sys.exit(1)
    else:
        with_digest = real_key

    if with_digest:
        seed_mod.generate_seed_digest(weeks)
    else:
        print(
            "Skipping seed digest. Re-run with --with-digest (real API key) "
            "or: python scripts/seed.py --with-digest"
        )


if __name__ == "__main__":
    main()
