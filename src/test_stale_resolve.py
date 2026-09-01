#!/usr/bin/env python3
"""Resolving stale marks —— the calculation schema_v3 does at the end of a rebuild.

Why it is tested separately
  When this calculation is wrong **nothing makes a sound.**  A mark wrongly cleared means that
  document is reported "clean" forever; a mark wrongly kept means a false warning that never
  stops.  Both are silent failures with no way for a person to notice.  So a runnable test stays here.

  A real rebuild takes tens of minutes, so only the calculation is lifted out and checked.  If
  it diverges from schema_v3 this test becomes meaningless —— **change schema_v3 and change this too.**

    python test_stale_resolve.py
"""
import time


def resolve(stale_rows, stale_at_start, drift):
    """Must be identical to the calculation at the end of schema_v3.py's rebuild.

    stale_rows      everything currently in the stale_docs table
    stale_at_start  the snapshot at the start of the rebuild (what this extraction resolved)
    drift           documents whose body changed since extraction (found by comparing doc_hashes)
    """
    keep = [r for r in stale_rows if r["doc_id"] not in stale_at_start]
    have = {r["doc_id"] for r in keep}
    keep += [r for r in drift if r["doc_id"] not in have]
    return sorted(r["doc_id"] for r in keep)


def main():
    t = int(time.time())
    M = lambda i: {"doc_id": i, "path": f"d{i}.md", "reason": "modified", "marked_at": t}

    cases = [
        ("starting marks only — all resolved",
         [M(111)], {111}, [], []),
        ("marks sync_v3 added mid-rebuild survive",
         [M(111), M(222)], {111}, [], [222]),
        ("documents changed since extraction are marked anew",
         [M(111)], {111}, [M(333)], [333]),
        ("both — one document on either side still yields one row",
         [M(111), M(222)], {111}, [M(222), M(333)], [222, 333]),
        ("an empty snapshot resolves nothing",
         [M(111)], set(), [], [111]),
    ]
    for name, rows, snap, drift, want in cases:
        got = resolve(rows, snap, drift)
        assert got == want, f"{name}: {got} != {want}"
        print(f"  ✅ {name}")
    print("  ✅ self-check passed — 5 stale-resolution scenarios")


if __name__ == "__main__":
    main()
