FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install uv
# Pinned, not :latest. This image's build now depends on
# --no-install-project and on uv's editable-install behaviour, so the tool
# that provides them is not a floating tag.
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

# Copy dependency files. README.md belongs here despite being docs: pyproject
# declares it as the project readme, so hatchling reads it during uv sync.
COPY pyproject.toml uv.lock README.md ./

# Dependencies only, in their own layer so it caches independently of src/.
# --no-install-project matters: without it uv installs cortex-utils here, while
# src/ does not exist yet, and the result is a venv whose editable install
# points at nothing.
RUN uv sync --frozen --no-dev --no-install-project

# Copy source code
COPY src/ ./src/

# NOW install the project itself, with its source present. This is what makes
# /app/.venv a complete, runnable environment at build time rather than
# something `uv run` has to finish at boot.
RUN uv sync --frozen --no-dev

# Create non-root user
RUN useradd --create-home --shell /bin/bash appuser
USER appuser

# THE BAKED VENV, NOT `uv run`. `uv run` re-resolves the project on a
# container's first start and fetches hatchling from pypi to do it, so every
# fresh container needed working DNS -- and a redeploy makes fresh ones. A
# container DNS failure on ares stopped cortex-school starting on 2026-10-02;
# this image runs the alerter, so the same failure here takes out the thing
# that would have reported it.
ENTRYPOINT ["/app/.venv/bin/cortex-utils"]
