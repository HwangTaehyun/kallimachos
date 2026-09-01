#!/usr/bin/env python3
"""Tuning the 4 component weights — BM25 · chunk vectors · entity vectors · relation vectors.

Measurement conditions (kept identical to the earlier benchmark):
  queries    58 (49 lookup + 9 synthesis)
  gold set   991 fully judged pairs (0 unjudged → nDCG == condensed)
  metrics    nDCG@10 primary · R@20 · MRR secondary
  test       Wilcoxon signed-rank + Benjamini-Hochberg (q=0.05)
  baseline   pre-registered — fixed before looking at any result

Caution:
  The weights are swept **on the test set**, so it is not held out.
  → A split validation runs alongside: pick on half the queries, confirm on the other half.
"""
import os, sys, json, argparse, itertools, collections, random

# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(KAL_HOME, "bench"))
import numpy as np
from kal_search import KAL, minmax, model, PRESETS
from hybrid_bench import (ndcg, recall_at, rr, wilcoxon_signed_rank,
                          benjamini_hochberg, bootstrap_ci, cohen_dz)

QRELS = os.path.join(KAL_HOME, "bench/full_qrels.json")
TS_LU = os.path.join(KAL_HOME, "bench/testset_v2.json")
TS_HL = os.path.join(KAL_HOME, "bench/testset_highlevel.json")
# The gold set is keyed on vault flat names → they have to be converted to doc_id
BASELINE = (0.20, 0.80, 0.0, 0.0)      # ★ pre-registered: the previous best (BM25 + chunk CC .8)


def load_tasks(kal):
    flat2id = {r["path"][:-3].replace("/", "_"): d
               for d, r in kal.D.items() if r.get("origin", "vault") == "vault"}
    Q = json.load(open(QRELS))
    out = []
    for x in json.load(open(TS_HL))["queries"]:
        qid = "HL-" + x["id"][:14]
        if qid in Q:
            out.append({"id": qid, "q": x["q"], "kind": "synthesis",
                        "qrel": {flat2id[k]: v for k, v in Q[qid].items()
                                 if not k.startswith("_") and k in flat2id}})
    for x in json.load(open(TS_LU))["queries"]:
        qr = Q.get(x["id"], {d: 2 for d in x["gold"]})
        out.append({"id": x["id"], "q": x["q"], "kind": "lookup", "cat": x["cat"],
                    "qrel": {flat2id[k]: v for k, v in qr.items()
                             if not k.startswith("_") and k in flat2id}})
    return [t for t in out if t["qrel"]]


def precompute(kal, tasks, vault_only=True):
    """Compute the per-component scores once per query.  It makes the weight sweep fast.

    vault_only=True   restrict the corpus to the 97 documents the gold set covers.  0 unjudged → a controlled comparison.
    vault_only=False  the real-use corpus including session documents.  Unjudged documents
                      appear, so scoring uses a condensed list (score()'s condensed argument).

    Why both conditions are measured: the first asks "is the weight itself right", the second
    "does that weight hold up on the corpus actually in use".  Measure only the first and the tuning is a lab value.
    """
    keep = {d for d, r in kal.D.items() if r.get("origin", "vault") == "vault"}
    f = (lambda d: {k: v for k, v in d.items() if k in keep}) if vault_only else (lambda d: d)
    comp = {}
    for t in tasks:
        qv = model().encode(["query: " + t["q"]], normalize_embeddings=True)[0].tolist()
        comp[t["id"]] = [minmax(f(kal.bm25(t["q"], k=300))),
                         minmax(f(kal.chunk_vec(qv, k=300))),
                         minmax(f(kal.entity_vec(qv))),
                         minmax(f(kal.relation_vec(qv)))]
    return comp


def score(comp, tasks, w, sel=None, condensed=False):
    """With condensed=True, unjudged documents are removed from the ranking before scoring.

    Measuring nDCG directly on an incomplete pool treats 'unjudged = irrelevant', so
    performance appears to fall as documents are added, unrelated to reality.  TREC's and
    Sakai's condensed list is the standard treatment that removes that bias.
    """
    n10, r20, mrr = [], [], []
    for t in tasks:
        if sel and not sel(t):
            continue
        parts = comp[t["id"]]
        keys = set().union(*[set(p) for p in parts])
        s = {k: sum(wi * p.get(k, 0.0) for p, wi in zip(parts, w)) for k in keys}
        rk = [d for d, _ in sorted(s.items(), key=lambda x: -x[1])]
        if condensed:
            rk = [d for d in rk if d in t["qrel"]]
        rk = rk[:100]
        n10.append(ndcg(rk, t["qrel"], 10))
        r20.append(recall_at(rk, t["qrel"], 20))
        mrr.append(rr(rk, t["qrel"]))
    f = lambda v: sum(v) / len(v) if v else 0.0
    return f(n10), f(r20), f(mrr), n10


def grid():
    """A 4-component grid summing to 1.  0.05 steps give far too many, so the candidates are narrowed."""
    out = []
    for b in (0.0, 0.05, 0.10, 0.15, 0.20, 0.30):
        for c in (0.0, 0.20, 0.35, 0.45, 0.55, 0.70, 0.80):
            for e in (0.0, 0.10, 0.15, 0.20, 0.30):
                r = round(1.0 - b - c - e, 3)
                if -1e-9 <= r <= 0.60:
                    out.append((b, c, e, max(0.0, r)))
    return sorted(set(out))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", action="store_true", help="A/B split held-out validation")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--protocol", action="store_true",
                    help="the honest protocol — pick on the selection set, test once on the test set")
    ap.add_argument("--seeds", type=int, default=20,
                    help="how many times --protocol repeats the split (one split mixes in luck)")
    ap.add_argument("--check-auto", action="store_true",
                    help="verify whether the auto mode's rule is really a gain")
    ap.add_argument("--full-corpus", action="store_true",
                    help="condensed scoring on the real-use corpus including session documents")
    a = ap.parse_args()

    kal = KAL()
    tasks = load_tasks(kal)
    print(f"{len(tasks)} queries (synthesis {sum(1 for t in tasks if t['kind']=='synthesis')} / "
          f"lookup {sum(1 for t in tasks if t['kind']=='lookup')})")
    print(f"corpus documents {len(kal.D):,} · KG {'present' if kal.has_kg else 'absent'}")
    print(f"baseline (pre-registered) BM25 {BASELINE[0]} · chunk {BASELINE[1]} · "
          f"entity {BASELINE[2]} · relation {BASELINE[3]}\n")

    VO = not a.full_corpus
    CD = a.full_corpus                       # the real-use corpus → unjudged appear → condensed
    n_sess = sum(1 for r in kal.D.values() if r.get("origin") == "session")
    print(f"condition: {'97 vault documents, controlled' if VO else f'the whole corpus ({n_sess} sessions included) · condensed scoring'}\n")
    comp = precompute(kal, tasks, vault_only=VO)
    SC = lambda *ar, **kw: score(*ar, condensed=CD, **kw)
    G = grid()
    print(f"evaluating {len(G)} grid combinations…")
    rows = []
    for w in G:
        n, r, mr, per = SC(comp, tasks, w)
        rows.append((n, r, mr, w, per))
    rows.sort(key=lambda x: -x[0])

    bn, br, bm_, bper = SC(comp, tasks, BASELINE)
    print(f"\n{'rank':<4}{'BM25':>6}{'chunk':>6}{'ent':>6}{'rel':>6}"
          f"{'nDCG@10':>9}{'R@20':>8}{'MRR':>8}{'  synth':>8}{'lookup':>7}")
    print("-" * 74)
    for i, (n, r, mr, w, per) in enumerate(rows[:a.top], 1):
        hn, _, _, _ = SC(comp, tasks, w, lambda t: t["kind"] == "synthesis")
        ln, _, _, _ = SC(comp, tasks, w, lambda t: t["kind"] == "lookup")
        print(f"{i:<4}{w[0]:>6.2f}{w[1]:>6.2f}{w[2]:>6.2f}{w[3]:>6.2f}"
              f"{n:>9.3f}{r:>8.3f}{mr:>8.3f}{hn:>8.3f}{ln:>7.3f}")
    print(f"{'base':<4}{BASELINE[0]:>6.2f}{BASELINE[1]:>6.2f}{BASELINE[2]:>6.2f}"
          f"{BASELINE[3]:>6.2f}{bn:>9.3f}{br:>8.3f}{bm_:>8.3f}")

    # ── The honest protocol ──
    # The problem: the table above evaluates 146 combinations **on every query** and then picks
    #       the winner.  The grounds for picking and for testing are the same, so p is
    #       optimistic.  Simulated, "significant" comes out 82% of the time with no real difference.
    # The fix: split the queries into a selection set A and a test set B, pick on A alone and
    #       test once on B.  B took no part in the selection, so there is exactly one test —— no multiple comparison.
    #       To remove split luck it runs several times with different seeds and looks at the distribution.
    if a.protocol:
        print(f"\nthe honest protocol — pick on the selection set, test once on the test set "
              f"({a.seeds} splits)")
        deltas, wins, picks = [], 0, collections.Counter()
        for sd in range(a.seeds):
            rng = random.Random(1000 + sd)
            idx = list(range(len(tasks)))
            rng.shuffle(idx)
            A = {tasks[i]["id"] for i in idx[::2]}
            selA = lambda t: t["id"] in A
            selB = lambda t: t["id"] not in A
            best = max(G, key=lambda w: SC(comp, tasks, w, selA)[0])   # picked on A alone
            picks[best] += 1
            nB, _, _, perB = SC(comp, tasks, best, selB)               # measured once on B
            nB0, _, _, per0 = SC(comp, tasks, BASELINE, selB)
            deltas.append(nB - nB0)
            p, _ = wilcoxon_signed_rank(perB, per0)
            if p < 0.05 and nB > nB0:
                wins += 1
        ds = sorted(deltas)
        m = ds[len(ds) // 2]
        print(f"   test-set Δ (against the baseline)   median {m:+.3f} · "
              f"range [{ds[0]:+.3f}, {ds[-1]:+.3f}]")
        print(f"   significantly better in {wins} of {a.seeds} splits ({wins*100//a.seeds}%)")
        print(f"   splits with Δ>0               {sum(1 for d in ds if d > 0)}")
        top = picks.most_common(3)
        print(f"   weights picked on A, most common: " +
              " · ".join(f"{w} ×{c}" for w, c in top))
        print(f"   → this Δ is the honest score.  The table's Δ is measured on the data used to pick, so it runs high.")

    # Significance — the top candidates against the baseline
    # ⚠ This test looks at **only the top 8 after seeing all 146**.  BH corrects those 8 and not
    #   the 146-way search.  Read it together with 'BH over the whole grid' below and --protocol.
    print(f"\nsignificance — against the baseline · Wilcoxon+BH (q=0.05)   [the selection and test sets are the same]")
    cand = rows[:8]
    ps = [wilcoxon_signed_rank(c[4], bper)[0] for c in cand]
    rej, adj = benjamini_hochberg(ps, 0.05)
    print(f"  {'weights':26}{'Δ':>8}{'95%CI':>18}{'dz':>7}{'p(BH)':>9}   verdict")
    for (n, r, mr, w, per), aj, rj in zip(cand, adj, rej):
        lo, hi, dm = bootstrap_ci(per, bper)
        v = "better" if (rj and dm > 0) else ("worse" if rj else "tie")
        print(f"  {str(w):26}{dm:>+8.3f}  [{lo:+.3f},{hi:+.3f}]"
              f"{cohen_dz(per, bper):>7.2f}{aj:>9.4f}   {v}")

    # Verifying the auto rule — is a hand-written verdict worth anything
    if a.check_auto:
        # The rule removed from kal_search is replicated here.
        #  ⚠ HINTS stays in Korean.  These are the query words the rule actually matched
        #     on the 58-query Korean benchmark, and translating them would change what is
        #     being reproduced —— the point of this block is to reproduce the measurement
        #     that concluded the rule does harm, not to run a new one.
        # This test is the evidence that "the rule does harm", so deleting the test along with
        # the rule would make that judgement unreproducible.
        HINTS = ("어떻게", "왜", "차이", "비교", "무엇이", "관계", "영향", "원인",
                 "다른가", "작용", "전략", "방식", "구조", "정리", "전반", "종합",
                 "why", "how", "compare", "difference", "relationship")
        def pick_mode(q):
            if any(h in q.lower() for h in HINTS):
                return "synth"
            return "lookup" if len(q) <= 25 else "default"
        print(f"\n verifying the auto rule — is per-query mode selection a gain")
        # Names absent from the current PRESETS keep their old values so the comparison still works
        OLD = {"lookup": (0.15,0.20,0.10,0.55), "synth": (0.30,0.20,0.00,0.50)}
        P = lambda k: PRESETS.get(k) or OLD[k]
        always = {k: [] for k in ("default", "lookup", "synth")}
        auto, oracle, picks = [], [], collections.Counter()
        for t in tasks:
            per = {}
            for k in always:
                n, _, _, one = SC(comp, [t], P(k))
                per[k] = one[0]
                always[k].append(one[0])
            m = pick_mode(t["q"])
            picks[m] += 1
            auto.append(per.get(m, per["default"]))
            oracle.append(max(per.values()))
        f = lambda v: sum(v) / len(v)
        print(f"   {'strategy':<22}{'nDCG@10':>9}")
        print("   " + "-" * 31)
        for k, v in always.items():
            print(f"   always {k:<15}{f(v):>9.3f}")
        print(f"   {'auto (the rule)':<22}{f(auto):>9.3f}   selection distribution {dict(picks)}")
        print(f"   {'oracle (the ceiling)':<22}{f(oracle):>9.3f}   if the best were known per query")
        best_fixed = max(always, key=lambda k: f(always[k]))
        d = f(auto) - f(always[best_fixed])
        pv, _ = wilcoxon_signed_rank(auto, always[best_fixed])
        lo, hi, dm = bootstrap_ci(auto, always[best_fixed])
        print(f"\n   auto vs the best fixed ({best_fixed})  Δ{dm:+.3f}  "
              f"[{lo:+.3f},{hi:+.3f}]  p={pv:.4f}  "
              f"{'a gain' if pv < 0.05 and dm > 0 else 'no gain — a fixed mode is enough'}")
        print(f"   what auto misses against the oracle  {f(oracle)-f(auto):+.3f}")

    # BH over the whole grid —— what corrected only 8 now covers all 146
    allp = [wilcoxon_signed_rank(r[4], bper)[0] for r in rows]
    rej_all, _ = benjamini_hochberg(allp, 0.05)
    n_sig = sum(1 for r, j in zip(rows, rej_all) if j and (sum(r[4]) / len(r[4])) > bn)
    print(f"\nBH applied over all {len(rows)} grid points → {n_sig} significantly better "
          f"({n_sig*100//len(rows)}%)")
    print(f"   More conservative than correcting the top 8 alone.  If {n_sig} still survive,"
          f" the effect is not search noise.")

    # The held-out split
    if a.split:
        rng = random.Random(11)
        idx = list(range(len(tasks)))
        rng.shuffle(idx)
        A = {tasks[i]["id"] for i in idx[::2]}
        selA = lambda t: t["id"] in A
        selB = lambda t: t["id"] not in A
        best_on_A = max(G, key=lambda w: SC(comp, tasks, w, selA)[0])
        nA, _, _, _ = SC(comp, tasks, best_on_A, selA)
        nB, rB, mB, _ = SC(comp, tasks, best_on_A, selB)
        nB_base, _, _, _ = SC(comp, tasks, BASELINE, selB)
        print(f"\nheld-out validation (queries split in half)")
        print(f"  best picked on A: {best_on_A}   A nDCG@10 {nA:.3f}")
        print(f"  confirmed on B:   nDCG@10 {nB:.3f}  (baseline {nB_base:.3f}, Δ {nB-nB_base:+.3f})")
        print(f"  → {'generalises' if nB > nB_base else 'overfitting suspected'}")

    json.dump([{"w": list(w), "ndcg10": n, "r20": r, "mrr": mr}
               for n, r, mr, w, _ in rows[:40]],
              open(os.path.join(KAL_HOME, "bench/tune_alpha_results%s.json") % ("_full" if a.full_corpus else ""), "w"), indent=1)
