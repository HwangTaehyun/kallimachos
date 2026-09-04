#!/usr/bin/env bash
# From distilling sessions to a searchable state — the whole thing, reproducibly.
#
# Why the order is forced:
#   promote  has to place the documents in the vault before lr_extract can read them,
#   and lr_extract has to write lr_kg.json before schema_v3 can fill the graph tables.
#   tune and eval only mean anything after indexing finishes.
#
# Every step is resumable.  Die in the middle and simply run it again.
#   distill    ~/.kal/distilled/.done  per session
#   lr_extract ~/.kal/lr_cache.jsonl   per chunk
#
# Note: embedding (schema_v3) is CPU-bound, so **do not run it alongside** distill or
#       lr_extract.  Running them together produced a measured 10-minute timeout.
set -euo pipefail

# The run is recorded under ~/.kal/runs/ —— so the web screen knows about CLI runs too.  This
# is shell and cannot use `record()`, so this step used to leave **no record at all** (the very
# state run_log.py:4-8 exists to prevent).  It is a trap, so failures and interrupts record too.
_RL_T0=$(date +%s)
_rl_done() {
  _rl_code=$?                       # must be captured on the first line
  "${KAL_PYTHON:-python3}" "$(dirname "$0")/run_log.py" \
      --step rebuild_all --code "$_rl_code" --started "$_RL_T0" 2>/dev/null || true
}
trap _rl_done EXIT

# Every path goes through the environment (docs/STACK.md §2).  This script runs as the web
# UI's "full rebuild" step **inside the container too** —— hardcoding host absolute paths
# would call a darwin binary there, or look for a path that does not exist, and quietly diverge.
# The header must match src/export_all.sh.
PY="${KAL_PYTHON:-python3}"
KAL_HOME="${KAL_HOME:-$HOME/.kal}"
KAL_PATH="${KAL_PATH:-$KAL_HOME/db}"
#  ⚠ No default —— it does not quietly fall back to the author's path (2026-08-25).
#  ⚠ …but it must look **where `just vault` wrote**, not only at the environment.  Every Python
#     entry point resolves through `vault_path.vault()` (environment → ~/.kal/config.json → .env);
#     these two shell scripts were left out of that repair, whose own comment records it as
#     "only `schema_v3` read the config and the other nine did not".  Result: `just index` worked
#     from a clean shell and `just export` died telling the user to set a variable the settings
#     screen had already stored.  Reproduced 2026-09-04.  Ask the same resolver rather than
#     restating the precedence order in shell —— a third copy is how this drifted in the first place.
VAULT="${KAL_VAULT:-$("$PY" "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/vault_path.py" 2>/dev/null || true)}"
[ -n "$VAULT" ] || { echo "  ❌ Please set KAL_VAULT —— the folder holding your notes, or run \`just vault <path>\`." >&2; exit 1; }
export KAL_HOME KAL_PATH
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SRC"

step() { printf '\n\033[1m══ %s\033[0m\n' "$*"; }
SKIP_DISTILL=${SKIP_DISTILL:-0}
SKIP_KG=${SKIP_KG:-0}

if [ "$SKIP_DISTILL" != 1 ]; then
  step "1/6  distil sessions — produce brain-ingest documents"
  $PY distill_sessions.py --workers "${DISTILL_WORKERS:-8}"
fi

step "2/6  promote to the vault — raw/conversations/sessions/ + the wiki corpus page + a commit"
$PY promote_distilled.py ${PROMOTE_ARGS:-}

if [ "$SKIP_KG" != 1 ]; then
  step "3/6  extract the knowledge graph — the whole vault (the same scope as indexing)"
  # When the extraction scope != the indexing scope, those documents are indexed and appear in
  # search while never entering the KG.  They really did diverge once (extraction covered wiki/ and raw/ only, indexing the whole vault).
  $PY lr_extract.py --check-scope
  # The stale-mark resolution arithmetic —— the kind that makes no sound when wrong, so it is checked every run
  $PY test_stale_resolve.py
  # ⚠ `LR_WORKERS` must **not** be set unconditionally here.  `effective_workers()` returns
  #   early on "a person set it, so honour it", so filling in a default here **kills the
  #   relay's `/health` `inflight_max` clamp entirely.**  There is no symptom today only
  #   because both defaults are 8 —— start it with `KAL_RELAY_CONCURRENCY=4` and it still
  #   fires 8, and the queue wait becomes a timeout.  Exactly the situation that docstring
  #   exists to prevent.  A value the user gave passes through untouched.  (r4-fresh, 2026-08-21)
  $PY lr_extract.py
fi

step "4/6  rebuild the knowledge DB — documents · chunks · ix_* · lr_*"
$PY schema_v3.py

step "5/6  tune the weights — the controlled condition + the real-use corpus (condensed)"
$PY tune_alpha.py --split --check-auto --protocol --seeds 20
echo
$PY tune_alpha.py --full-corpus
echo
$PY eval_sessions.py --n 300 --json
echo
$PY ablate_params.py --json

step "6/6  export the graph — the 3D viewer · graphml · json · Obsidian"
#  ⚠ **`--obsidian` writes ~700 notes into the vault, and only an Obsidian vault wants them.**
#     Against an openwiki bundle —— a published git repository —— it drops 3.5 MB of generated
#     pages into something that goes out.  `export_all.sh` was given this guard on 2026-09-04 and
#     **this file was not**, so the two scripts answered the same question differently for a day.
#     (codex adversarial review 2026-09-04, finding #2 —— reproduced with KAL_PYTHON=/usr/bin/true)
if [ -d "$VAULT/.obsidian" ]; then
  $PY export_graph.py --min-degree 2 --max-nodes 700 \
      --obsidian "$VAULT/kg"
else
  echo "   no .obsidian/ — the vault-notes projection is skipped (not an Obsidian vault)"
  $PY export_graph.py --min-degree 2 --max-nodes 700
fi
# viewer/ is a host convenience, not a pipeline artifact.  It is not mounted in the container,
# so set -e killed the whole thing here (export_all.sh already guards it for the same reason).
if [ -f "$KAL_HOME/graph_export/graph3d.html" ] && [ -d "$SRC/../viewer" ]; then
  cp "$KAL_HOME/graph_export/graph3d.html" "$SRC/../viewer/graph3d.html"
fi

# The galaxy view is built from the galaxy-view fork (**the same source** as the plugin).
# The old export_webgl.py was a separate hand-written renderer with a different UI and was
# retired — the file remains but the pipeline does not call it.  Calling it overwrites the artifacts below.
$PY export_kal_graph.py                  # → .obsidian/plugins/kal-galaxy/kal-graph.json
# node esbuild.web.mjs      → ../viewer/galaxy.html (a self-contained HTML)
# node esbuild.config.mjs   → dist/ (the Obsidian plugin), then copied into the vault
# plugin/ is in .dockerignore and does not enter the image —— without the guard,
# `cd ../plugin` fails in the container and the whole rebuild ends here.
if [ -d "$SRC/../plugin/node_modules" ]; then
(
  cd "$SRC/../plugin" &&
  node esbuild.web.mjs &&
  node esbuild.config.mjs production
)
#  ⚠ Install into the vault only if it **is** an Obsidian vault —— `mkdir -p` would otherwise
#     create `.obsidian/plugins/` inside a bundle that gets published.  Same gate as above.
if [ -d "$VAULT/.obsidian" ]; then
  mkdir -p "$VAULT/.obsidian/plugins/kal-galaxy" &&
  cp "$SRC/../plugin/dist/main.js" "$SRC/../plugin/dist/manifest.json" \
     "$SRC/../plugin/dist/styles.css" "$VAULT/.obsidian/plugins/kal-galaxy/"
else
  echo "   the vault has no .obsidian/ — plugin install skipped"
fi
else
  echo "   no plugin/node_modules — skipping the bundle rebuild (normal in a container)"
fi

step "verify the documented numbers"
# Copying numbers by hand goes wrong — it did three times.  A machine catches it.
DOCS_OK=1
$PY verify_docs.py || DOCS_OK=0
[ "$DOCS_OK" = 1 ] || echo "  ⚠ the documented numbers disagree with reality (see the list above)"

step "done"
$PY - <<'PYEOF'
import collections, os
import lancedb
# The same reason as above: no hardcoded paths.  KAL_PATH/KAL_HOME were exported above.
db = lancedb.connect(os.environ.get("KAL_PATH") or
                     os.path.join(os.environ.get("KAL_HOME") or
                                  os.path.expanduser("~/.kal"), "db"))
D = db.open_table("documents").search().limit(999999).to_list()
c = collections.Counter(r.get("origin", "vault") for r in D)
t = collections.Counter(r.get("doc_type", "") for r in D if r.get("doc_type"))
for name in sorted(db.list_tables().tables):
    print(f"  {name:<14}{db.open_table(name).count_rows():>12,} rows")
print(f"\n  origin   {dict(c)}")
print(f"  doc_type {dict(t)}")
PYEOF

# The warning above gets buried in a long log.  It appears once more on the last line —— getting
# this far means the rebuild itself succeeded, so the exit code is 0 (the numbers are a person's job).
[ "$DOCS_OK" = 1 ] || printf '\n\033[1;33m  ⚠ the documented numbers disagree with reality — python verify_docs.py --fix\033[0m\n'

