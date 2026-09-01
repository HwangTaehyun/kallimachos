# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
import os
#!/usr/bin/env python3
"""An inverted index built directly as **ordinary LanceDB tables** — without the built-in FTS.

Why build it:
  The inverted index LanceDB's create_index(FTS) produces is a compressed binary under
  _indices/, so SQL cannot look inside it.  To see "which chunks hold this word, and how many
  times" directly, the same structure can be reproduced as ordinary tables.  LanceDB supports that.

ERD:
  ix_terms     term(PK) · term_id · df · idf
  ix_postings  term_id ─┐  chunk_id ─┐  tf  · positions
  ix_docs      chunk_id(PK) · doc · num_tokens
                        │            └──> chunks.id
                        └──> ix_terms.term_id

How it differs from the built-in FTS:
  · no compression → larger, but everything is queryable
  · positions stored too → phrase search is possible
  · BM25 computed in SQL or Python → the parameters (k1, b) can be tuned directly
"""
import json, math, sys, collections

# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
sys.path.insert(0, os.path.join(KAL_HOME, "bench"))
import lancedb
from lancedb.index import BTree, Bitmap, LabelList
from kal_lock import db_lock
import pyarrow as pa

DB = os.path.join(KAL_HOME, "lancedb")
CHUNKS = os.path.join(KAL_HOME, "chunks_500.json")
K1, B = 1.2, 0.75          # BM25 parameters — the built-in FTS cannot change these


def tokenize(text, lo=2, hi=3):
    """ngram(2,3) — the same configuration as LanceDB's built-in FTS.

    lower_case=True · stem=False · remove_stop_words=False · ascii_folding=False
    Returns: [(token, start position), ...]
    """
    t = text.lower()
    out = []
    for n in range(lo, hi + 1):
        for i in range(len(t) - n + 1):
            tok = t[i:i + n]
            if not tok.strip():
                continue
            out.append((tok, i))
    return out


def build():
    chunks = json.load(open(CHUNKS))
    db = lancedb.connect(DB)

    # 1) Tokenise per chunk → aggregate frequency and position per (term, chunk_id)
    tf = collections.defaultdict(lambda: collections.defaultdict(list))   # term -> cid -> [pos]
    doclen = {}
    for cid, c in enumerate(chunks):
        toks = tokenize(c["text"])
        doclen[cid] = len(toks)
        for tok, pos in toks:
            tf[tok][cid].append(pos)

    # 2) The term dictionary + IDF
    N = len(chunks)
    terms = sorted(tf.keys())
    tid = {t: i for i, t in enumerate(terms)}
    term_rows = []
    for t in terms:
        df = len(tf[t])
        idf = math.log((N - df + 0.5) / (df + 0.5) + 1.0)       # BM25 IDF
        term_rows.append({"term": t, "term_id": tid[t], "df": df, "idf": idf})

    # 3) postings — (term_id, chunk_id, tf, positions)
    post_rows = []
    for t in terms:
        for cid, poss in tf[t].items():
            post_rows.append({"term_id": tid[t], "chunk_id": cid,
                              "tf": len(poss), "positions": poss[:32]})

    # 4) Document lengths
    doc_rows = [{"chunk_id": cid, "doc": chunks[cid]["doc"], "num_tokens": doclen[cid]}
                for cid in range(N)]

    t1 = db.create_table("ix_terms", mode="overwrite", data=term_rows,
                         schema=pa.schema([pa.field("term", pa.string()),
                                           pa.field("term_id", pa.int32()),
                                           pa.field("df", pa.int32()),
                                           pa.field("idf", pa.float32())]))
    t2 = db.create_table("ix_postings", mode="overwrite", data=post_rows,
                         schema=pa.schema([pa.field("term_id", pa.int32()),
                                           pa.field("chunk_id", pa.int32()),
                                           pa.field("tf", pa.int32()),
                                           pa.field("positions", pa.list_(pa.int32()))]))
    t3 = db.create_table("ix_docs", mode="overwrite", data=doc_rows,
                         schema=pa.schema([pa.field("chunk_id", pa.int32()),
                                           pa.field("doc", pa.string()),
                                           pa.field("num_tokens", pa.int32())]))
    # A scalar index for lookup performance
    t1.create_index("term", replace=True, config=BTree())
    t2.create_index("term_id", replace=True, config=BTree())
    t3.create_index("chunk_id", replace=True, config=BTree())
    return t1, t2, t3, sum(doclen.values()) / N


class ManualBM25:
    """BM25 search over the hand-built inverted index.  k1 and b are adjustable."""

    def __init__(self, db, avglen, k1=K1, b=B):
        self.t = db.open_table("ix_terms")
        self.p = db.open_table("ix_postings")
        self.d = db.open_table("ix_docs")
        self.avglen = avglen
        self.k1, self.b = k1, b
        self.len_of = {r["chunk_id"]: r["num_tokens"]
                       for r in self.d.search().limit(999999).to_list()}
        self.doc_of = {r["chunk_id"]: r["doc"]
                       for r in self.d.search().limit(999999).to_list()}

    def lookup(self, term):
        """Which chunks hold this word, and how many times — queryable directly"""
        rows = self.t.search().where(f"term = '{term}'").limit(1).to_list()
        if not rows:
            return None, []
        tr = rows[0]
        posts = self.p.search().where(f"term_id = {tr['term_id']}").limit(99999).to_list()
        return tr, sorted(posts, key=lambda x: -x["tf"])

    def search(self, query, k=100):
        scores = collections.defaultdict(float)
        seen = set()
        for tok, _ in tokenize(query):
            if tok in seen:
                continue
            seen.add(tok)
            tr, posts = self.lookup(tok)
            if not tr:
                continue
            for po in posts:
                cid, f = po["chunk_id"], po["tf"]
                ln = self.len_of[cid]
                scores[cid] += tr["idf"] * (f * (self.k1 + 1)) / (
                    f + self.k1 * (1 - self.b + self.b * ln / self.avglen))
        top = sorted(scores.items(), key=lambda x: -x[1])[:k]
        return [(self.doc_of[c], s) for c, s in top]


if __name__ == "__main__":
    # Two writers on the same DB diverge silently (see the comments in kal_lock.py)
    with db_lock("manual_index"):
        t1, t2, t3, avglen = build()
        print(f"ix_terms     {t1.count_rows():>8,} rows  [term, term_id, df, idf]")
        print(f"ix_postings  {t2.count_rows():>8,} rows  [term_id, chunk_id, tf, positions]")
        print(f"ix_docs      {t3.count_rows():>8,} rows  [chunk_id, doc, num_tokens]")
        print(f"average document length {avglen:.1f} tokens\n")

        db = lancedb.connect(DB)
        m = ManualBM25(db, avglen)
        tr, posts = m.lookup("lancedb")
        print(f"■ 'lancedb' lookup — df={tr['df']} · idf={tr['idf']:.3f}")
        print(f"  {'chunk':>6}{'TF':>5}{'tokens':>7}  document")
        for po in posts[:8]:
            print(f"  {po['chunk_id']:>6}{po['tf']:>5}{m.len_of[po['chunk_id']]:>7}  "
                  f"{m.doc_of[po['chunk_id']].split('_',2)[-1][:38]}")
        print(f"  first occurrence positions: {posts[0]['positions'][:8]}")
