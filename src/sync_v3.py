#!/usr/bin/env python3
"""Incremental application — re-index only what changed.

What schema_v3.py's diff_vault() only decided, and then rebuilt everything, is applied here.

Rules per table:
  documents      add/edit upsert · delete · rename = delete + insert
  chunks         delete that doc_id's chunks → regenerate and insert (embeddings recomputed)
  ix_*           a changed chunk shifts df/idf globally → **a full rebuild**
                 (with few documents changed the df shift is negligible and an approximate
                  update would work.  Correctness is chosen here.  The rebuild is 4 seconds)
  lr_entities    needs LLM extraction → cannot be updated automatically.  Dead references cleaned + a warning
  lr_relations   the same

The core constraint — honestly:
  The knowledge graph is made by an LLM.  A changed document needs its entities and relations
  re-extracted, and that is an LLM call, which cannot happen automatically here.  Instead
    · references to deleted documents are cleaned up, and
    · "these documents have a stale KG" is recorded in the stale_docs table
  → re-running lr_extract.py against those documents alone is what fixes it.
"""
import os, sys, time, json, math, argparse, collections

# The model weights are on disk.  Without this pin, every process pays a round trip to
# huggingface.co (measured 5.2 s).  schema_v3 is imported first at :29, so it **currently**
# covers this, but that rests on line order —— isort and ruff move third-party imports **above**
# first-party ones.  Swapping :29 and :32 really did switch offline off (5.43s → 7.16s).  Pinned directly here.
# (r4-perf, round 5, 2026-08-21)
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
sys.path.insert(0, os.path.join(KAL_HOME, "bench"))
import lancedb
from lancedb.index import BTree, Bitmap, LabelList
from schema_v3 import SCALAR_CFG, S_STALE
from kal_lock import db_lock
import pyarrow as pa
from sentence_transformers import SentenceTransformer
from schema_v3 import (VAULT, DB, MODEL, CHUNK, OVERLAP, NGRAM, schemas, INDEXES,
                       FTS_COL, FTS_KW, scan_vault, diff_vault, tokenize,
                       build_inverted, stable_doc_id)

# S_STALE lives in schema_v3 (two files write the same table — the definition lives in one place)


def reindex_chunks(db, docs, targets, m):
    """Delete only the target documents' chunks and rebuild them."""
    if not targets:
        return 0, 0
    t = db.open_table("chunks")
    ids = ",".join(str(x) for x in targets)
    before = t.count_rows()
    t.delete(f"doc_id IN ({ids})")
    rows = []
    for did in sorted(targets):
        d = docs.get(did)
        if not d:
            continue                      # a deleted document → delete only
        body = d["_body"]
        for seq, i in enumerate(range(0, len(body), CHUNK - OVERLAP)):
            piece = body[i:i + CHUNK]
            if piece.strip():
                rows.append({"chunk_id": did * 10000 + seq, "doc_id": did,
                             "seq": seq, "char_start": i, "text": piece,
                             "origin": d.get("origin", "vault")})
    if rows:
        vecs = m.encode(["passage: " + r["text"] for r in rows],
                        normalize_embeddings=True, batch_size=64, show_progress_bar=False)
        for i, r in enumerate(rows):
            r["vector"] = vecs[i].tolist()
        t.add(rows)
    return before - t.count_rows() + len(rows), len(rows)


def rebuild_inverted(db, S):
    """ix_* is rebuilt in full.  df/idf are global statistics, so a partial update is inexact.
    Kept for debugging and tuning (at the user's request).  4 seconds for 1,400 chunks."""
    chunks = db.open_table("chunks").search().limit(999999).to_list()
    ixt, ixp, ixd = build_inverted(chunks)
    for name, rows in [("ix_terms", ixt), ("ix_postings", ixp), ("ix_doclen", ixd)]:
        tbl = db.create_table(name, mode="overwrite", schema=S[name], data=rows)
        for col, kind in INDEXES.get(name, []):
            try:
                tbl.create_index(col, replace=True, config=SCALAR_CFG[kind]())
            except Exception:
                pass
    return len(ixt), len(ixp)


def prune_kg(db, live_docs, live_chunks):
    """③ Remove dead doc_id / chunk_id references.  They are array columns, so whole rows are rewritten."""
    removed = 0
    for name in ("lr_entities", "lr_relations"):
        t = db.open_table(name)
        rows = t.search().limit(999999).to_list()
        dirty = []
        for r in rows:
            d0 = list(r["doc_ids"])
            d1 = [x for x in d0 if x in live_docs]
            if len(d1) != len(d0):
                removed += len(d0) - len(d1)
                r["doc_ids"] = d1
                dirty.append(r)
        if dirty:
            key = "entity_id" if name == "lr_entities" else "rel_id"
            ids = ",".join(str(r[key]) for r in dirty)
            t.delete(f"{key} IN ({ids})")
            t.add(dirty)
    return removed


def mark_stale(db, docs, diff, S):
    """Record the documents that need LLM extraction.  Running lr_extract.py on just these is enough."""
    rows = []
    now = int(time.time())
    for did in diff["added"]:
        rows.append({"doc_id": did, "path": docs[did]["path"], "reason": "added", "marked_at": now})
    for did in diff["modified"]:
        rows.append({"doc_id": did, "path": docs[did]["path"], "reason": "modified", "marked_at": now})
    for _, new in diff["renamed"].items():
        rows.append({"doc_id": new, "path": docs[new]["path"], "reason": "renamed", "marked_at": now})
    try:
        t = db.open_table("stale_docs")
        if rows:
            t.add(rows)
    except Exception:
        t = db.create_table("stale_docs", mode="overwrite", schema=S_STALE, data=rows or [])
    return len(rows)


def _selftest():
    """Does the incremental path **avoid mixing embedding spaces.**

    `schema_v3` regenerates everything and replaces the lot —— nothing can mix there.  The place
    where it does mix is here: only a changed document's chunks are deleted and re-inserted, so
    when the code's `MODEL` differs from the DB's, **one table ends up holding two embedding
    spaces.**  Search has no way to know, and no error is raised.

    The model is not called —— **and making that true takes some setting up.**

    The guard (:211) comes before the model is created (:220).  So when the guard *fires*, no
    model is built.  But when the guard **passes** (same model, or a first build) it carried on
    and read the weights at :220 —— which is why this self-check was the slowest step in the
    suite (3.4 s of 9.2 s).  And this docstring claimed it was not called.
    (raised by r4-perf, round 5, 2026-08-21)

    `SentenceTransformer` is replaced with a sentinel.  That has two effects:
      · the weights are not read —— the docstring becomes true
      · the passing case's assertion **gets sharper**: instead of "some exception", it proves
        "it reached the point where the model is created".  It used to be `except Exception:
        pass`, which could not tell a passing guard from dying earlier for another reason.
    """
    import schema_v3 as _S

    class _T:
        def __init__(s, rows): s.rows = rows
        def search(s): return s
        def limit(s, n): return s
        def to_list(s): return s.rows
    class _DB:
        def __init__(s, model): s.model = model
        def open_table(s, n):
            if n == "meta":
                if s.model is None:
                    raise RuntimeError("no meta")       # a first build
                return _T([{"key": "embedding_model", "value": s.model}])
            raise AssertionError("another table was opened before the guard could fire")

    class _ReachedModel(Exception):
        """A marker that the guard passed and reached the model-creation point.  No weights are read."""

    import lancedb as _l
    _real = _l.connect
    _real_st = globals()["SentenceTransformer"]

    def _no_model(*_a, **_k):
        raise _ReachedModel()

    globals()["SentenceTransformer"] = _no_model
    try:
        # ① the same model —— the guard must not fire (stopping at model loading counts as passing)
        _l.connect = lambda *a, **k: _DB(_S.MODEL)
        try:
            sync(dry_run=True)
            raise AssertionError("it never reached the model-creation point —— the flow changed")
        except SystemExit as e:
            raise AssertionError(f"the models are the same and the guard fired: {e}")
        except _ReachedModel:
            pass                       # it passed the guard and got here = the expected behaviour

        # ② a different model —— it **must** stop
        _l.connect = lambda *a, **k: _DB("old/model-v1")
        try:
            sync(dry_run=True)
            raise AssertionError("the models differ and the incremental sync went ahead —— the spaces mix")
        except SystemExit as e:
            assert "embedding model" in str(e), f"the message does not name the cause: {e}"

        # ③ with no meta (a first build) it does not block —— there is nothing to mix
        _l.connect = lambda *a, **k: _DB(None)
        try:
            sync(dry_run=True)
            raise AssertionError("it never reached the model-creation point —— the flow changed")
        except SystemExit as e:
            raise AssertionError(f"a first build is blocked: {e}")
        except _ReachedModel:
            pass
    finally:
        _l.connect = _real
        globals()["SentenceTransformer"] = _real_st

    # ── A present-but-unreadable file must not be deleted from the index ────────────────
    #  This is the path that actually removes rows, so it is the one that has to be pinned.
    #  A dangling symlink never appears in `scan_vault()`'s result, `diff_vault` sees an indexed
    #  document with no file, and everything below deletes it —— silently.  Measured 2026-09-04:
    #  a two-document vault with one dangling symlink came out of `sync` holding one.
    #
    #  ⚠ Driven end to end as a **subprocess**, not by calling the rescue: the whole failure was
    #     that two walks disagreed, and a unit test of the arithmetic would agree with itself.
    import subprocess as _sp, tempfile as _tf2, lancedb as _lc2
    _v, _h = _tf2.mkdtemp(), _tf2.mkdtemp()
    _dbp = os.path.join(_h, "db")
    for _n in ("keep.md", "victim.md"):
        open(os.path.join(_v, _n), "w", encoding="utf-8").write(
            f"---\ntitle: {_n}\n---\n" + "본문 " * 40 + "\n")
    _env = {**os.environ, "KAL_VAULT": _v, "KAL_PATH": _dbp, "KAL_HOME": _h}
    _here = os.path.dirname(os.path.abspath(__file__))
    _r = _sp.run([sys.executable, os.path.join(_here, "schema_v3.py")],
                 env=_env, capture_output=True, text=True)
    assert _lc2.connect(_dbp).open_table("documents").count_rows() == 2, \
        f"the fixture did not index two documents, so this tests nothing: {_r.stderr[-300:]}"
    #  The shape an Obsidian rename leaves behind.
    os.remove(os.path.join(_v, "victim.md"))
    os.symlink(os.path.join(_v, "moved-away.md"), os.path.join(_v, "victim.md"))
    _r2 = _sp.run([sys.executable, os.path.join(_here, "sync_v3.py")],
                  env=_env, capture_output=True, text=True)
    _left = _lc2.connect(_dbp).open_table("documents").count_rows()
    assert _left == 2, (
        f"sync deleted a document whose file is present but unreadable —— {_left} left of 2.\n"
        f"{_r2.stdout[-400:]}")
    #  …and it has to **say so**, or the rescue is itself a silent behaviour.
    assert "could not be read" in _r2.stdout, \
        f"the unreadable file was rescued without a word: {_r2.stdout[-300:]}"
    _sh2 = __import__("shutil")
    _sh2.rmtree(_v, ignore_errors=True)
    _sh2.rmtree(_h, ignore_errors=True)
    print("  ✅ sync_v3 self-check —— a dangling symlink is not a deletion (and it says so)")

    print("  ✅ sync_v3 self-check —— a different model stops the incremental path (no space mixing)")


def sync(dry_run=False):
    db = lancedb.connect(DB)

    # ⚠ **Check which model the DB was built with, first.**
    #
    # This is the incremental path —— only a changed document's chunks are deleted and
    # re-inserted.  So when the code's `MODEL` differs from the DB's, **one table ends up
    # holding two mixed embedding spaces.**  Search has no way to know: no error, just quietly
    # worse results.  (schema_v3, which regenerates everything, replaces the lot and does not
    #  have this problem.  Guarding only there left the place where mixing actually happens open.)
    #
    # It **stops** rather than fixing it —— triggering a full re-index automatically here would
    # turn an "incremental sync" into a job of tens of minutes.  That is a person's decision.
    try:
        _meta = {r["key"]: r["value"] for r in
                 db.open_table("meta").search().limit(99).to_list()}
    except Exception:
        _meta = {}                      # a first build —— nothing to mix
    _was = _meta.get("embedding_model")
    if _was and _was != MODEL:
        raise SystemExit(
            f"❌ the embedding model differs —— stopping the incremental sync.\n"
            f"   DB: {_was}\n"
            f"   code: {MODEL}\n"
            f"   Inserting anyway mixes two embedding spaces into one table, and search gets\n"
            f"   worse with no error.  A full re-index is required:  just index")

    m = SentenceTransformer(MODEL)
    S = schemas(m.get_embedding_dimension())

    docs, unreadable = scan_vault(with_unreadable=True)
    d = diff_vault(docs, db)
    if d["first_build"]:
        return print("a first build is required — run schema_v3.py first")

    #  ⚠ **A file that is present but could not be read is not a deletion, and this is the path
    #     that would delete it.**  `glob` yields a dangling symlink —— an Obsidian rename or a
    #     moved attachment leaves one routinely —— `is_skipped` passes it, `open` raises, and it
    #     simply never appears in `scan_vault()`'s result.  `diff_vault` then sees a document in
    #     the index with no file behind it and calls it deleted, and the lines below remove its
    #     row, its chunks and its postings.  Nothing said a word.
    #     `scan_vault(with_unreadable=True)` returns them **from this walk**, so nothing
    #     else can clear the list between the walk and the rescue.
    #     (measured 2026-09-04: a two-file vault with one dangling symlink lost that document.)
    if unreadable:
        _unread = {stable_doc_id(_rel) for _rel in unreadable}
        _rescued = d["deleted"] & _unread
        d["deleted"] -= _unread
        print(f"  ⚠ {len(unreadable)} file(s) in the vault could not be read "
              f"(first: {unreadable[0]}).  A dangling symlink is the usual cause."
              + (f"  {len(_rescued)} of them would have been deleted from the index —— kept."
                 if _rescued else ""))

    touched = d["added"] | d["modified"] | set(d["renamed"].values())
    gone = d["deleted"] | set(d["renamed"].keys())
    #  estimate.py's speed-learning unit —— "changed documents".
    try:
        import run_log; run_log.count(len(d['added']) + len(d['modified']) + len(d['deleted']))
    except Exception:
        pass
    print(f"decided  added {len(d['added'])} · modified {len(d['modified'])} · deleted {len(d['deleted'])} "
          f"· renamed {len(d['renamed'])} · unchanged {len(d['unchanged'])}")
    if not touched and not gone:
        return print("no changes — nothing done")
    if dry_run:
        return print("(dry-run)")

    t0 = time.time()
    # 1) documents
    T = db.open_table("documents")
    if gone:
        T.delete(f"doc_id IN ({','.join(str(x) for x in gone)})")
    if touched:
        T.delete(f"doc_id IN ({','.join(str(x) for x in touched)})")
        T.add([{k: v for k, v in docs[x].items() if k != "_body"} for x in touched])
    print(f"  documents     deleted {len(gone)} · upserted {len(touched)}   {time.time()-t0:.1f}s")

    # 2) chunks — only the targets are re-chunked and re-embedded
    t1 = time.time()
    dele, ins = reindex_chunks(db, docs, touched | gone, m)
    print(f"  chunks        regenerated {ins} rows                 {time.time()-t1:.1f}s")

    # 3) ix_* — a full rebuild (df/idf are global statistics).  Kept for debugging
    t2 = time.time()
    nt, npst = rebuild_inverted(db, S)
    print(f"  ix_*          rebuilt terms {nt:,} postings {npst:,}   {time.time()-t2:.1f}s")

    # 4) KG — clean dead references + mark stale
    t3 = time.time()
    # live = the whole of vault + session.  Using docs (the vault scan) alone mistakes session
    # doc_ids for 'dead references' and cuts them out of the KG.
    live_d = {r["doc_id"] for r in db.open_table("documents").search().limit(999999).to_list()}
    live_c = {r["chunk_id"] for r in db.open_table("chunks").search().limit(999999).to_list()}
    pruned = prune_kg(db, live_d, live_c)
    staled = mark_stale(db, docs, d, S)
    print(f"  lr_*          {pruned} dangling reference(s) removed · {staled} marked stale  {time.time()-t3:.1f}s")

    print(f"\ntotal {time.time()-t0:.1f}s")
    if staled:
        print(f"⚠️  the knowledge graph needs LLM extraction and cannot be updated automatically.")
        # ⚠ It used to say "re-run lr_extract.py on just those documents".  Both halves were
        #   wrong —— lr_extract writes only lr_kg.json, so run alone it leaves **search
        #   unchanged** while confidently printing an entity count that looks like success.
        #   And it has no per-document option.  (adversarial review 2026-08-18, BLOCKER)
        print(f"    {staled} recorded in stale_docs — restore them with `python refresh_kg.py`.")
        print(f"    (running lr_extract.py alone updates lr_kg.json only and never reaches search)")


def _main_sync():
    # Two writers on the same DB diverge silently (see the comments in kal_lock.py)
    # ⚠ It used to be `sync(dry_run="--dry-run" in sys.argv)`.  One typo (--dryrun, -n) or even
    #   --help fell through to **a real change** —— deleting and re-inserting documents and
    #   chunks and rebuilding ix_* wholesale.  The docs say to run dry-run "always first", so a
    #   typo while following that advice produced exactly the opposite result.
    #   (adversarial review 2026-08-18)
    ap = argparse.ArgumentParser(description="apply only the vault's changes to the index")
    ap.add_argument("--dry-run", action="store_true", help="decide only, change nothing")
    args = ap.parse_args()
    with db_lock("sync_v3"):
        sync(dry_run=args.dry_run)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest(); sys.exit(0)
    # Record the run under ~/.kal/runs/ —— the web screen's "last run" only knew about runs
    # started from the web UI, so a CLI success still showed yesterday's failure as the last.
    from run_log import record
    with record("sync"):
        _main_sync()
