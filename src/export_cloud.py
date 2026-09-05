#!/usr/bin/env python3
"""Export the index for the cloud —— the tree `just push` uploads.

Not the raw `db/`.  The local index is built for one machine and carries three things a cloud
copy has no business holding (deep-review 2026-09-05, sync lens D3):

  · `documents.abs_path` —— the absolute path on the host (`/Users/<you>/…`) for every note.
  · `meta.vault_path`   —— the vault's location on the host.
  · every row derived from a gated document —— gated by the frontmatter flag `no_llm` **or** by a
    `KAL_NO_LLM` path (both are resolved into `documents.no_llm` at index time since 2026-09-05;
    the path rule is applied here again for indexes built before that).  Locally these stay (the
    note is searchable on your own machine); remotely the MCP already refuses to return them,
    but "gated at serve time" still meant the text left the machine.

What the export does, table by table:

  documents   `abs_path` blanked for **every** row.  A gated row keeps only `doc_id` and
              `no_llm=True` (path · title · folder · hash · dates blanked, sizes zeroed) —— the row
              itself must survive because the remote child (`kal_mcp._BLOCKED`, `llm_gate`) learns
              **which** doc_ids are gated from this table, and the entity/relation `doc_ids` lists
              still point at them.  Drop the row and the serve-time gate goes blind.
  chunks      rows whose `doc_id` is gated are dropped.
  ix_terms · ix_postings · ix_doclen   **not exported at all.**  Search uses lancedb's FTS index on
              `chunks.text`; the only readers of these hand-rolled BM25 tables are the ablation and
              manual-index scripts.  Leaving them out drops ~106 MB per push and keeps the
              3-gram vocabulary of gated text (`ix_terms.term`) on the machine.
  lr_entities · lr_relations   left as-is —— their `description`/`events` may summarise gated
              text, and the serve-time gate (`llm_gate`) filters them by `doc_ids`.  Removing them
              here would need the same rule in two places.
  meta        the `vault_path` row is dropped.
  everything else   copied unchanged.

Indices are rebuilt on the copy with the **same** configuration as the local build
(`schema_v3.INDEXES` · `FTS_COL` · `FTS_KW`): `create_table` from Arrow does not carry them over,
and `kal_search` queries chunks with `query_type="fts"` —— a copy without that index does not
search, it errors.  An index that fails to build is an error here, not a `✗` in a log line.

Usage:  export_cloud.py SRC_DB DST_DIR        (DST_DIR must be empty or absent)
        export_cloud.py --selftest
"""
import os
import shutil
import sys
import tempfile

import pyarrow as pa
import pyarrow.compute as pc

#  Columns of a gated `documents` row that are blanked (strings) or zeroed (numbers).  Anything not
#  listed —— `doc_id`, `no_llm`, and any column added later —— is kept, so a new column that carries
#  text would leak until it is added here.  The self-check below pins the set against the live schema.
GATED_BLANK = ("path", "abs_path", "title", "folder", "content_hash", "origin", "doc_type",
               "doc_date", "date_src", "doc_updated")
GATED_ZERO = ("size", "mtime", "indexed_at")
GATED_KEEP = ("doc_id", "no_llm")


def _gated_ids(docs):
    """doc_ids that must not leave, and the row mask —— two gates, both applied here.

    `documents.no_llm` carries both gates since 2026-09-05 (`schema_v3.scan_vault` ORs in
    `lr_extract.blocked_path` at index time).  The path gate is applied here **again** for an index
    built before that —— the cloud child has no config, so it has to be resolved on the machine that
    has it (deep-review 2026-09-05 R3–R4, sync/fact lenses).
    """
    from lr_extract import blocked_path
    flag = pc.fill_null(docs.column("no_llm"), False) if "no_llm" in docs.column_names \
        else pa.array([False] * docs.num_rows)
    by_path = pa.array([bool(pth) and blocked_path(pth) for pth in docs.column("path").to_pylist()])
    flag = pc.or_(flag, by_path)
    #  The stub keeps `no_llm=True` for path-gated rows too —— that is what the serve-time gate reads.
    return set(pc.filter(docs.column("doc_id"), flag).to_pylist()), flag


def _scrub_documents(docs, flag):
    """Blank `abs_path` everywhere; blank/zero the rest of a gated row."""
    cols = {}
    for name in docs.column_names:
        col = docs.column(name)
        if name == "abs_path":
            cols[name] = pa.array([""] * docs.num_rows, type=col.type)
        elif name == "no_llm":
            cols[name] = pc.or_(pc.fill_null(col, False), flag)      # path-gated rows become no_llm=True stubs
        elif name in GATED_BLANK:
            cols[name] = pc.if_else(flag, pa.scalar("", type=col.type), col)
        elif name in GATED_ZERO:
            cols[name] = pc.if_else(flag, pa.scalar(0, type=col.type), col)
        else:
            unknown = name not in GATED_KEEP
            if unknown and pa.types.is_string(col.type):
                #  A text column this module does not know about —— blank it on gated rows rather
                #  than let it through.  (Numeric unknowns stay: they cannot carry note text.)
                cols[name] = pc.if_else(flag, pa.scalar("", type=col.type), col)
            else:
                cols[name] = col
    return pa.table(cols, schema=docs.schema)


def _drop_rows(t, col, ids):
    if not ids or col not in t.column_names:
        return t
    mask = pc.invert(pc.is_in(t.column(col), value_set=pa.array(list(ids), type=t.column(col).type)))
    return t.filter(mask)


def export(src, dst):
    import lancedb
    from lancedb.index import FTS
    import schema_v3 as S

    if os.path.isdir(dst) and os.listdir(dst):
        raise SystemExit(f"❌ destination is not empty: {dst}")
    os.makedirs(dst, exist_ok=True)
    sdb = lancedb.connect(src)
    names = sorted(sdb.list_tables().tables)
    if not {"documents", "meta"} <= set(names):
        raise SystemExit(f"❌ {src} is not an index (documents/meta missing) —— run `just run index` first")
    docs = sdb.open_table("documents").to_arrow()
    gated, flag = _gated_ids(docs)
    gated_chunks = set()
    if "chunks" in names:
        ch = sdb.open_table("chunks").to_arrow()
        gated_chunks = set(pc.filter(ch.column("chunk_id"),
                                     pc.is_in(ch.column("doc_id"), value_set=pa.array(list(gated), type=ch.column("doc_id").type))).to_pylist()) if gated else set()
    ddb = lancedb.connect(dst)
    report = []
    for name in names:
        #  ⚠ ix_* (the hand-rolled BM25 tables) are **not exported**: kal_search queries lancedb's FTS index, and the only
        #     readers of ix_* are the ablation/manual scripts.  Leaving them out drops ~106 MB per push and the
        #     vocabulary of gated text that `ix_terms.term` would otherwise carry (R3, sync lens).
        if name.startswith("ix_"):
            report.append((name, sdb.open_table(name).count_rows(), 0, ["skipped"]))
            continue
        t = sdb.open_table(name).to_arrow()
        before = t.num_rows
        if name == "documents":
            t = _scrub_documents(t, flag)
        elif name == "meta":
            t = t.filter(pc.invert(pc.equal(t.column("key"), "vault_path")))
        elif "doc_id" in t.column_names:
            #  Covers `stale_docs` too (doc_id + the vault path): a gated note's path must not travel.  A separate
            #  path rule was tried and could not fire —— this one already removes the row (R4, sync lens; mutation).
            t = _drop_rows(t, "doc_id", gated)
        elif "chunk_id" in t.column_names:
            t = _drop_rows(t, "chunk_id", gated_chunks)
        tbl = ddb.create_table(name, data=t, schema=t.schema, mode="create")
        made = []
        for col, kind in S.INDEXES.get(name, []):
            tbl.create_index(col, replace=True, config=S.SCALAR_CFG[kind]())
            made.append(col)
        if name in S.FTS_COL:
            tbl.create_index(S.FTS_COL[name], replace=True, config=FTS(**S.FTS_KW))
            made.append("FTS")
        report.append((name, before, t.num_rows, made))
    return {"gated_documents": len(gated), "gated_chunks": len(gated_chunks), "tables": report}


def _selftest():
    import lancedb
    t = tempfile.mkdtemp()
    src, dst = os.path.join(t, "db"), os.path.join(t, "out")
    db = lancedb.connect(src)
    vec = pa.list_(pa.float32(), 4)
    db.create_table("documents", data=pa.table({
        "doc_id": pa.array([1, 2], pa.int64()), "path": ["ok.md", "Private/secret.md"],
        "abs_path": ["/home/u/v/ok.md", "/home/u/v/Private/secret.md"], "title": ["ok", "SECRET TITLE"],
        "folder": [".", "Private"], "size": pa.array([10, 20], pa.int64()), "mtime": pa.array([1, 2], pa.int64()),
        "content_hash": ["h1", "h2"], "indexed_at": pa.array([1, 2], pa.int64()), "origin": ["vault", "vault"],
        "doc_type": ["", "diary"], "doc_date": ["", "2026-01-01"], "date_src": ["none", "fm"],
        "no_llm": [False, True], "doc_updated": ["", "2026-01-02"]}))
    db.create_table("chunks", data=pa.table({
        "chunk_id": pa.array([10000, 20000, 20001], pa.int64()), "doc_id": pa.array([1, 2, 2], pa.int64()),
        "seq": pa.array([0, 0, 1], pa.int32()), "char_start": pa.array([0, 0, 500], pa.int32()),
        "text": ["public text", "SECRET BODY ONE", "SECRET BODY TWO"], "origin": ["vault"] * 3,
        "vector": pa.array([[0.1] * 4] * 3, vec)}))
    db.create_table("ix_doclen", data=pa.table({"chunk_id": pa.array([10000, 20000, 20001], pa.int64()),
                                                  "num_tokens": pa.array([2, 3, 3], pa.int32())}))
    db.create_table("ix_terms", data=pa.table({"term_id": pa.array([1], pa.int64()), "term": ["secret"],
                                                 "df": pa.array([2], pa.int64()), "idf": pa.array([0.5], pa.float32())}))
    db.create_table("meta", data=pa.table({"key": ["vault_path", "schema_version", "built_at"],
                                             "value": ["/home/u/v", "3", "1"]}))
    #  A third document is gated by **path** only (KAL_NO_LLM), not by frontmatter —— it must come out a stub too.
    db.open_table("documents").add(pa.table({
        "doc_id": pa.array([3], pa.int64()), "path": ["Finance/tax.md"], "abs_path": ["/home/u/v/Finance/tax.md"], "title": ["TAX"],
        "folder": ["Finance"], "size": pa.array([5], pa.int64()), "mtime": pa.array([3], pa.int64()), "content_hash": ["h3"],
        "indexed_at": pa.array([3], pa.int64()), "origin": ["vault"], "doc_type": [""], "doc_date": [""], "date_src": ["none"],
        "no_llm": [False], "doc_updated": [""]}))
    db.open_table("chunks").add(pa.table({
        "chunk_id": pa.array([30000], pa.int64()), "doc_id": pa.array([3], pa.int64()), "seq": pa.array([0], pa.int32()),
        "char_start": pa.array([0], pa.int32()), "text": ["TAX BODY"], "origin": ["vault"], "vector": pa.array([[0.1] * 4], vec)}))
    os.environ["KAL_NO_LLM"] = "Finance"
    import lr_extract
    #  The same cleaner the real setting goes through (schema_v3._clean_pathspec) —— a raw list bypassed it (R4, sync lens).
    import schema_v3 as _S
    lr_extract.NO_LLM = _S._clean_pathspec(os.environ["KAL_NO_LLM"])
    db.create_table("stale_docs", data=pa.table({"doc_id": pa.array([3, 1], pa.int32()), "path": ["Finance/tax.md", "ok.md"],
                                                  "reason": ["modified", "added"], "marked_at": pa.array([1, 1], pa.int64())}))
    r = export(src, dst)
    out = lancedb.connect(dst)
    d = {row["doc_id"]: row for row in out.open_table("documents").to_arrow().to_pylist()}
    assert set(d) == {1, 2, 3}, "the gated rows must stay (the serve-time gate learns doc_ids from them)"
    assert d[3]["no_llm"] is True and d[3]["path"] == "" and d[3]["title"] == "", f"path-gated row must be a stub: {d[3]}"
    assert not {n for n in out.list_tables().tables if n.startswith("ix_")}, "ix_* must not be exported"
    assert d[2]["no_llm"] is True and d[2]["path"] == "" and d[2]["title"] == "" and d[2]["size"] == 0 \
        and d[2]["doc_date"] == "" and d[2]["folder"] == "", f"gated row not scrubbed: {d[2]}"
    assert d[1]["abs_path"] == "" and d[2]["abs_path"] == "", "abs_path must be blank for every row"
    assert d[1]["path"] == "ok.md" and d[1]["title"] == "ok", "a normal row must be untouched"
    chunks = out.open_table("chunks").to_arrow().to_pylist()
    assert [c["chunk_id"] for c in chunks] == [10000], f"gated chunks must be gone: {chunks}"
    assert "SECRET" not in " ".join(c["text"] for c in chunks) and "TAX" not in " ".join(c["text"] for c in chunks)
    assert "vault_path" not in {m["key"] for m in out.open_table("meta").to_arrow().to_pylist()}, "meta.vault_path must not be exported"
    assert [r["path"] for r in out.open_table("stale_docs").to_arrow().to_pylist()] == ["ok.md"], "a gated note's stale_docs row (its vault path) must not be exported"
    names = {getattr(i, "name", str(i)) for i in out.open_table("chunks").list_indices()}
    assert any("text" in n for n in names), f"the FTS index on chunks.text must be rebuilt on the copy: {names}"
    #  …and the copy must answer a lexical query the way the original does —— `kal_search.bm25` now raises when the
    #  FTS index is missing instead of returning {} (R3, sync lens); this pins that the export never trips it.
    hits = out.open_table("chunks").search("public", query_type="fts").limit(5).to_list()
    assert [h["chunk_id"] for h in hits] == [10000], f"fts on the copy must find the open chunk only: {hits}"
    assert r["gated_documents"] == 2 and r["gated_chunks"] == 3, r
    #  The blank/zero sets must cover the live documents schema —— a new text column would leak otherwise.
    #  ⚠ The first version of this check read `S.S["documents"]` —— an attribute that does not exist —— behind a
    #     `hasattr` guard, so `live` was always empty and the assertion could never fire (a guard that has never
    #     been seen red is not a guard).  The schemas are a function of the embedding width.
    import schema_v3 as S
    live = {f.name for f in S.schemas(4)["documents"]}
    unknown = live - set(GATED_BLANK) - set(GATED_ZERO) - set(GATED_KEEP)
    assert live and not unknown, f"documents columns this module does not classify: {sorted(unknown)}"
    #  A non-empty destination is refused —— never merge into a stale export.
    try:
        export(src, dst); raise AssertionError("a non-empty destination must be refused")
    except SystemExit as e:
        assert "not empty" in str(e)
    shutil.rmtree(t)
    print("  ✅ export_cloud self-check —— gated row kept but scrubbed · its chunks/postings gone · abs_path blank · "
          "vault_path gone · FTS rebuilt · schema columns all classified · non-empty destination refused")


def main(argv):
    if "--selftest" in argv:
        _selftest()
        return 0
    if len(argv) != 3:
        print(__doc__.split("Usage:")[1].strip(), file=sys.stderr)
        return 2
    r = export(argv[1], argv[2])
    for name, before, after, made in r["tables"]:
        print(f"  {name:14}{before:>9,} → {after:>9,} rows   {' '.join(made)}")
    print(f"  gated documents {r['gated_documents']} · their chunks dropped {r['gated_chunks']} · abs_path blanked · vault_path dropped")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
