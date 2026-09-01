#!/usr/bin/env python3
"""Restore only the stale parts of the knowledge graph.

Why it is needed
  sync_v3.py updates chunks, vectors and the inverted index immediately but cannot touch
  entities and relations (that needs LLM extraction).  Instead it writes "these documents have
  a stale KG" into stale_docs.  There was no command that actually restored them, so every
  time meant running rebuild_all.sh wholesale or typing four scripts in order.  This removes that step.

What it saves and what it does not — honestly
  Saves      LLM extraction.  lr_extract's cache is keyed on **the chunk content hash**, so an
             unchanged chunk is never called.  Only changed documents' chunks are called anew.
  Does not   the graph build.  Entity merging is global — one changed document means its
             entities have to be merged against every other document's again.  A partial build
             does not hold.  Measured at 88 seconds, so there is nothing worth saving.
  That is, this command's benefit is not "fewer LLM calls" (the cache already does that) but
  **"it is one line, and the stale marks are reliably tidied afterwards"**.

Usage:
  python refresh_kg.py --check      report the stale documents and stop (changes nothing)
  python refresh_kg.py              extract → build the graph → export
  python refresh_kg.py --no-export  skip regenerating the graph artifacts
"""
import vault_path
import argparse
import collections
import os
import subprocess
import sys
import time

import lancedb


# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
DB = os.environ.get("KAL_PATH", os.path.join(KAL_HOME, "db"))
# ⚠ The venv under KAL_HOME must not be used as is —— in the container KAL_HOME is the mounted
# host ~/.kal and the python inside it is a darwin binary (it will not even execute).
# The same rule as export_all.sh:16 —— KAL_PYTHON first, else the interpreter running now.
PY = os.environ.get("KAL_PYTHON") or sys.executable
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.abspath(os.path.join(HERE, "..", "plugin"))
# The vault path goes through the environment too (docs/STACK.md §2).  In the container it is /vault.
VAULT = vault_path.vault()
VAULT_PLUGIN = os.path.join(VAULT, ".obsidian/plugins/kal-galaxy")

# Past this ratio a rebuild is recommended.
#
# ⚠ The grounds are weaker than first thought.  The loss figures for 20%/50% staleness came
#   from **a single draw**, and re-measured across 8 seeds the seed range (0.055–0.065) is
#   comparable to the loss itself (adversarial review 2026-08-18).  So "-0.029 at 20%" cannot be stated with that precision.
#
# It stays at 20% for **cost** rather than loss size —— a rebuild is about 2 minutes on a warm
# cache, so running it often is cheap, and even when stale, BM25 and chunk vectors still find
# the document, so it is not an irreversible loss.  There is no reason to pick the threshold precisely here.
# The full grounds are in docs/PIPELINE.md §KG refresh.
WARN_RATIO = 0.20


def stale_state(db):
    """→ (a dict of the latest reason per document, the total document count).  A document recorded several times counts once.

    sync_v3.mark_stale appends on every run — edit the same document twice and there are two
    rows.  Counting rows directly inflates the stale ratio.
    """
    total = db.open_table("documents").count_rows()
    try:
        rows = db.open_table("stale_docs").search().limit(999999).to_list()
    except Exception:
        return {}, total
    latest = {}
    for r in sorted(rows, key=lambda x: x.get("marked_at", 0)):
        latest[r["doc_id"]] = r
    return latest, total


def report(latest, total):
    n = len(latest)
    ratio = n / total if total else 0.0
    by = collections.Counter(r["reason"] for r in latest.values())
    print(f"  stale documents {n} / {total}  =  {ratio*100:.1f}%"
          + (f"   ({' · '.join(f'{k} {v}' for k, v in by.most_common())})" if by else ""))
    if n:
        newest = max(r.get("marked_at", 0) for r in latest.values())
        print(f"  most recent mark {time.strftime('%Y-%m-%d %H:%M', time.localtime(newest))}")
        for r in sorted(latest.values(), key=lambda x: -x.get("marked_at", 0))[:10]:
            print(f"    {r['reason']:<9}{r['path']}")
        if n > 10:
            print(f"    … and {n - 10} more")
    if ratio >= WARN_RATIO:
        print(f"\n  ⚠ past {WARN_RATIO*100:.0f}% — a refresh is recommended "
              f"(measured: nDCG@10 -0.029 at 20% stale)")
    return ratio


def run(cmd, cwd=None):
    print(f"\n$ {' '.join(cmd)}", flush=True)
    p = subprocess.run(cmd, cwd=cwd)
    if p.returncode != 0:
        sys.exit(f"failed: {' '.join(cmd)} (exit {p.returncode})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report the state and exit")
    ap.add_argument("--no-export", action="store_true", help="skip regenerating the graph artifacts")
    ap.add_argument("--force", action="store_true", help="run even with no stale documents")
    ap.add_argument("--aliases-only", action="store_true",
                    help="skip extraction and only rebuild the index and the graph JSON.  "
                         "For applying changes to aliases.yml")
    a = ap.parse_args()

    db = lancedb.connect(DB)
    latest, total = stale_state(db)
    ratio = report(latest, total)

    if a.check:
        # Reported through the exit code — cron and hooks use `refresh_kg.py --check || refresh_kg.py`
        sys.exit(1 if ratio >= WARN_RATIO else 0)

    if not latest and not a.force and not a.aliases_only:
        print("\n  Nothing to refresh.  To force it, pass --force")
        return

    #  From here is **the real work** —— the record is written only here.
    #
    #  ⚠ It must not wrap the whole of `main()`.  `--check` is a read-only probe that signals
    #    staleness with `sys.exit(1)` (cron uses `--check || refresh_kg`).  `record()` records
    #    the SystemExit code verbatim, so wrapping it prints **"failed" on screen every time
    #    staleness is reported.**  Written that way at first and noticed.
    #    The "nothing to refresh" early return is likewise not something to record.
    from run_log import record
    #  `--aliases-only` is **a different step** in status.py (`apply_aliases`).
    #  Recording it under the same name would leave that row on screen permanently empty.
    with record("apply_aliases" if a.aliases_only else "refresh_kg"):
        return _do_refresh(a)


def _do_refresh(a):
    t0 = time.time()
    if a.aliases_only:
        # Aliases are applied by build_canon, and **both** lr_extract and schema_v3 call it.
        # For a newly written alias the two spellings are still separate in lr_kg.json, so
        # schema_v3's call to build_canon (defined at entity_resolve.py:186, called inside
        # schema_v3.build_graph() at schema_v3.py:550) merges them on its own —— extraction
        # need not run again.  (measured 2026-08-19: all 6 undeclared candidate pairs existed
        # in lr_kg.json under both spellings)
        #
        # What is lost instead: schema_v3 calls no LLM, so a newly merged entity's description
        # becomes stitched fragments (schema_v3.py:590).  Running lr_extract has the LLM
        # rewrite it as one paragraph (FORCE_LLM_SUMMARY_ON_MERGE).
        print("  aliases only — skipping extraction (①)\n")
    else:
        # ① LLM extraction — the cache is keyed on the chunk hash, so only changed chunks are really called
        run([PY, "lr_extract.py"], cwd=HERE)
    # ② the graph build (global merge + vector regeneration).  Afterwards schema_v3 drops stale_docs
    run([PY, "schema_v3.py"], cwd=HERE)

    if a.aliases_only:
        # Only the graph JSON the galaxy view reads is rebuilt.  graphml, the 700 kg/ notes and
        # a plugin rebuild are not worth running for one alias change.
        run([PY, "export_kal_graph.py"], cwd=HERE)
    elif not a.no_export:
        # graphml · graph3d.html · the 700 kg/ entity notes.
        # Skip these and Obsidian's own graph view keeps showing the old entities.
        run([PY, "export_graph.py", "--min-degree", "2", "--max-nodes", "700",
             "--obsidian", os.path.join(VAULT, "kg")], cwd=HERE)
        #  ⚠ **The target directory has to be checked too.**  A container has no viewer/ mount,
        #    so `/app/src/../viewer` does not exist, and run() exits on non-zero, so it dies
        #    entirely here —— export_kal_graph.py never runs at all and kal-graph.json is left
        #    stale while the run ends "failed".  After 30 minutes of extraction and 3 of indexing.
        #
        #    export_all.sh:29 fixed exactly this on 2026-08-19, and this file, which does the
        #    same copy, went unfixed.  That file's header says "the order and arguments must
        #    match refresh_kg.py's export block" —— they were supposed to match and diverged.
        #    (r4-fresh, 2026-08-21)
        _g3 = os.path.join(KAL_HOME, "graph_export/graph3d.html")
        _viewer = os.path.join(HERE, "..", "viewer")
        if os.path.isfile(_g3) and os.path.isdir(_viewer):
            run(["cp", _g3, os.path.join(_viewer, "graph3d.html")])
        else:
            print("   no viewer/ — skipping the graph3d.html copy (a host convenience)")
        run([PY, "export_kal_graph.py"], cwd=HERE)
        #  Like export_all.sh:48, it checks **node_modules**.  With the directory present but
        #  no dependencies, esbuild is missing and it dies.
        if os.path.isdir(os.path.join(PLUGIN, "node_modules")):
            run(["node", "esbuild.web.mjs"], cwd=PLUGIN)
            run(["node", "esbuild.config.mjs", "production"], cwd=PLUGIN)
            for f in ("main.js", "manifest.json", "styles.css"):
                run(["cp", os.path.join(PLUGIN, "dist", f), VAULT_PLUGIN])

    # The entity and relation counts changed, so the documented numbers are **checked**.
    # --fix is deliberately not used —— running an automatic document rewriter unattended means
    # nobody notices a wrong fix.  fix() really did have a substitution bug and went round three
    # repositories in that state.  It shows what disagrees and lets a person decide.
    v = subprocess.run([PY, "verify_docs.py"], cwd=HERE)
    if v.returncode != 0:
        print("\n  ⚠ The documented numbers disagree with reality.  Check the list above, then")
        print("     fix them with:  python verify_docs.py --fix")

    left, total2 = stale_state(db)
    print(f"\n  ── done {time.time()-t0:.0f}s ──")
    print(f"  stale documents {len(latest)} → {len(left)} / {total2}")
    if left:
        print("  ⚠ Marks remain.  schema_v3.py may have failed to drop stale_docs.")


if __name__ == "__main__":
    main()
