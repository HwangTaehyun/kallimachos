#!/usr/bin/env python3
"""Ablation experiments on **the parameters other than the weights**.

tune_alpha.py deals only with the ratio between the 4 components.  But the retriever holds
more hand-picked values, and they sat there with no basis.  Here they are actually measured.

  ① the 1-hop expansion factor   the weight given, inside relation_vec, to documents where a
                                 relation's endpoint entities appear.  It was set to 0.5 arbitrarily.
  ② candidate depth per component  how many top results each component contributes to the fusion (k).
  ③ the answer document count N    how many are handed over as evidence.  Where recall saturates.
  ④ the mode-selection rule        see tune_alpha.py --check-auto (not covered here)

Measured (a 561-document transcript corpus · 58 queries · a 991-pair gold set, 2026-08-17):
  ① 0.0 → -0.025 against 0.754 (p<0.001).  1.0 → +0.004 (p=0.025)
     → expansion is essential.  There is no basis for discounting an indirect path, so 1.0 is right
  ② 60/60/15/20 is best at 0.761.  120/120/30/40 (the default) gives 0.743; deeper is worse
     → RRF accumulates, so more depth lets weakly related documents push out the top
  ③ Recall@20 saturates at 0.939.  N=10 misses 22%
     → 15–20 documents as answer evidence

Usage:
  python ablate_params.py            # everything
  python ablate_params.py --only hop
"""
import os, sys, json, argparse, collections


# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(KAL_HOME, "bench"))
from tune_alpha import load_tasks
from kal_search import KAL, minmax, model
from hybrid_bench import ndcg, recall_at, rr, wilcoxon_signed_rank, bootstrap_ci

W = (0.20, 0.20, 0.20, 0.40)          # the default weights tune_alpha picked
HOPS = (0.0, 0.25, 0.5, 1.0)
DEPTHS = [(60, 60, 15, 20), (120, 120, 30, 40), (120, 120, 60, 80),
          (300, 300, 30, 40), (300, 300, 100, 120)]
NS = (3, 5, 8, 10, 15, 20, 30)


def vault_only(kal):
    """Restrict the corpus to the documents the gold set covers — 0 unjudged makes the comparison controlled."""
    keep = {d for d, r in kal.D.items() if r.get("origin", "vault") == "vault"}
    return lambda d: {k: v for k, v in d.items() if k in keep}


def rel_vec(kal, qv, k=40, hop=0.5):
    """The same as kal_search.relation_vec, with the hop factor lifted out as an argument."""
    out = collections.defaultdict(float)
    for rank, r in enumerate(kal.R.search(qv).limit(k).to_list(), 1):
        w = 1.0 / (60 + rank)
        for d in r["doc_ids"]:
            out[d] += w
        if hop:
            for eid in (r["src_id"], r["tgt_id"]):
                for d in kal.ent_docs.get(eid, []):
                    out[d] += w * hop
    return dict(out)


def rank_all(kal, tasks, f, depth, hop):
    kb, kc, ke, kr = depth
    out = []
    for t in tasks:
        qv = model().encode(["query: " + t["q"]], normalize_embeddings=True)[0].tolist()
        parts = [minmax(f(kal.bm25(t["q"], k=kb))), minmax(f(kal.chunk_vec(qv, k=kc))),
                 minmax(f(kal.entity_vec(qv, k=ke))), minmax(f(rel_vec(kal, qv, kr, hop)))]
        keys = set().union(*[set(p) for p in parts])
        s = {x: sum(w * p.get(x, 0.0) for p, w in zip(parts, W)) for x in keys}
        out.append(([d for d, _ in sorted(s.items(), key=lambda y: -y[1])[:100]], t["qrel"]))
    return out


def mean(v):
    return sum(v) / len(v) if v else 0.0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["hop", "depth", "n"])
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    kal = KAL()
    tasks = load_tasks(kal)
    f = vault_only(kal)
    n_sess = sum(1 for r in kal.D.values() if r.get("origin") == "session")
    print(f"{len(tasks)} queries · corpus {len(kal.D):,} documents ({n_sess} sessions) · "
          f"control: vault only\n")
    out = {}

    if a.only in (None, "hop"):
        print("① the 1-hop expansion factor — the weight for documents where a relation's endpoints appear")
        print(f"   {'factor':<8}{'nDCG@10':>9}{'R@20':>8}{'MRR':>8}")
        per = {}
        for h in HOPS:
            rk = rank_all(kal, tasks, f, (120, 120, 30, 40), h)
            per[h] = [ndcg(r, q, 10) for r, q in rk]
            print(f"   {h:<8.2f}{mean(per[h]):>9.3f}"
                  f"{mean([recall_at(r,q,20) for r,q in rk]):>8.3f}"
                  f"{mean([rr(r,q) for r,q in rk]):>8.3f}")
        base = per[0.5]
        print(f"\n   against 0.5 (current)")
        for h in HOPS:
            if h == 0.5:
                continue
            p, _ = wilcoxon_signed_rank(per[h], base)
            lo, hi, dm = bootstrap_ci(per[h], base)
            v = "better" if p < 0.05 and dm > 0 else ("worse" if p < 0.05 else "tie")
            print(f"     {h:.2f}   Δ{dm:+.3f}  [{lo:+.3f},{hi:+.3f}]  p={p:.4f}   {v}")
        out["hop"] = {str(h): mean(v) for h, v in per.items()}
        print()

    if a.only in (None, "depth"):
        print("② candidate depth per component — how many top results each contributes to the fusion")
        print(f"   {'BM25':>6}{'chunk':>6}{'ent':>6}{'rel':>6}{'nDCG@10':>10}{'R@20':>8}")
        d_res = {}
        for d in DEPTHS:
            rk = rank_all(kal, tasks, f, d, 0.5)
            n10 = mean([ndcg(r, q, 10) for r, q in rk])
            d_res[str(d)] = n10
            print(f"   {d[0]:>6}{d[1]:>6}{d[2]:>6}{d[3]:>6}{n10:>10.3f}"
                  f"{mean([recall_at(r,q,20) for r,q in rk]):>8.3f}")
        print("   → RRF accumulates, so more depth lets weakly related documents push out the top")
        out["depth"] = d_res
        print()

    if a.only in (None, "n"):
        print("③ the number of documents N handed to the answer — where recall saturates")
        rk = rank_all(kal, tasks, f, (120, 120, 30, 40), 0.5)
        print(f"   {'N':>4}{'Recall@N':>11}{'nDCG@N':>9}")
        n_res = {}
        for N in NS:
            r = mean([recall_at(x, q, N) for x, q in rk])
            n_res[N] = r
            print(f"   {N:>4}{r:>11.3f}{mean([ndcg(x,q,N) for x,q in rk]):>9.3f}")
        sat = next((N for N in NS if n_res[N] >= 0.93), NS[-1])
        print(f"   → recall 0.93 reached at N={sat}.  Below that, relevant documents are meaningfully missed")
        out["n"] = n_res

    if a.json:
        json.dump(out, open(os.path.join(KAL_HOME, "bench/ablate_params.json"), "w"),
                  ensure_ascii=False, indent=1)
