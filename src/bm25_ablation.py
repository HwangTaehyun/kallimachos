# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
import os
#!/usr/bin/env python3
"""BM25 component ablation — testing the claim that "in RAG, BM25 degenerates into IDF-only".

The claim (moonzoo blog, 2025-11-04):
  · with short, uniform chunks TF is 0/1 and carries no discriminating power
  · length normalisation is always near 1 and therefore meaningless
  → BM25 = effectively an IDF lookup

Measured (this vault, 500-char chunks · ngram 2-3):
  · TF=1 is 72.3%, TF≥2 is 27.7%   → TF is more alive than the claim allows
  · length coefficient of variation 0.177, the 10th-to-90th percentile ratio 1.1× → length normalisation is nearly meaningless

What is actually switched off here:
  full      k1=1.2 b=0.75   standard BM25
  no-len    k1=1.2 b=0.0    length normalisation removed
  no-tf     k1=0             the TF term becomes the constant 1 → only the IDF sum remains (= IDF-only)
  binary    TF pinned to 1   the assumption "TF is only ever 0/1", implemented by force
"""
import json, math, sys, collections

# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
sys.path.insert(0, os.path.join(KAL_HOME, "bench"))
import lancedb
from hybrid_bench import ndcg, recall_at, rr, load_tasks

DB = os.path.join(KAL_HOME, "lancedb")
FULL = json.load(open(os.path.join(KAL_HOME, "bench/full_qrels.json")))


def tokenize(text, lo=2, hi=3):
    t = text.lower()
    return [t[i:i + n] for n in range(lo, hi + 1) for i in range(len(t) - n + 1) if t[i:i + n].strip()]


class BM25:
    def __init__(self, db):
        T = db.open_table("ix_terms").search().limit(999999).to_list()
        P = db.open_table("ix_postings").search().limit(999999).to_list()
        D = db.open_table("ix_docs").search().limit(999999).to_list()
        self.idf = {r["term"]: r["idf"] for r in T}
        self.tid = {r["term"]: r["term_id"] for r in T}
        self.post = collections.defaultdict(list)
        for r in P:
            self.post[r["term_id"]].append((r["chunk_id"], r["tf"]))
        self.len = {r["chunk_id"]: r["num_tokens"] for r in D}
        self.doc = {r["chunk_id"]: r["doc"] for r in D}
        self.avg = sum(self.len.values()) / len(self.len)

    def search(self, q, k1=1.2, b=0.75, binary=False, k=100):
        sc = collections.defaultdict(float)
        for tok in set(tokenize(q)):
            if tok not in self.idf:
                continue
            idf = self.idf[tok]
            for cid, f in self.post[self.tid[tok]]:
                if binary:
                    f = 1
                if k1 == 0:                       # the TF term is disabled → the IDF sum
                    sc[cid] += idf
                else:
                    ln = self.len[cid]
                    sc[cid] += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * ln / self.avg))
        best = {}
        for cid, s in sc.items():
            d = self.doc[cid]
            if s > best.get(d, -1e18):
                best[d] = s
        return [d for d, _ in sorted(best.items(), key=lambda t: -t[1])[:k]]


if __name__ == "__main__":
    bm = BM25(lancedb.connect(DB))
    tasks = load_tasks()
    for t in tasks:
        t["qrel"] = FULL.get(t["id"], t["qrel"])
    print(f"{len(tasks)} queries · 991 fully judged pairs · avg doc len {bm.avg:.0f}\n")

    VARIANTS = [
        ("full  k1=1.2 b=0.75", dict(k1=1.2, b=0.75)),
        ("no-len  b=0        ", dict(k1=1.2, b=0.0)),
        ("no-tf   k1=0 (IDF only)", dict(k1=0.0, b=0.0)),
        ("binary TF≡1        ", dict(k1=1.2, b=0.75, binary=True)),
        ("k1=0.5 (TF saturates early)", dict(k1=0.5, b=0.75)),
        ("k1=2.0 (TF saturates late)", dict(k1=2.0, b=0.75)),
    ]
    base = None
    print(f"{'variant':24}{'nDCG@10':>9}{'R@20':>8}{'MRR':>8}{'  vs full':>11}")
    print("-" * 62)
    for name, kw in VARIANTS:
        rows = []
        for t in tasks:
            rk = bm.search(t["q"], **kw)
            rows.append({"n": ndcg(rk, t["qrel"], 10), "r": recall_at(rk, t["qrel"], 20),
                         "m": rr(rk, t["qrel"])})
        a = lambda k: sum(x[k] for x in rows) / len(rows)
        if base is None:
            base = a("n")
        print(f"{name:24}{a('n'):>9.3f}{a('r'):>8.3f}{a('m'):>8.3f}{a('n')-base:>+11.3f}")
