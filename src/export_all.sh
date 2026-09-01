#!/usr/bin/env bash
# **Every** export.  The UI's "export the graph" step calls this.
#
# Why a script —— status.py's STEPS.cmd holds a single command.  But what this step promises in
# writes is 4 artifacts (graphml · graph3d.html · kal-graph.json · the plugin), and cmd used to
# be `export_kal_graph.py` alone, so **only kal-graph.json** came out.
# As a result graph3d.html was never updated through the UI, and status's artifact freshness
# check kept showing `graph_export` as stale —— a warning no amount of button-pressing could
# clear.  (pipeline check, 2026-08-19)
#
# The order and the arguments **must match** refresh_kg.py's export block.  Diverge and the
# artifacts differ depending on which one was run.
set -euo pipefail

# The run is recorded under ~/.kal/runs/ —— so the web screen knows about CLI runs too.  This
# is shell and cannot use `record()`, so this step used to leave **no record at all** (the very
# state run_log.py:4-8 exists to prevent).  It is a trap, so failures and interrupts record too.
_RL_T0=$(date +%s)
_rl_done() {
  _rl_code=$?                       # must be captured on the first line
  "${KAL_PYTHON:-python3}" "$(dirname "$0")/run_log.py" \
      --step export --code "$_rl_code" --started "$_RL_T0" 2>/dev/null || true
}
trap _rl_done EXIT

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${KAL_PYTHON:-python3}"
KAL_HOME="${KAL_HOME:-$HOME/.kal}"
#  ⚠ No default —— it does not quietly fall back to the author's path (2026-08-25).
VAULT="${KAL_VAULT:-}"
[ -n "$VAULT" ] || { echo "  ❌ Please set KAL_VAULT —— the folder holding your notes." >&2; exit 1; }

cd "$HERE"

echo "① the Obsidian graph + graphml + graph3d.html"
"$PY" export_graph.py --min-degree 2 --max-nodes 700 --obsidian "$VAULT/kg"

# Copied so the viewer opens from inside the repository too.
#
# ⚠ **The target directory has to be checked too.**  A container has no viewer/ mount, so
# `/app/src/../viewer` does not exist and set -e killed the whole thing here —— after which
# export_kal_graph.py never ran at all and it ended "failed".  This copy is a host convenience,
# not a required pipeline artifact.  (2026-08-19)
if [ -f "$KAL_HOME/graph_export/graph3d.html" ] && [ -d "$HERE/../viewer" ]; then
    cp "$KAL_HOME/graph_export/graph3d.html" "$HERE/../viewer/graph3d.html"
    echo "   viewer/graph3d.html updated"
else
    echo "   no viewer/ — copy skipped (normal in a container)"
fi

echo "② kal-graph.json, which the plugin and the web read"
"$PY" export_kal_graph.py

# ③ The galaxy view bundle.  The same work in the same order as refresh_kg.py:150-154
#    (the "must not diverge" rule at :11).
#
# ⚠ **Host only.**  plugin/ is in .dockerignore so it does not enter the image, and neither
#   compose file mounts it.  So in a container (the web UI's "export the graph") this is where
#   it ends, and there are 3 artifacts —— status.py's writes has to say so.
if [ -d "$HERE/../plugin/node_modules" ]; then
    echo "③ the galaxy view bundle (the plugin)"
    (
      cd "$HERE/../plugin" &&
      node esbuild.web.mjs &&
      node esbuild.config.mjs production &&
      mkdir -p "$VAULT/.obsidian/plugins/kal-galaxy" &&
      cp dist/main.js dist/manifest.json dist/styles.css \
         "$VAULT/.obsidian/plugins/kal-galaxy/"
    )
else
    echo "③ no plugin/node_modules — skipping the bundle rebuild (normal in a container)"
fi

echo "✅ every export finished"
