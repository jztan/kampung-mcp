# The Kampung server image.
#
# No platform pin on purpose: build it on the host that runs it, so nothing
# goes through QEMU emulation.
FROM python:3.12-slim AS builder

# Pinned so the builder stage is reproducible.
COPY --from=docker.io/astral/uv:0.12.22 /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, so a source change does not reinstall them. uv needs
# every workspace member's pyproject to check the lock, but only the server
# is installed.
COPY pyproject.toml uv.lock ./
COPY packages/server/pyproject.toml packages/server/
COPY packages/client/pyproject.toml packages/client/
RUN uv sync --locked --no-dev --package kampung-server --no-install-workspace

COPY packages/server/src packages/server/src
RUN uv sync --locked --no-dev --package kampung-server --no-editable

FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="Kampung server" \
      org.opencontainers.image.description="Self-hosted messaging between coding-agent sessions on different machines, over MCP" \
      org.opencontainers.image.source="https://github.com/jztan/kampung-mcp" \
      org.opencontainers.image.licenses="AGPL-3.0-only"

# 0.0.0.0 is not a weakening of the loopback default: a container has its
# own network namespace, so binding loopback here would be unreachable even
# from the host. The loopback guarantee lives in the compose port publish.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KAMPUNG_HTTP_HOST=0.0.0.0 \
    KAMPUNG_HTTP_PORT=8000 \
    KAMPUNG_DB_PATH=/data/kampung.db

RUN groupadd --gid 1000 kampung && \
    useradd --uid 1000 --gid kampung --shell /bin/sh --create-home kampung

COPY --from=builder --chown=kampung:kampung /opt/venv /opt/venv
COPY packages/server/LICENSE /usr/share/licenses/kampung-server/LICENSE
COPY NOTICE /usr/share/licenses/kampung-server/NOTICE

# Create and chown /data BEFORE any volume mounts. Docker seeds a fresh named
# volume from the ownership of the image directory it covers, which is what
# keeps SQLite writable as uid 1000 on a Linux host.
RUN mkdir -p /data && chown kampung:kampung /data

USER kampung

# slim has no curl. Without start-interval the first check would wait a full
# interval, so `docker compose up --wait` would stall for 30 seconds.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --start-interval=2s \
    --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

EXPOSE 8000

# Exec form, so `kampung serve` gets SIGTERM and stops quickly.
CMD ["kampung", "serve"]
