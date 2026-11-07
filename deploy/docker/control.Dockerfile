# Multi-stage Dockerfile for evalspleef-control
# Installs workspace packages using uv from repository
FROM ghcr.io/astral-sh/uv:0.5.11 AS uv-bin

FROM python:3.12-slim-bookworm AS builder

COPY --from=uv-bin /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /workspace

COPY pyproject.toml uv.lock ./
COPY packages/ ./packages/

RUN uv sync --frozen --no-dev --package evalspleef-control

# --- Final Runtime Image ---
FROM python:3.12-slim-bookworm AS runtime

RUN groupadd -g 10001 spleef && \
    useradd -u 10001 -g spleef -m -s /bin/bash spleef

COPY --from=builder --chown=spleef:spleef /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER spleef
WORKDIR /home/spleef

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python3 -c "import evalspleef.control" || exit 1

ENTRYPOINT ["spleef"]
CMD ["--help"]
