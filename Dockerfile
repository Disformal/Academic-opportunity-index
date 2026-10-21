# ─── AOI Application Image ────────────────────────────────────────────────────
# Multi-stage build: install dependencies in builder, copy to slim runtime.
# Uses uv for fast, reproducible installs.

# ── Stage 1: builder ─────────────────────────────────────────────────────────
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first (cached unless pyproject.toml or uv.lock change)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy application code and install the project itself
COPY . .
RUN uv sync --frozen --no-dev


# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.13-slim AS runtime

# curl for healthcheck, supercronic for the scheduler service
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# supercronic — cron replacement designed for containers (stdout logging,
# graceful SIGTERM, no syslog). Used by the scheduler service; ignored by app.
ARG TARGETARCH=amd64
RUN SUPERCRONIC_ARCH=$([ "$TARGETARCH" = "arm64" ] && echo "linux-arm64" || echo "linux-amd64") \
    && curl -fsSL "https://github.com/aptible/supercronic/releases/download/v0.2.33/supercronic-${SUPERCRONIC_ARCH}" \
       -o /usr/local/bin/supercronic \
    && chmod +x /usr/local/bin/supercronic

# Non-root user for runtime security
RUN useradd --create-home --shell /bin/bash aoi

WORKDIR /app

# Copy the virtual environment and application code from builder
COPY --from=builder --chown=aoi:aoi /app /app

USER aoi

# Add the virtual environment to PATH
ENV PATH="/app/.venv/bin:$PATH"

# Alembic needs alembic.ini and the migrations directory
# (already copied as part of COPY . . in builder)

EXPOSE 8000

# Health check: hit the search endpoint
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD curl -sf http://localhost:8000/health || exit 1

# Run uvicorn directly — not through uv, since we have the venv on PATH
CMD ["uvicorn", "aoi.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
