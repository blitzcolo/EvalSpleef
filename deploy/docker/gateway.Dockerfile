# Multi-stage Dockerfile for evalspleef-gateway
# Installs workspace packages using uv from repository
FROM ghcr.io/astral-sh/uv:0.5.11 AS uv-bin

FROM python:3.12-slim-bookworm AS builder

# Install uv binary
COPY --from=uv-bin /uv /uvx /bin/

# Environment for uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /workspace

# Copy workspace definition and locks
COPY pyproject.toml uv.lock ./
# Copy workspace packages and adapters
COPY packages/ ./packages/

# Sync virtualenv with required packages (without dev dependencies)
RUN uv sync --frozen --no-dev --package evalspleef-gateway

# --- Final Runtime Image ---
FROM python:3.12-slim-bookworm AS runtime

# Security: non-root user
RUN groupadd -g 10001 spleef && \
    useradd -u 10001 -g spleef -m -s /bin/bash spleef

# Copy virtual environment
COPY --from=builder --chown=spleef:spleef /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SPLEEF_GATEWAY_HOST=0.0.0.0 \
    SPLEEF_GATEWAY_DATA_PORT=8390 \
    SPLEEF_GATEWAY_ADMIN_PORT=8391

USER spleef
WORKDIR /home/spleef

EXPOSE 8390 8391

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8390/healthz').read()" || exit 1

ENTRYPOINT ["spleef-gateway"]
