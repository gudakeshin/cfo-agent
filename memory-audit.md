# CFO Agent Memory Subsystem — Audit & Letta Benchmark

**Date:** 2026-07-18  
**Scope:** Post Phase-1 implementation (`projects/cfo-agent/app/backend/cfo_agent/memory/`)  
**Status:** Phase-1 complete; Phase-2 roadmap below

---

## Architecture inventory

| Component | Path | Role |
|-----------|------|------|
| Core blocks | `memory/blocks.py` | Four named blocks (`kra_framework`, `priority_weighting`, `cfo_observations`, `quarter_narrative`) with char limits + revision history |
| Archival | `memory/archival.py` | Long-term semantic store; ORM row + `archival_vec` (sqlite-vec) + stored float32 embedding bytes |
| Recall | `memory/recall.py` | Chat history search via `chat_vec` KNN + substring fallback |
| Consolidation | `memory/consolidation.py` | Sleep-time job: feedback/chat → block edits + archival; chunked; compaction at end |
| Embeddings | `memory/embeddings.py` | fastembed MiniLM-L6-v2 (384-d) |
| Tools | `memory/tools.py` | Claude tool schemas for write/search/consolidation/chat |
| API | `api/memory.py` | Blocks, search, consolidate, maintenance, consolidation history |
| Models | `models.py` | `MemoryBlock`, `MemoryBlockRevision`, `ArchivalMemory`, `ConsolidationRun`, `ChatMessage` |

**Migrations:** `db.py::_ensure_column` adds `archival_memory.content_hash`, `archival_memory.scope`, `chat_messages.scope`. Virtual tables `archival_vec` and `chat_vec` created in `init_db()`.

---

## Letta benchmark

| Capability | Letta | CFO Agent (post Phase-1) | Verdict |
|------------|-------|--------------------------|---------|
| Core memory blocks | Named blocks with limits, agent/human edit rules | Four blocks; human-only derived from KRA/Weighting editors (409 on direct PUT); agent-editable observations/narrative | ≈ parity |
| Archival memory | Insert + semantic search, importance, pagination | Insert + dedup + semantic search + compaction scoring; no importance-weighted retrieval, no pagination | Parity-ish |
| Recall (conversation search) | Semantic conversation search | `chat_vec` KNN + substring fallback; embed on chat write | Gap closed |
| Sleep-time agent | Dedicated sleeptime agent | Consolidation job (debounce + pre-digest + manual); chunked 80×3; not a separate agent loop | Partial |
| Per-user / agent scoping | Identity-scoped memory | `scope` column + filter plumbing on archival/recall (default `global`); **no user table / login** — full scoped core blocks need identity | Groundwork only; full → Phase-2 |
| Context management | Pressure-based eviction / summarization in chat loop | Compaction of archival by week; no chat-context pressure summarization | Gap → Phase-2 |
| Tool-set naming | `core_memory_append`, paginated `conversation_search` | `memory_insert` / `memory_replace`, `recall_search`, `archival_*` — functional but not naming-parity | Partial |
| Observability / ADE | Agent Development Environment | Consolidation history API + Memory UI panel; maintenance endpoint | Improved vs pre-Phase-1 |

---

## Findings (including disproven reports)

1. **`_brute_force_search` “distance bug” — DISPROVEN.** The list comprehension correctly binds `sim` per `(sim, r)` tuple (`archival.py`). The real defect was O(N) re-embedding on every fallback call plus a dead `ArchivalMemory.embedding` column. Phase-1 stores float32 bytes on insert and reuses them; only NULL embeddings re-embed. Consistency pass backfills.

2. **Broad `except Exception` on vec KNN** — narrowed to `OperationalError` / `DatabaseError` with `logger.warning`.

3. **Digest re-runs re-archived identical text** — fixed via `content_hash` + `(hash, week)` dedup with metadata merge.

4. **Vec/ORM drift** — `delete_archival` removes vec row; `check_archival_consistency` repairs orphans, missing vec, hash/embedding, and chat_vec backfill (cap 500). Called from lifespan + `POST /api/memory/maintenance`.

5. **`upsert_kra_version` shadowed user edits** — was re-seeding from YAML on every orchestrator/API call. Now seed-only when `KraVersion` empty; `get_active_framework(session)` reads latest DB version. (WS3)

6. **Human-only blocks editable via Memory API** — now 409; UI links to KRA/Weighting editors.

7. **Consolidation O(N²) archival scan** — patched to index by `digest_item_id` via metadata match (still one scan, not nested per-feedback full rescan of updates). Chat volume capped 200×500 chars, max 3 chunks of 80 sharing one `LlmBudget`.

---

## Phase-1 (done)

1. Populate / reuse `ArchivalMemory.embedding`; fix brute-force cost  
2. Narrow vec exception handling  
3. Archival dedup via `content_hash`  
4. Vec consistency + delete_archival + maintenance endpoint  
5. Semantic recall (`chat_vec`) + chat embed-on-write  
6. Consolidation coverage / chunking / metadata patch fix  
7. Block governance (409 human-only; correct `editable` flag)  
8. Compaction (`compact_archival`, per_week_cap=50)  
9. Scoping groundwork (`scope` column + API filters)  
10. Observability UI (consolidation history + Run maintenance)

---

## Phase-2 roadmap

| Item | Why deferred |
|------|----------------|
| Context-pressure summarization in the chat loop | Needs token accounting + mid-conversation summarization policy |
| Importance-weighted archival retrieval | Compaction scores exist; retrieval still pure semantic distance |
| Per-user identity + scoped core blocks | App has no user table / auth; `memory_blocks` PK is global name — needs constraint rebuild |
| Paginated `conversation_search` / Letta tool naming parity | UX polish once multi-user lands |
| Vault/KMS for connector secrets | See ops notes |

---

## Ops notes

- **Connector secrets** are stored plaintext in local SQLite, masked at the API (`••••`+last4). Deliberate demo-grade choice — production needs a vault/KMS.  
- **Scheduler** is single-process APScheduler (in-app). Not safe for multi-worker deployments without an external job runner.  
- **News web search** requires org-level Anthropic enablement of `web_search_20250305`; `news_model` defaults to Sonnet; `max_uses:5` + daily cron bounds spend.  
- **Shared ERP seed** across connectors keeps cross-connector FK/reconciliation consistent; per-connector `seed_offset` intentionally desyncs (demo only).
