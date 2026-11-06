# CFO Agentic Intelligence — one image for the API and the job worker.
#   docker compose up -d        (see docker-compose.yml and DEPLOYMENT.md)

# ── 1. Build the web UI ──────────────────────────────────────────────────────
FROM node:22.12-bookworm-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ── 2. Python app ────────────────────────────────────────────────────────────
FROM python:3.13-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    FASTEMBED_CACHE_PATH=/opt/models \
    FRONTEND_DIST=/app/frontend/dist \
    CSV_INBOX_DIR=/data/inbox \
    PATH=/app/backend/.venv/bin:$PATH
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
# DejaVu Sans has the ₹ glyph for PDF exports
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
RUN uv sync --frozen --no-dev \
 && rm -rf data tests .pytest_cache

# Bake the embedding model in: no download (and no outbound internet) at runtime.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/all-MiniLM-L6-v2', cache_dir='/opt/models')"

COPY --from=ui /ui/dist /app/frontend/dist
COPY scripts/ /app/scripts/

RUN useradd --system --uid 10001 --home /app cfo \
 && mkdir -p /app/backend/data/outbox /data/inbox \
 && chown -R cfo /app/backend/data /data /opt/models
USER cfo

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"
CMD ["uvicorn", "cfo_agent.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]
