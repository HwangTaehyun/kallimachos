#!/usr/bin/env python3
"""Migrate lr_cache.jsonl to content addressing.

Why it is needed
  The cache key was (document path, chunk index).  A changed document with an unchanged path
  reused the old extraction result — silent staleness.  lr_extract.py was fixed to put the
  chunk body's hash into the key too, but the existing cache holds no hash, so leaving it
  alone re-extracts everything (876 chunks, about 3 hours).

What this does
  It rebuilds the same (path, index) chunk from the current document, computes the hash and fills it in.

  ⚠️ This rests on the assumption "that chunk has not changed since extraction".  If it has,
     a wrong hash makes a stale result valid forever.  So **documents known to have changed
     are excluded** and left to be re-extracted (see STALE below).

Usage:
  python migrate_lr_cache.py --dry-run
  python migrate_lr_cache.py
"""
import os, sys, json, shutil, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lr_extract import collect, CACHE

# Documents known to have changed in this rebuild — left without a hash so they are re-extracted
STALE = ("wiki_sources_claude-session-corpus", "wiki_index", "wiki_log")


def main(dry):
    if not os.path.exists(CACHE):
        raise SystemExit(f"no cache found: {CACHE}")
    rows = []
    for line in open(CACHE, encoding="utf-8"):
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    have_h = sum(1 for r in rows if "h" in r)
    print(f"  cache {len(rows)} rows · with hash {have_h} · without {len(rows)-have_h}")

    # From the current documents: (path, index) → hash
    now = {(c["doc"], c["idx"]): c["h"] for c in collect()}
    print(f"  {len(now)} chunks in the current corpus")

    filled = dropped = kept = 0
    out = []
    for r in rows:
        if "h" in r:
            out.append(r); kept += 1; continue
        k = (r["doc"], r["idx"])
        if any(s in r["doc"] for s in STALE) or k not in now:
            dropped += 1                      # a re-extraction target
            continue
        r["h"] = now[k]
        out.append(r); filled += 1

    print(f"  hashes filled {filled} · kept {kept} · dropped {dropped}")
    print(f"  → {len(now)-len(out):,} chunk(s) will be re-extracted on the next run")
    if dry:
        return print("  (dry-run)")

    shutil.copy2(CACHE, CACHE + ".bak")
    with open(CACHE, "w", encoding="utf-8") as fh:
        for r in out:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"  ✅ {CACHE}  (the original is kept as .bak)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    main(ap.parse_args().dry_run)
