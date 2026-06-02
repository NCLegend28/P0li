# syntax=docker/dockerfile:1.7
# Multi-stage build — keeps the runtime image lean and free of build tools.
# Pinned uv version so rebuilds are reproducible.
# Note: legacy builder (DOCKER_BUILDKIT=0) doesn't expand ARGs inside --from,
# so the uv image tag is hardcoded. Bump both places together.

# ─── Builder stage ────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5.18 /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency files first for layer caching.
COPY pyproject.toml uv.lock ./

# Install dependencies into a project-local .venv (no dev deps, frozen lockfile).
# UV_LINK_MODE=copy because hardlinks break across overlayfs layers.
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
RUN uv sync --frozen --no-dev --no-install-project

# Copy source AFTER deps so source edits don't bust the dep cache.
COPY src/ src/
RUN uv sync --frozen --no-dev

# ─── Runtime stage ────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Non-root user. uid/gid 10001 keeps it well clear of any host user.
RUN groupadd --system --gid 10001 polybot \
 && useradd  --system --uid 10001 --gid polybot --home-dir /app --shell /sbin/nologin polybot

WORKDIR /app

# Copy the prebuilt venv + source from the builder. No build tools in runtime.
COPY --from=builder --chown=polybot:polybot /app/.venv /app/.venv
COPY --from=builder --chown=polybot:polybot /app/src   /app/src

# data/trades is bind-mounted at runtime; create owned by polybot so the
# volume mount inherits the right ownership when it's first created empty.
RUN mkdir -p /app/data/trades && chown -R polybot:polybot /app/data

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER polybot

# Default command runs the scanner; override with `dashboard` for the UI service.
CMD ["scan"]
