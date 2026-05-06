# ============================================================================
# Multi-stage Dockerfile — Compliance Automation Platform
# ============================================================================
# Build:  docker build -t compliance-system .
# Run:    docker run --env-file .env compliance-system
# ============================================================================

# ── Stage 1: Base ────────────────────────────────────────────────────────────
FROM python:3.11-slim AS base

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# ── Stage 2: Dependencies ────────────────────────────────────────────────────
FROM base AS deps

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ── Stage 3: Application ────────────────────────────────────────────────────
FROM deps AS app

# Copy source code
COPY config.py main.py ./
COPY audit/ ./audit/
COPY ingestion/ ./ingestion/
COPY parser/ ./parser/
COPY llm/ ./llm/
COPY routing/ ./routing/
COPY validation/ ./validation/
COPY workflow/ ./workflow/
COPY utils/ ./utils/

# Create non-root user for security
RUN groupadd -r compliance && useradd -r -g compliance -d /app compliance && \
    mkdir -p /app/reports && chown -R compliance:compliance /app

USER compliance

# Default report output directory
ENV REPORT_OUTPUT_DIR=/app/reports

# Health check (liveness probe)
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import main; print('ok')" || exit 1

# Entry point
ENTRYPOINT ["python", "main.py"]
