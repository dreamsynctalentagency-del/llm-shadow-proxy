# Root-level Dockerfile so build systems / PaaS autodetect find the app
# without extra configuration. The canonical multi-stage build lives at
# docker/Dockerfile; this file is a thin passthrough that reuses it.
#
# For local builds prefer:
#     docker build -f docker/Dockerfile .
#
# syntax=docker/dockerfile:1.7

# ------------------------------------------------------------------------
# Stage 1: build a self-contained virtual environment with all deps.
# ------------------------------------------------------------------------
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      libpq-dev \
      curl \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv "$VIRTUAL_ENV" && pip install --upgrade pip setuptools wheel

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install .

# ------------------------------------------------------------------------
# Stage 2: minimal runtime image.
# ------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    CONFIG_FILE=/app/config/models.yaml \
    DATABASE_URL=sqlite+aiosqlite:////app/data/comparisons.db \
    RAW_STORE_TYPE=filesystem \
    RAW_STORE_FILESYSTEM_PATH=/app/data/raw \
    LOG_LEVEL=INFO \
    LOG_FORMAT=json

RUN apt-get update && apt-get install -y --no-install-recommends \
      libpq5 \
      curl \
      tini \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --create-home --home-dir /home/app app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app config ./config
COPY --chown=app:app src ./src
COPY --chown=app:app ui ./ui

RUN mkdir -p /app/data /app/data/raw && chown -R app:app /app/data

USER app

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "shadow_proxy.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
