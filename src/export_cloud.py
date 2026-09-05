#!/usr/bin/env python3
"""Export the index for the cloud —— the tree `just push` uploads.

Not the raw `db/`.  The local index is built for one machine and carries three things a cloud
copy has no business holding (deep-review 2026-09-05, sync lens D3):

  · `documents.abs_path` —— the absolute path on the host (`/Users/<you>/…`) for every note.
  · `meta.vault_path`   —— the vault's location on the host.
  · every row derived from a `no_llm` document —— the 500-character body pieces in `chunks`, and
    the BM25 rows keyed by those chunks (`ix_doclen`, `ix_postings`).  Locally these stay (the
    note is searchable on your own machine); remotely the MCP already refuses to return them,
    but "gated at serve time" still meant the text left the machine.

What the export does, table by table:

  documents   `abs_path` blanked for **every** row.  A gated row keeps only `doc_id` and
              `no_llm=True` (path · title · folder · hash · dates blanked, sizes zeroed) —— the row
              itself must survive because the remote child (`kal_mcp._BLOCKED`, `llm_gate`) learns
              **which** doc_ids are gated from this table, and the entity/relation `doc_ids` lists
              still point at them.  Drop the row and the serve-time gate goes blind.
  chunks      rows whose `doc_id` is gated are dropped.
  ix_doclen · ix_postings   rows whose `chunk_id` belonged to a gated document are dropped.
  ix_terms    left as-is —— `df`/`idf` still count the dropped chunks.  That is a ranking detail
              (a slightly wrong idf for a few terms), not text; documented here rather than fixed
              because a recount would mean re-running the whole BM25 build.
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
    flag = pc.fill_null(docs.column("no_llm"), False) if "no_llm" in docs.column_names \
        else pa.array([False] * docs.num_rows)
    return set(pc.filter(docs.column("doc_id"), flag).to_pylist()), flag


def _scrub_documents(docs, flag):
    """Blank `abs_path` everywhere; blank/zero the rest of a gated row."""
    cols = {}
    for name in docs.column_names:
        col = docs.column(name)
        if name == "abs_path":
            cols[name] = pa.array([""] * docs.num_rows, type=col.type)
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
        t = sdb.open_table(name).to_arrow()
        before = t.num_rows
        if name == "documents":
            t = _scrub_documents(t, flag)
        elif name == "meta":
            t = t.filter(pc.invert(pc.equal(t.column("key"), "vault_path")))
        elif "doc_id" in t.column_names:
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
    r = export(src, dst)
    out = lancedb.connect(dst)
    d = {row["doc_id"]: row for row in out.open_table("documents").to_arrow().to_pylist()}
    assert set(d) == {1, 2}, "the gated row must stay (the serve-time gate learns doc_ids from it)"
    assert d[2]["no_llm"] is True and d[2]["path"] == "" and d[2]["title"] == "" and d[2]["size"] == 0 \
        and d[2]["doc_date"] == "" and d[2]["folder"] == "", f"gated row not scrubbed: {d[2]}"
    assert d[1]["abs_path"] == "" and d[2]["abs_path"] == "", "abs_path must be blank for every row"
    assert d[1]["path"] == "ok.md" and d[1]["title"] == "ok", "a normal row must be untouched"
    chunks = out.open_table("chunks").to_arrow().to_pylist()
    assert [c["chunk_id"] for c in chunks] == [10000], f"gated chunks must be gone: {chunks}"
    assert "SECRET" not in " ".join(c["text"] for c in chunks)
    assert [r_["chunk_id"] for r_ in out.open_table("ix_doclen").to_arrow().to_pylist()] == [10000], "ix_doclen keyed by gated chunks must be gone"
    assert out.open_table("ix_terms").count_rows() == 1, "ix_terms is copied as-is (documented)"
    assert "vault_path" not in {m["key"] for m in out.open_table("meta").to_arrow().to_pylist()}, "meta.vault_path must not be exported"
    names = {getattr(i, "name", str(i)) for i in out.open_table("chunks").list_indices()}
    assert any("text" in n for n in names), f"the FTS index on chunks.text must be rebuilt on the copy: {names}"
    assert r["gated_documents"] == 1 and r["gated_chunks"] == 2, r
    #  The blank/zero sets must cover the live documents schema —— a new text column would leak otherwise.
    import schema_v3 as S
    live = {f.name for f in S.S["documents"]} if hasattr(S, "S") and "documents" in getattr(S, "S", {}) else set()
    unknown = live - set(GATED_BLANK) - set(GATED_ZERO) - set(GATED_KEEP)
    assert not unknown, f"documents columns this module does not classify: {sorted(unknown)}"
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
