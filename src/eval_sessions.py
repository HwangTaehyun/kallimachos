#!/usr/bin/env python3
"""Session document search quality — a known-item evaluation.

Why it is needed separately
  The existing gold set (991 pairs) judges only 97 vault documents.  It says nothing about how
  well session documents are found.  Condensed scoring only looks at "the order among judged documents".

Method — known-item retrieval (a scaled-down TREC known-item task)
  Each distilled document's frontmatter `why_captured` is one sentence on "why a future me
  would look this up again".  That is **a natural query aimed at that document**.
  It goes in as the query and that one document is the answer.  Metrics: MRR · Recall@k · nDCG@10.

Honest limits — why these numbers must not be over-trusted
  L-a  the query and the answer document were made by the same LLM from the same source.  The
       vocabulary overlaps, so there is leakage favouring BM25.  Use it for **comparing
       weights**, never for the absolute value.
  L-b  there is exactly one right answer.  In reality another session on the same subject may
       also be relevant and is counted wrong → it lowers every method uniformly (harmless for ranking).
  L-c  no human judged any of it.

Usage:
  python eval_sessions.py                 # the default 200-query sample
  python eval_sessions.py --n 400 --json
"""
import os, sys, json, random, argparse

# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(KAL_HOME, "bench"))
from kal_search import KAL, minmax, model, PRESETS
from hybrid_bench import (ndcg, recall_at, rr, wilcoxon_signed_rank,
                          benjamini_hochberg, bootstrap_ci, cohen_dz)

# Where the vault lives.  Mounted at /vault inside the container (see docker-compose).
#  ⚠ **The default is not the author's path.**  A stranger does not have that path, and then
#     "no session documents" appears —— with no way to tell a wrong path from a genuine absence
#     (deep review 2026-08-25).
VAULT = os.environ.get("KAL_VAULT") or os.environ.get("VAULT_DIR")
if not VAULT:
    raise SystemExit(
        "  ❌ Please set KAL_VAULT —— the folder holding your notes.\n"
        "     For example:  KAL_VAULT=~/my-notes python src/eval_sessions.py")
VAULT = os.path.expanduser(VAULT)
SESSION_DIR = "raw/conversations/sessions/"


def build_tasks(kal, n, seed=7):
    """A session document's why_captured → the query; that document → the answer."""
    import re
    out = []
    for did, r in kal.D.items():
        if r.get("origin") != "session":
            continue
        p = r.get("abs_path") or os.path.join(VAULT, r["path"])
        try:
            head = open(p, encoding="utf-8", errors="ignore").read()[:1500]
        except OSError:
            continue
        m = re.search(r"^why_captured:\s*\"?(.+?)\"?\s*$", head, re.M)
        if not m:
            continue
        q = m.group(1).strip()
        if len(q) < 15:                       # too short to work as a query
            continue
        out.append({"id": f"KI-{did}", "q": q, "qrel": {did: 3}, "kind": "known-item"})
    random.Random(seed).shuffle(out)
    return out[:n]


def evaluate(kal, tasks, weights):
    comp = {}
    for t in tasks:
        qv = model().encode(["query: " + t["q"]], normalize_embeddings=True)[0].tolist()
        comp[t["id"]] = [minmax(kal.bm25(t["q"], k=300)), minmax(kal.chunk_vec(qv, k=300)),
                         minmax(kal.entity_vec(qv)), minmax(kal.relation_vec(qv))]
    res = {}
    for name, w in weights.items():
        n10, r10, mrr = [], [], []
        for t in tasks:
            parts = comp[t["id"]]
            keys = set().union(*[set(p) for p in parts])
            s = {k: sum(wi * p.get(k, 0.0) for p, wi in zip(parts, w)) for k in keys}
            rk = [d for d, _ in sorted(s.items(), key=lambda x: -x[1])[:100]]
            n10.append(ndcg(rk, t["qrel"], 10))
            r10.append(recall_at(rk, t["qrel"], 10))
            mrr.append(rr(rk, t["qrel"]))
        f = lambda v: sum(v) / len(v)
        res[name] = (f(n10), f(r10), f(mrr), n10)
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    kal = KAL()
    tasks = build_tasks(kal, a.n)
    if not tasks:
        raise SystemExit("no session documents — rebuild after promote_distilled.py")
    n_sess = sum(1 for r in kal.D.values() if r.get("origin") == "session")
    print(f"{len(tasks)} known-item queries · corpus {len(kal.D):,} documents ({n_sess} sessions)")
    print(f"query = each document's why_captured · answer = that one document\n")

    W = {k: v for k, v in PRESETS.items() if k in ("legacy", "keyword", "vector",
                                                   "graph", "lookup", "default", "synth")}
    res = evaluate(kal, tasks, W)
    order = sorted(res, key=lambda k: -res[k][2])       # by MRR
    print(f"  {'mode':<10}{'BM25':>6}{'chunk':>6}{'ent':>6}{'rel':>6}"
          f"{'MRR':>9}{'R@10':>8}{'nDCG@10':>9}")
    print("  " + "-" * 60)
    for k in order:
        w = W[k]
        n10, r10, mrr, _ = res[k]
        print(f"  {k:<10}{w[0]:>6.2f}{w[1]:>6.2f}{w[2]:>6.2f}{w[3]:>6.2f}"
              f"{mrr:>9.3f}{r10:>8.3f}{n10:>9.3f}")

    base = "legacy"                                     # the pre-registered baseline
    print(f"\n  significance — against '{base}' (pre-tuning) · Wilcoxon+BH (q=0.05)")
    cand = [k for k in order if k != base]
    ps = [wilcoxon_signed_rank(res[k][3], res[base][3])[0] for k in cand]
    rej, adj = benjamini_hochberg(ps, 0.05)
    for k, aj, rj in zip(cand, adj, rej):
        lo, hi, dm = bootstrap_ci(res[k][3], res[base][3])
        v = "better" if (rj and dm > 0) else ("worse" if rj and dm < 0 else "tie")
        print(f"    {k:<10}Δ{dm:>+7.3f}  [{lo:+.3f},{hi:+.3f}]"
              f"  dz {cohen_dz(res[k][3], res[base][3]):>5.2f}  p(BH) {aj:.4f}   {v}")

    print(f"\n  ⚠️ the queries and answers were made by the same LLM from the same source (vocabulary leakage).")
    print(f"     Read it as a relative comparison between weights, never as an absolute.  See L-a–c at the top of this script.")

    if a.json:
        json.dump({k: {"ndcg10": v[0], "r10": v[1], "mrr": v[2], "w": list(W[k])}
                   for k, v in res.items()},
                  open(os.path.join(KAL_HOME, "bench/eval_sessions.json"), "w"), indent=1)
