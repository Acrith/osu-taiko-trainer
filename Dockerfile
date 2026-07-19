# taiko-trainer production image.
#
# Multi-stage: `builder` compiles + resolves deps with uv, `runtime` copies
# only the resolved venv + source. Result is a small image (~200 MB) that
# doesn't ship the toolchain.
#
# Runs as a non-root user (uid 1000) and expects a workspace directory
# mounted at /workspace where SQLite files live. Env-driven config for
# TAIKO_TRAINER_MODE, OSU_OAUTH_*, SESSION_SECRET — see README-deploy.md.

# ---------- builder ----------
FROM python:3.12-slim AS builder

# Install uv (fast, deterministic dependency resolver)
COPY --from=ghcr.io/astral-sh/uv:0.5.4 /uv /usr/local/bin/uv

WORKDIR /app
# Copy only dep manifests first so pip's layer caches across code changes
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy source + install the project itself
COPY src ./src
RUN uv sync --frozen --no-dev

# ---------- runtime ----------
FROM python:3.12-slim AS runtime

# Non-root user. UID/GID 1000 so bind mounts from a typical Linux host
# don't have permission mismatches.
RUN groupadd --gid 1000 app && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash app

# curl is only for the HEALTHCHECK — 4 MB, worth it.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

# Workspace is a mount point; the container has no persistent data of its own.
RUN mkdir -p /workspace && chown -R app:app /workspace /app
USER app

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    TAIKO_TRAINER_MODE=web

# The uvicorn socket. Cloudflare Tunnel / a reverse proxy fronts this.
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/status || exit 1

CMD ["taiko-trainer", "serve", "--ws", "/workspace", "--host", "0.0.0.0", "--port", "8000"]
