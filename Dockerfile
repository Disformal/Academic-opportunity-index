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

# The old web app was removed in M3 (ADR 0032). M9 runs `aoi scheduler` and `aoi worker` from this
# image as services; until then the default command only lists the CLI (the venv is on PATH).
CMD ["aoi", "--help"]
