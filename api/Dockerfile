# The API and the pipeline runner.
#
# Why one image
#   Go only orchestrates; the actual work is Python's (LanceDB, torch,
#   sentence-transformers).  Splitting the containers would mean putting another HTTP hop
#   between them, and since all Go does is "start a process and read stdout", there is
#   nothing to gain.  Instead the layers are split so a Go-only edit does not refetch torch.
#
# Why node is in here too
#   lr_extract extracts entities with `claude -p`, and that CLI is node.
#   Authentication comes from mounting ~/.claude, so it uses the host's (see compose).

# The build context is the **repository root** (src/, pyproject.toml, uv.lock and aliases.yml go in too).
# That is why the Go source paths are prefixed with api/.
# ── The Go build ──────────────────────────────────────────────────────
FROM golang:1.25-alpine AS gobuild
WORKDIR /build
COPY api/go.mod ./
RUN go mod download
COPY api/*.go ./
# With CGO off, a binary built on alpine runs unchanged on the debian runtime
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/api .

# ── The Python dependencies ───────────────────────────────────────────
# Dependencies alone first —— so a source change does not refetch torch.
FROM python:3.13-slim AS pydeps
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv

#  ⚠ **`uv.lock` is the source of truth.**  This used to carry a two-step trick: "install
#    torch from the CPU index first, then `-r requirements.txt`", and the same trick was
#    **copied** into the root Dockerfile.  Fix one of the two and the images end up on
#    different torches.  `[[tool.uv.index]]` in `pyproject.toml` now holds that as a
#    declaration, so the host and both images use the same lock.
#
#    (The reason for leaving CUDA out is unchanged: measured, `torch.cuda.is_available()`
#     is False, yet `site-packages/nvidia` (2.9GB) and `triton` (652MB) came along —— 3.5GB
#     of a 6.53GB image that would never run, piling up as dangling layers on every rebuild
#     until the Docker VM disk hit 100% and LanceDB died with no room for its /tmp spill.  2026-08-21)
COPY pyproject.toml uv.lock /tmp/proj/
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
#  ⚠ The cache is given as a **mount**.  Otherwise the wheels uv fetched set in `$HOME/.cache/uv`
#    and ride into the image —— measured (2026-08-24): of a 2.77GB layer the venv was 1.6GB
#    and the other **1.3GB was cache** (2.26GB → 3.56GB).  Taking it out as a mount removes
#    it from the image and makes rebuilds faster rather than slower.
RUN --mount=type=cache,target=/root/.cache/uv \
    cd /tmp/proj && uv sync --frozen --no-dev --no-install-project

# ── The runtime ───────────────────────────────────────────────────────
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:/usr/local/bin:$PATH" \
    KAL_SRC=/app/src \
    KAL_PYTHON=/opt/venv/bin/python \
    KAL_HOME=/data/kal \
    HOME=/data/kal/container-home \
    KAL_PATH=/data/kal/db

RUN apt-get update && apt-get install -y --no-install-recommends \
      bash git curl ca-certificates nodejs npm \
 && npm install -g @anthropic-ai/claude-code \
 && npm cache clean --force \
 && apt-get purge -y npm && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

COPY --from=pydeps /opt/venv /opt/venv
COPY --from=gobuild /out/api /usr/local/bin/sb-api

WORKDIR /app
COPY src/ /app/src/
# Both go in.  compose mounts the host files over these (a person edits them through the UI),
# but running from the image alone without homonyms makes homonym splitting quietly disappear.
COPY aliases.yml homonyms.yml /app/

# It does not run as root.  Using the mounted ~/.kal needs a matching uid, so compose
# passes user in (`user: "${UID}:${GID}"`).
# The uid is decided at run time from .env, so a build-time chown cannot match it.
# The mount points are therefore left **writable by anyone** —— the real access control
# is the host-side permission on ~/.kal (700).
RUN useradd -m -u 1000 app \
 && mkdir -p /data/kal/container-home \
 && chmod -R 0777 /data
USER app

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1

CMD ["sb-api"]
