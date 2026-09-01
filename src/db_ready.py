#!/usr/bin/env python3
"""Check in one place whether the knowledge DB is **ready**.

Why —— opening a DB that has not been built makes LanceDB raise
`ValueError: Table 'chunks' was not found`.  That is a Python traceback, and it is **the first
screen a fresh install sees** (the README puts `just verify` and `just search` "next").
It says neither what went wrong nor what to do about it.

`status.py` already treats this state as normal (`_empty_status`) while the rest did not.
This exists so the same judgement is not hand-written a third time.

Self-check:  python3 src/db_ready.py --selftest
"""
import os
import sys


def missing(db_path, need):
    """Which of `need` is missing.  An empty list means ready."""
    try:
        import lancedb
        have = set(lancedb.connect(db_path).table_names())
    except Exception:
        return sorted(need)                 # if it cannot even be opened, everything is missing
    return sorted(t for t in need if t not in have)


def require(db_path, need, what):
    """When it is not ready, exit with **guidance a person can read** (instead of a traceback).

    `what` is "what were you trying to do" —— the guidance has to carry that context.
    """
    miss = missing(db_path, need)
    if not miss:
        return
    raise SystemExit(
        f"  ❌ there is no knowledge DB yet —— {what} cannot run.\n"
        f"     missing tables: {', '.join(miss)}\n"
        f"     {db_path.replace(os.path.expanduser('~'), '~')}\n\n"
        f"     just plan          # see how long it will take first\n"
        f"     just run index     # read the notes and build the knowledge DB")


def _selftest():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        empty = os.path.join(d, "db")
        assert missing(empty, ["chunks"]) == ["chunks"], "an empty DB is reported ready"
        try:
            require(empty, ["chunks", "documents"], "search")
        except SystemExit as e:
            msg = str(e)
            assert "there is no knowledge DB yet" in msg, msg
            assert "just run index" in msg, "it does not say what to do next"
            assert "chunks" in msg and "documents" in msg, "it does not say what is missing"
        else:
            raise AssertionError("it was not ready and went straight through")

        #  A ready DB is **not blocked** —— no crying wolf
        import fixture_db
        good = fixture_db.build()
        try:
            assert missing(good, ["documents", "lr_entities"]) == [], missing(good, ["documents"])
            require(good, ["documents"], "search")       # must not die
        finally:
            import shutil
            shutil.rmtree(good, ignore_errors=True)
    print("  ✅ db_ready —— empty-DB guidance (what is missing · what to do next) · a ready DB passes")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest(); sys.exit(0)
