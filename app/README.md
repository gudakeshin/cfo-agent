# CFO Agentic Intelligence — Demo

A complete, demo-ready reasoning partner for a CFO of an Indian enterprise. Synthetic ERP data, Deloitte Four Faces KRA framework, dynamic priority weighting, six-step pipeline (Map → Weight → Connect → Filter → Synthesize → Surface), Letta-style persistent memory, weekly digests + red-line alerts, in-app UI + HTML email (outbox fallback).

**Two modes.** The local demo (below) runs on SQLite with simulated ERP data and sign-in off.
A client pilot runs in Docker on PostgreSQL with sign-in and roles, real ERP exports via the
CSV file drop, a separate job worker and PII masking — see **[DEPLOYMENT.md](DEPLOYMENT.md)**.

## Prerequisites

- **Python 3.11+ with loadable SQLite extensions** — Homebrew Python works (`/opt/homebrew/bin/python3`). Apple system / python.org Framework builds will fail at startup (needed for `sqlite-vec`).
- Node 20+ (for frontend build / Vite dev)
- `ANTHROPIC_API_KEY` (required to start the API)

## Quick start

```bash
cd projects/cfo-agent/app
cp .env.example .env
# Edit .env — set a real ANTHROPIC_API_KEY

# Backend (Homebrew Python + uv)
cd backend
uv venv --python /opt/homebrew/bin/python3.13 .venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# Seed ERP + configs + memory blocks (+ download embeddings ~90 MB once).
# With a real API key, also pre-generates the week[-3] digest (e.g. W26) so the inbox isn't empty.
cd ../scripts
python seed.py
# Or: python seed.py --no-digest   # data only
# Or: python seed.py --with-digest # force digest (requires real key)

python demo_reset.py               # clean slate; same digest default as seed
# python demo_reset.py --no-digest
# python demo_reset.py --with-digest

# Run API (single worker — required so APScheduler jobs don't duplicate)
cd ../backend
uvicorn cfo_agent.main:app --host 127.0.0.1 --port 8000

# Frontend (dev) — another terminal
cd ../frontend
npm install
npm run dev
# → http://127.0.0.1:5173 (proxies /api → :8000)
```

**One-process demo** (API serves built UI):

```bash
cd frontend && npm run build
cd ../backend && source .venv/bin/activate
uvicorn cfo_agent.main:app --host 127.0.0.1 --port 8000
# → http://127.0.0.1:8000
```

## Tests

```bash
cd backend && source .venv/bin/activate
pytest -m "not llm"    # deterministic suite (no API cost)
pytest -m llm          # real Claude API tests (pipeline smoke + consolidation)

# Same suite on PostgreSQL + pgvector:
docker run -d --name cfo-pg -e POSTGRES_USER=cfo -e POSTGRES_PASSWORD=test -e POSTGRES_DB=cfo \
  -p 55432:5432 pgvector/pgvector:pg16
TEST_DATABASE_URL=postgresql+psycopg://cfo:test@127.0.0.1:55432/cfo pytest -m "not llm"
```

## Demo script (acceptance walkthrough)

1. `python scripts/demo_reset.py` (with real `.env` key → seeds a W26-ish digest automatically). Or `python scripts/seed.py --with-digest`.
2. Open the app → **Digest Inbox** → generate **week index -2** (e.g. `2026-W27` under Acquisition readiness). Watch the six-step progress.
3. Open a citation on the DSO / acquisition insight → underlying invoices. Expand **Run audit** (score decomposition as face_weight × rank_boost × severity). Use **Resend email** to write/refresh `backend/data/outbox/`.
4. **Weighting** → load **Balance-sheet de-risking** preset (or edit red lines: add/remove level & event rules) → Save → regenerate the **same week**. Rankings/framing shift; both digests stay in the inbox.
5. Mark feedback (acted-on / set-aside + optional note + “already knew”) → **Memory** → Run consolidation → see `cfo_observations` before/after revision panes.
6. `POST /api/alerts/check` with `{"as_of": "<ISO date>"}` where the date is the **Wednesday of your latest data week** (the last option in the inbox week selector; e.g. if the final week is `2026-W28`, use `"2026-07-08"`). Cash red line fires → banner (**Ack** to dismiss) + `backend/data/outbox/alert-*.html`. A second check does not re-alert (episode dedup).
7. Generate final week digest → red lines + continuity. Open digest HTML from outbox. Check **Feedback** for precision + G1 rollup (and note column).
8. Edge: Data Health → regenerate with `stale_days=5` (confirm cascade) → degraded banner. Set risk appetite `high` → "nothing met the bar".

## Architecture sketch

| Layer | Role |
|-------|------|
| KRA framework YAML | Stable Four Faces map (versioned in DB) |
| Priority weighting | Dynamic lens (immutable versions; presets) |
| Synthetic ERP | Seeded quarter via `SourceAdapter` (S1–S5 scenarios), as-of-week metrics, paise integers |
| Pipeline | Deterministic Map/Weight/Filter/Surface; LLM Connect/Synthesize |
| Memory | Core blocks + sqlite-vec archival + chat recall + consolidation |
| Delivery | In-app + email (Resend → SMTP → `data/outbox/`) |

## Important env vars

| Var | Purpose |
|-----|---------|
| `ANTHROPIC_API_KEY` | Required |
| `ANTHROPIC_MODEL` | Default `claude-opus-4-8`; use Haiku for cheap rehearsals |
| `DIGEST_CRON` / `REDLINE_CRON` | APScheduler (IST). Point digest cron 2 min ahead to verify live |
| `RESEND_API_KEY` / `SMTP_*` | Optional; else emails write to `backend/data/outbox/` |

## Layout

```
app/
├── README.md, .env.example
├── backend/          # FastAPI + pipeline + memory
├── frontend/         # Vite React TS
└── scripts/          # seed.py, demo_reset.py
```
