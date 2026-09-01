#!/usr/bin/env python3
"""Extract the DB's measured values as a markdown block.

Copying numbers into the README or the skill docs by hand goes wrong (disk usage was once
written as 2.5GB and corrected to a measured 499MB).  Run this and paste when updating the docs.

  python report_stats.py            # the markdown block
  python report_stats.py --json
"""
import os, sys, json, argparse, collections, subprocess


# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lancedb

DB = os.environ.get("KAL_PATH", os.path.join(KAL_HOME, "db"))
def du(path):
    try:
        out = subprocess.run(["du", "-sh", path], capture_output=True, text=True).stdout
        return out.split()[0]
    except Exception:
        return "?"


def collect():
    db = lancedb.connect(DB)
    counts = {t: db.open_table(t).count_rows() for t in sorted(db.list_tables().tables)}
    D = db.open_table("documents").search().limit(999999).to_list()
    origin = collections.Counter(r.get("origin", "vault") for r in D)
    dtype = collections.Counter(r.get("doc_type", "") for r in D if r.get("doc_type"))
    ent = db.open_table("lr_entities").search().limit(999999).to_list()
    etype = collections.Counter(e["type"] for e in ent)
    meta = {r["key"]: r["value"] for r in db.open_table("meta").search().limit(99).to_list()}
    return {"counts": counts, "origin": dict(origin), "doc_type": dict(dtype),
            "entity_type": dict(etype), "meta": meta,
            "disk": {"db": du(f"{KAL_HOME}/db"), "venv": du(f"{KAL_HOME}/venv"),
                     "total": du(KAL_HOME)}}


def markdown(s):
    c, o = s["counts"], s["origin"]
    L = ["```"]
    W = max(len(k) for k in c)
    note = {
        "documents": f"vault {o.get('vault',0)} + session {o.get('session',0)}",
        "chunks": f"{s['meta'].get('chunk_chars','?')} chars · "
                  f"{s['meta'].get('embedding_model','?').split('/')[-1]} "
                  f"{s['meta'].get('embedding_dim','?')}d · FTS {s['meta'].get('fts_tokenizer','?')}",
        "ix_terms": "the raw BM25 inverted index — unused by search, for debugging and tuning",
        "ix_postings": "term × chunk occurrences (tf + positions)",
        "ix_doclen": "token count per chunk",
        "lr_entities": "LLM-extracted entities · " +
                       " · ".join(f"{k} {v}" for k, v in
                                  sorted(s["entity_type"].items(), key=lambda x: -x[1])[:4]),
        "lr_relations": "LLM-extracted relations (undirected)",
        "meta": "the DB describing itself",
    }
    for k, v in c.items():
        L.append(f"   {k:<{W}}  {v:>12,}   {note.get(k,'')}".rstrip())
    L.append("   " + "─" * (W + 60))
    L.append(f"   disk DB {s['disk']['db']} · venv {s['disk']['venv']} · total {s['disk']['total']}")
    L.append("```")
    if s["doc_type"]:
        L.append("")
        L.append("Session document character (brain-ingest `doc_type`):")
        L.append("")
        L.append("```")
        for k, v in sorted(s["doc_type"].items(), key=lambda x: -x[1]):
            L.append(f"   {k:<16}{v:>5}")
        L.append("```")
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    s = collect()
    print(json.dumps(s, ensure_ascii=False, indent=1) if a.json else markdown(s))
