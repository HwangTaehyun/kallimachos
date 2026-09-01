#!/usr/bin/env bash
#  The python stand-in `just dc <recipe>` uses.
#
#  A recipe calls `{{py}} {{src}}/x.py <args>`.  This takes that verbatim and runs it inside
#  the container —— a thin layer that avoids maintaining two sets of recipes.
#
#  ⚠ Without `-T`, TTY allocation breaks the pipe.
#  ⚠ The data is **a host bind**.  With a named volume, what the container indexes is invisible
#    to the host CLI and MCP (an empty result, with no error).
set -euo pipefail
KAL_DIR="${KAL_DIR:-$HOME/.kal}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

#  ⚠ **Nothing may be guessed here.**  The old default `dirname($REPO)` becomes **the workspace
#     root** when this repository sits inside a monorepo —— measured (2026-08-28):
#     that root held six private documents belonging to another repository.
#     `just dc extract` sends them through `claude -p`.
#     They are exactly the files the whole public-boundary apparatus exists to protect.
#     The judgement is made in `vault_path.py` alone —— the same rule as justfile's `_vault`.
VAULT_DIR="$("$REPO/.venv/bin/python" "$REPO/src/vault_path.py" 2>/dev/null \
             || python3 "$REPO/src/vault_path.py")" || {
  echo "  ❌ Could not determine the vault —— set it with just vault <path>." >&2; exit 1; }
"$REPO/.venv/bin/python" "$REPO/src/vault_path.py" --check "$VAULT_DIR" >&2 \
  || python3 "$REPO/src/vault_path.py" --check "$VAULT_DIR" >&2 || exit 1

#  ⚠ The vault must be **writable**.  The old comment said "the pipeline does not write here",
#     which is not true —— `promote_distilled.py` deletes, rewrites and commits documents
#     inside the vault, and `fm_migrate.py` and `remask_docs.py` edit in place.
#     Left as `:ro`, those steps fail silently in the container alone.
#     compose's api being rw is right and this was wrong.
exec docker run --rm -i \
  --user "$(id -u):$(id -g)" \
  -v "$KAL_DIR:/data" \
  -v "$VAULT_DIR:/vault" \
  -v "$REPO/src:/app/src:ro" \
  -v "$REPO/aliases.yml:/app/aliases.yml:ro" \
  -v "$REPO/homonyms.yml:/app/homonyms.yml:ro" \
  -e KAL_VAULT=/vault \
  -e KAL_HOME=/data \
  -e KAL_SKIP="${KAL_SKIP:-}" \
  -e KAL_NO_LLM="${KAL_NO_LLM:-}" \
  -e KAL_CLAUDE_RELAY="${KAL_CLAUDE_RELAY:-}" \
  -e KAL_RELAY_TOKEN="${KAL_RELAY_TOKEN:-}" \
  --add-host host.docker.internal:host-gateway \
  kal:local "$@"
