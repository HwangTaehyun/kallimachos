# kal — the knowledge DB pipeline and the MCP server in one.
#
# This one image does all three:
#   index    docker run … just index
#   search   docker run … just search "a query"
#   MCP      docker run -i … python src/kal_mcp.py      ← stdio, so -i is required
#
# ⚠ **The model weights are baked in at build time.**  Otherwise the first search makes a
#   round trip to huggingface and spends 13 seconds, and offline it stops dead (measured 13.2s → 4.7s).
FROM python:3.13-slim AS base

# The lancedb and pyarrow wheels need no compiler, but the tokenizers that
# sentence-transformers drags in sometimes does.  This is slim, so put one in up front.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Bring uv in (just the binary, from the official image —— the runner image stays as it is)
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv

# Dependencies first —— this layer is reused when only the source changes.
#
#  ⚠ **`uv.lock` is the source of truth.**  This used to carry a two-step trick: "install
#    torch from the CPU index first, then `-r requirements.txt`".  With the trick living
#    only in the Dockerfile, **the host venv never got its benefit**, and the two could end
#    up on different torches.  `[[tool.uv.index]]` in `pyproject.toml` now holds that as a
#    declaration, so the host and the container use the same lock.
#
#    (The reason for leaving CUDA out is unchanged: measured, `torch.cuda.is_available()`
#     is False, and yet `site-packages/nvidia` (2.9GB) and `triton` (652MB) came along ——
#     3.5GB of a 6.53GB image that would never run, piling up as dangling layers on every
#     rebuild until the Docker VM disk hit 100% and LanceDB died with no room for its /tmp spill.  2026-08-21)
COPY pyproject.toml uv.lock ./
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
#  ⚠ The cache is given as a **mount**.  Otherwise the wheels uv fetched set in `$HOME/.cache/uv`
#    and ride into the image —— measured (2026-08-24): of a 2.77GB layer the venv was 1.6GB
#    and the other **1.3GB was cache** (2.26GB → 3.56GB).  Taking it out as a mount removes
#    it from the image and makes rebuilds faster rather than slower.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
ENV PATH="/opt/venv/bin:$PATH"

# Bake the embedding model into the image.  This one line removes the runtime network dependency.
ENV HF_HOME=/opt/hf
RUN python -c "\
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('intfloat/multilingual-e5-small')" \
    && chmod -R a+rX /opt/hf

COPY src/ ./src/
# The aliases and homonyms a person confirmed.  entity_resolve's default path is `src/../`,
# so they have to be here (/app) —— without them it quietly becomes an empty dict
# (entity_resolve.load_aliases) and the graph is built with no alias applied at all.
COPY aliases.yml homonyms.yml ./
COPY skills/ ./skills/
COPY .claude-plugin/ ./.claude-plugin/
COPY justfile README.md ./

# Offline at runtime too —— it uses the baked weights and does not go looking for an update.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    PYTHONUNBUFFERED=1 \
    KAL_PATH=/data/db \
    KAL_VAULT=/vault \
    KAL_HOME=/data

# Non-root.  Mounting the vault read-only is recommended.
RUN useradd -u 1000 -m kal && mkdir -p /data && chown kal /data
USER kal
#  ⚠ `VOLUME ["/data"]` is **deliberately not declared.**
#     Declaring it makes `docker run --read-only` **not cover that path** —— an anonymous
#     volume attaches rw, and what was written there disappears silently with `--rm`.
#     Measured (2026-08-23, with every hardening flag on):
#         touch /data/probe → succeeded · mount → /dev/vda1 on /data type ext4 (rw)
#     MCP only ever sees `/data/db` read-only, so it needs no volume declaration.
#     To run the pipeline, the caller attaches it directly with `-v <host>:/data`.
#     (deep review 2026-08-23, security lens)

# The default is the **MCP server**.  It is stdio, so attach with `docker run -i`.
# To run the pipeline, override the command: `docker run … python src/schema_v3.py`
ENTRYPOINT ["python"]
CMD ["src/kal_mcp.py"]
