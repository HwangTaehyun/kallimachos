#!/usr/bin/env python3
"""**A small temporary LanceDB** for self-checks.

Why it exists —— the self-checks in `export_kal_graph`, `export_graph` and `export_webgl` were
opening **the real DB** at `~/.kal`.  That passes on the author's machine and dies anywhere
without one, such as CI, with `ValueError: Table 'lr_entities' was not found`.
`just` stops at the first failure, so **the 23 after it never run** —— CI starts red from the
very first push (reproduced 2026-08-25, deep review round 4).

Three places need the same four tables, so they live in **one place**.  Three copies of a
fixture drift eventually, and this repository has made that mistake several times already.

The data is **deliberately** shaped so the gate test holds:
  - an entity sourced only from `d_secret` → must drop out entirely (`should_drop`)
  - an entity sourced from two documents → must survive with its description redacted (`red > 0`)
  - relations are given so `min_degree=1` is cleared
"""
import os
import tempfile

#  The document the gate test picks as its block target —— it must hold the most entities.
TARGET_DOC = "d_secret"


def db_is_usable(path):
    """Can the real DB be used for a self-check —— do the tables **exist and hold rows**.

    Checking "does the table exist" is not enough.  Run `just run index` without `extract` and
    `lr_entities` **exists with 0 rows**, and then the gate test's
    `Counter(...).most_common(1)[0]` dies with `IndexError` —— the fixture was switched on only
    "when a table is missing" and so never covered exactly that state
    (deep review 2026-08-25, newcomer lens).
    """
    need = ("lr_entities", "documents", "lr_relations", "meta")
    try:
        import lancedb
        db = lancedb.connect(path)
        if not set(need) <= set(db.table_names()):
            return False
        #  With 0 entities the gate test does not hold
        return db.open_table("lr_entities").count_rows() > 0
    except Exception:
        return False


def build(dirpath=None):
    """Return the path of a temporary DB holding the four tables.  Deleting it is the caller's job."""
    import lancedb
    d = dirpath or tempfile.mkdtemp(prefix="kal-fixture-")
    db = lancedb.connect(d)

    docs = [
        {"doc_id": "d_secret", "path": "vault/Private/secret.md", "no_llm": False,
         "title": "secret", "mtime": 1.0, "sha": "a" * 8},
        {"doc_id": "d_open", "path": "vault/notes/open.md", "no_llm": False,
         "title": "open", "mtime": 2.0, "sha": "b" * 8},
        {"doc_id": "d_third", "path": "vault/notes/third.md", "no_llm": False,
         "title": "third", "mtime": 3.0, "sha": "c" * 8},
    ]
    #  e1 and e2 are sole-sourced from d_secret → they drop out entirely when it is blocked.
    #  e3 comes from d_secret + d_open → it survives with its description redacted.
    #  e4 comes from d_open + d_third → untouched (the control).
    ents = [
        {"entity_id": "e1", "name": "Alpha", "type": "person",
         "description": "only in secret", "doc_ids": ["d_secret"], "degree": 2},
        {"entity_id": "e2", "name": "Beta", "type": "organization",
         "description": "only in secret too", "doc_ids": ["d_secret"], "degree": 2},
        {"entity_id": "e3", "name": "Gamma", "type": "concept",
         "description": "seen in both", "doc_ids": ["d_secret", "d_open"], "degree": 3},
        {"entity_id": "e4", "name": "Delta", "type": "concept",
         "description": "clean", "doc_ids": ["d_open", "d_third"], "degree": 2},
    ]
    rels = [
        {"src_id": "e1", "tgt_id": "e2", "doc_ids": ["d_secret"],
         "description": "r12", "keywords": "k"},
        {"src_id": "e1", "tgt_id": "e3", "doc_ids": ["d_secret"],
         "description": "r13", "keywords": "k"},
        {"src_id": "e3", "tgt_id": "e4", "doc_ids": ["d_open"],
         "description": "r34", "keywords": "k"},
        {"src_id": "e4", "tgt_id": "e3", "doc_ids": ["d_third"],
         "description": "r43", "keywords": "k"},
    ]
    #  ⚠ `built_at` is **an epoch integer** (`status.py:97` parses it with `int()`).
    #     A date string there died with `ValueError: invalid literal for int()`.
    #     A fixture **dying because its shape differs** from the real thing beats a silent
    #     failure, but not having to meet it at all is better —— the key list follows `status.py:97,202-205`.
    meta = [{"key": "built_at", "value": "1"},
            {"key": "embedding_model", "value": "fixture-embed"},
            {"key": "schema_version", "value": "3"},
            {"key": "vault_path", "value": "/tmp/fixture-vault"},
            {"key": "model", "value": "fixture"}]

    db.create_table("documents", docs, mode="overwrite")
    db.create_table("lr_entities", ents, mode="overwrite")
    db.create_table("lr_relations", rels, mode="overwrite")
    db.create_table("meta", meta, mode="overwrite")
    return d


def _selftest():
    import shutil
    import lancedb
    d = build()
    try:
        db = lancedb.connect(d)
        E = db.open_table("lr_entities").search().limit(99).to_list()
        docs = db.open_table("documents").search().limit(99).to_list()
        #  Is the shape one in which the gate test holds —— otherwise the test is quietly meaningless.
        import collections
        cnt = collections.Counter(x for e in E for x in (e.get("doc_ids") or []))
        assert cnt.most_common(1)[0][0] == TARGET_DOC, \
            f"the document with the most entities is not {TARGET_DOC} —— the test blocks the wrong one"
        solo = {e["entity_id"] for e in E if set(e["doc_ids"]) <= {TARGET_DOC}}
        assert len(solo) >= 2, f"{len(solo)} sole-sourced entity(ies) —— the gate test is meaningless"
        both = [e for e in E if TARGET_DOC in e["doc_ids"] and len(e["doc_ids"]) > 1]
        assert both, "no partially blocked entity —— redaction (REDACTED) cannot be verified"
        assert all(r["doc_id"] for r in docs), "a document row has an empty id"
        #  meta must be **the shape the readers expect**.  `built_at` was left as a date
        #  string once and died at `status.py:97`'s `int()` (2026-08-25).
        M = {r["key"]: r["value"] for r in db.open_table("meta").search().limit(99).to_list()}
        int(M["built_at"])                       # dying here means the fixture is wrong
        for k in ("embedding_model", "schema_version", "vault_path"):
            assert M.get(k), f"meta has no {k} —— status.py reads that key"
        for t in ("documents", "lr_entities", "lr_relations", "meta"):
            assert db.open_table(t).search().limit(1).to_list(), f"{t} is empty"
        print(f"  ✅ fixture_db —— 4 tables · {len(solo)} sole-sourced · {len(both)} mixed")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest(); sys.exit(0)
    print(build())
