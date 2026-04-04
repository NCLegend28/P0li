FROM python:3.12-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies (frozen — matches uv.lock exactly)
RUN uv sync --frozen --no-dev

# Copy source
COPY src/ src/

# data/trades is mounted as a volume at runtime
RUN mkdir -p data/trades

ENV PYTHONPATH=src \
    PYTHONUNBUFFERED=1

# Default command is the scanner; override with `dashboard` for the dashboard service
CMD ["uv", "run", "scan"]
