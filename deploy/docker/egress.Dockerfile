# Multi-stage Dockerfile for evalspleef-egress
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

# --no-editable: workspace members must be copied into the venv, because the runtime
# stage does not carry /workspace/packages.
RUN uv sync --frozen --no-dev --no-editable --package evalspleef-egress

# --- Final Runtime Image ---
FROM python:3.12-slim-bookworm AS runtime

RUN groupadd -g 10001 spleef && \
    useradd -u 10001 -g spleef -m -s /bin/bash spleef && \
    mkdir -p /var/log/evalspleef && \
    chown -R spleef:spleef /var/log/evalspleef

COPY --from=builder --chown=spleef:spleef /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER spleef
WORKDIR /home/spleef

EXPOSE 3128

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python3 -c "import socket; s = socket.socket(); s.connect(('127.0.0.1', 3128)); s.close()" || exit 1

ENTRYPOINT ["spleef-egress"]
CMD ["--help"]
