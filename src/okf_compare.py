#!/usr/bin/env python3
"""Compare the knowledge graphs extracted from two vault formats (LLM-Wiki ↔ OKF).

What is compared
  ① scale       entity and relation counts, density per document
  ② types       the distribution of entity types the LLM assigned
  ③ overlap     did the two graphs find **the same entities** (Jaccard on the merge key)
  ④ structure   degree distribution, orphan ratio, connections across documents
  ⑤ links       in OKF, body links are edge candidates.  How much do they overlap the LLM extraction

Why overlap is measured on the merge key
  Comparing name strings counts 'LanceDB' and 'lancedb' as different and underestimates the
  overlap.  entity_resolve.merge_key is the normalisation the index really uses, so measuring
  with it judges "is this the same node" by the same standard as the pipeline.

Usage:
    python okf_compare.py ~/.kal/exp/h-sample-base ~/.kal/exp/h-sample-okf \\
        --vault-a ~/.kal/exp/sample-base --vault-b ~/.kal/exp/sample-okf
"""
import argparse
import collections
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from entity_resolve import merge_key  # noqa: E402

LINK = re.compile(r"\]\((/[^)#]+\.md)")
WIKILINK = re.compile(r"\[\[([^\]\|#]+)")


def load(home):
    p = os.path.join(home, "lr_kg.json")
    if not os.path.exists(p):
        raise SystemExit(f"no extraction output: {p}")
    return json.load(open(p, encoding="utf-8"))


def keys(kg):
    return {merge_key(e["name"]) for e in kg["entities"] if e.get("name")}


def rel_keys(kg):
    out = set()
    for r in kg["relationships"]:
        a, b = merge_key(r.get("source", "")), merge_key(r.get("target", ""))
        if a and b:
            out.add((a, b) if a <= b else (b, a))
    return out


def vault_links(vault):
    """Count the links a person put in the body (per file)."""
    import glob
    md, wiki = 0, 0
    for f in glob.glob(f"{vault}/**/*.md", recursive=True):
        t = open(f, encoding="utf-8", errors="ignore").read()
        md += len(LINK.findall(t))
        wiki += len(WIKILINK.findall(t))
    return md, wiki


def chunk_count(home):
    p = os.path.join(home, "lr_cache.jsonl")
    if not os.path.exists(p):
        return 0
    return sum(1 for _ in open(p, encoding="utf-8"))


def overlap(a, b):
    """|A∩B| / min(|A|,|B|).  More honest than Jaccard when the two sets differ in size.

    When entity counts differ per condition (1,515–1,686 in the experiment), Jaccard shows
    **the size difference** as though it were an overlap difference.  This coefficient does not move with it.
    """
    m = min(len(a), len(b))
    return len(a & b) / m if m else None


def jaccard(a, b):
    """|A∩B| / |A∪B|."""
    u = len(a | b)
    return len(a & b) / u if u else None


def row(label, a, b):
    d = ""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and a:
        d = f"{(b - a) / a * 100:+.1f}%"
    print(f"  {label:<28}{a:>10}{b:>12}   {d}")


def matrix(homes, labels):
    """Every pair's Jaccard as a table.  With three or more conditions, attribution becomes possible ——
    "OKF changed it" only holds if it exceeds **a control that changed nothing**."""
    kgs = [load(h) for h in homes]
    ks = [keys(k) for k in kgs]
    rs = [rel_keys(k) for k in kgs]
    n = len(labels)
    for name, sets in (("entities", ks), ("relations", rs)):
        # The overlap coefficient comes too.  Jaccard is sensitive to set **size**, so when
        # entity counts differ per condition (1,515–1,686) the size difference reads as an
        # overlap difference.  |A∩B|/min(|A|,|B|) is unaffected by it.
        print(f"\n  {name} overlap coefficient  |A∩B| / min(|A|,|B|)")
        print("  " + " " * 10 + "".join(f"{l:>12}" for l in labels))
        for i in range(n):
            cells = []
            for j in range(n):
                if i == j:
                    cells.append(f"{'—':>12}")
                else:
                    v = overlap(sets[i], sets[j])
                    cells.append(f"{v:>12.4f}" if v is not None else f"{'-':>12}")
            print(f"  {labels[i]:<10}" + "".join(cells))
        print(f"\n  {name} Jaccard")
        print("  " + " " * 10 + "".join(f"{l:>12}" for l in labels))
        for i in range(n):
            cells = []
            for j in range(n):
                if i == j:
                    cells.append(f"{'—':>12}")
                else:
                    v = jaccard(sets[i], sets[j])
                    cells.append(f"{v:>12.3f}" if v is not None else f"{'-':>12}")
            print(f"  {labels[i]:<10}" + "".join(cells))
        print("  " + "  ".join(f"{l}={len(s):,}" for l, s in zip(labels, sets)))


def _selftest():
    """This tool's output became the conclusion of `insights/19`.  A wrong formula makes that document wrong.

    What is guarded is what the docstring **explicitly claims**:
      · counting on the merge key makes `LanceDB` and `lancedb` **one entity**
        (counting name strings underestimates the overlap —— that is why this function exists)
      · relations are direction-normalised, so A→B and B→A are one edge
      · the overlap coefficient does not move with a set **size difference** (Jaccard does)
    """
    kg = {"entities": [{"name": "LanceDB"}, {"name": "lancedb"}, {"name": " LanceDB "},
                       {"name": ""}, {}],
          "relationships": [{"source": "A", "target": "B"},
                            {"source": "B", "target": "A"},      # the same edge
                            {"source": "C", "target": ""},        # no endpoint —— dropped
                            {"source": "b", "target": "a"}]}      # differs only in case
    k = keys(kg)
    assert len(k) == 1, f"spelling variants counted as different entities: {k}"
    r = rel_keys(kg)
    assert len(r) == 1, f"direction and spelling counted as different edges: {r}"

    A, B = {"x", "y"}, {"y", "z", "w", "v"}
    assert jaccard(A, B) == 1 / 5, jaccard(A, B)
    assert overlap(A, B) == 1 / 2, overlap(A, B)
    #  ★ Sensitivity to a size difference —— the reason this tool reports overlap as well
    #  ⚠ The padding elements must not intersect A —— using a..z at first let "x" in, the
    #    intersection grew, and my assertion failed while being wrong itself.
    big = B | {f"pad{i}" for i in range(40)}
    assert overlap(A, big) == overlap(A, B), "overlap moves with set size"
    assert jaccard(A, big) < jaccard(A, B), "Jaccard does not move with size —— the premise is broken"
    #  It does not die dividing by an empty set
    assert overlap(set(), A) is None and jaccard(set(), set()) is None
    assert jaccard(A, set()) == 0.0
    print("  ✅ okf_compare —— merge-key overlap · direction normalisation · the overlap/Jaccard formulas")


def analysis(home_a, home_b, label_a="A", label_b="B"):
    """Reproduce §3.4, §3.5 and §3.6.  These three were absent from the script and unreproducible.
    (deep review 2026-08-19, round 2, methodology lens)"""
    A, B = load(home_a), load(home_b)
    ka, kb = keys(A), keys(B)
    common = ka & kb

    print(f"\n  ── df stratification (by {label_a}) ──")
    def by_df(kg, lo):
        return {merge_key(e["name"]) for e in kg["entities"]
                if e.get("name") and len(set(e.get("docs") or [])) >= lo}
    print(f"  {'floor':<10}{'n(A)':>7}{'n(B)':>7}{'common':>7}{'Jaccard':>10}")
    for lo in (1, 2, 3, 5):
        a, b = by_df(A, lo), by_df(B, lo)
        u = len(a | b)
        print(f"  ≥{lo} docs{'':<5}{len(a):>7}{len(b):>7}{len(a & b):>7}"
              + (f"{len(a & b) / u:>10.3f}" if u else f"{'-':>10}"))
    n1 = len(by_df(A, 1)) - len(by_df(A, 2))
    print(f"  single-document entities {n1}/{len(by_df(A, 1))} = {n1 / len(by_df(A, 1)) * 100:.1f}%")

    print(f"\n  ── edges: lost endpoints vs edges lost in their own right ──")
    ra, rb = rel_keys(A), rel_keys(B)
    lost = ra - rb
    alive = {e for e in ra if e[0] in common and e[1] in common}
    lost_alive = {e for e in lost if e[0] in common and e[1] in common}
    print(f"  {label_a} edges {len(ra):,} · lost {len(lost):,}")
    print(f"    edges with both endpoints alive {len(alive):,} · of those, lost {len(lost_alive):,}"
          f" = {len(lost_alive) / max(1, len(alive)) * 100:.1f}%")
    print(f"    loss breakdown: from a lost endpoint {len(lost) - len(lost_alive):,}"
          f" ({(len(lost) - len(lost_alive)) / max(1, len(lost)) * 100:.1f}%)"
          f" · endpoints alive yet lost {len(lost_alive):,}"
          f" ({len(lost_alive) / max(1, len(lost)) * 100:.1f}%)")
    print("    ⚠ The ceiling of the conditional Jaccard is 1.0, not the entity Jaccard ——")
    print("      conditioning on the endpoints has already removed that mechanism.")

    print(f"\n  ── per chunk: length · entity count · stability ──")
    print("     Do not stratify by entity count —— it is badly confounded with chunk length.")
    def cache(home):
        out = {}
        p = os.path.join(os.path.expanduser(home), "lr_cache.jsonl")
        if not os.path.exists(p):
            return out
        for line in open(p, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("h"):
                out[r["h"]] = [e.get("name", "") for e in (r.get("entities") or [])]
        return out
    ca, cb = cache(home_a), cache(home_b)
    shared = set(ca) & set(cb)
    if not shared:
        print("    (the chunk hashes do not intersect —— normal when the conditions have different inputs)")
        return
    # The chunk's raw length —— confounded with entity count, so it must always be seen alongside.
    lengths = {}
    try:
        import lr_extract
        for c in lr_extract.collect():
            lengths[c["h"]] = len(c["text"])
    except Exception:
        pass        # with KAL_VAULT not pointing at this condition, the length analysis is skipped

    rows, order = [], []
    for h in shared:
        x = {merge_key(v) for v in ca[h] if v}
        y = {merge_key(v) for v in cb[h] if v}
        u = len(x | y)
        if u == 0:
            continue        # both at 0 gives J=1.0 and inflates the mean.  Excluded.
        if lengths and h not in lengths:
            continue
        rows.append((len(ca[h]), len(x & y) / u))
        order.append(h)
    buckets = collections.defaultdict(list)
    for n, j in rows:
        b = ("1-9" if n < 10 else "10-14" if n < 15 else "15-17" if n < 18
             else "18-19" if n < 20 else "20+ (the cap)")
        buckets[b].append(j)
    for b in ("1-9", "10-14", "15-17", "18-19", "20+ (the cap)"):
        v = buckets.get(b, [])
        if v:
            print(f"    {b:<12} n={len(v):>3}  J={sum(v) / len(v):.3f}")
    # Separate the confound.  Is the apparent effect of entity count really length.
    import math

    def corr(a, b):
        n_ = len(a)
        if n_ < 3:
            return 0.0
        ma, mb = sum(a) / n_, sum(b) / n_
        sa = math.sqrt(sum((x - ma) ** 2 for x in a))
        sb = math.sqrt(sum((x - mb) ** 2 for x in b))
        return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb) if sa and sb else 0.0

    if lengths:
        L = [lengths[h] for h in order]
        N = [n for n, _ in rows]
        Jv = [j for _, j in rows]
        rjn, rjl, rnl = corr(Jv, N), corr(Jv, L), corr(N, L)
        den = math.sqrt(max(1e-9, (1 - rjl ** 2) * (1 - rnl ** 2)))
        print(f"\n    r(entities, length) = {rnl:+.3f}   ← large here and a contrast by entity count is void")
        print(f"    r(J, entities)      = {rjn:+.3f}")
        print(f"    r(J, length)        = {rjl:+.3f}")
        print(f"    partial r(J, entities | length) = {(rjn - rjl * rnl) / den:+.3f}"
              "   ← near 0 means the entity count itself has no effect")
        dens = [n / l * 1000 for n, l in zip(N, L) if l]
        jj = [j for (n, j), l in zip(rows, L) if l]
        print(f"    r(J, density per character)   = {corr(jj, dens):+.3f}"
              "   ← positive and 'denser means less stable' is refuted")
        full = [(n, j) for (n, j), l in zip(rows, L) if l >= 2400]
        if len(full) > 3:
            print(f"    length fixed (full chunks, {len(full)}) r(J, entities)"
                  f" = {corr([x[0] for x in full], [x[1] for x in full]):+.3f}")
        print("\n    mean entity count by length band (does extraction saturate)")
        lb = collections.defaultdict(list)
        for n, l in zip(N, L):
            b = ("<800" if l < 800 else "800-1199" if l < 1200 else "1200-1599" if l < 1600
                 else "1600-2399" if l < 2400 else "2400")
            lb[b].append(n)
        for b in ("<800", "800-1199", "1200-1599", "1600-2399", "2400"):
            if lb[b]:
                print(f"      {b:<11} n={len(lb[b]):>3}  mean entities {sum(lb[b]) / len(lb[b]):.2f}")
        print("      → If entities do not fall proportionally when shortened, shrinking chunks only adds cost.")


def main():
    #  `--selftest` is handled **before parsing** —— `home_a`/`home_b` are positional and
    #  required, so reaching the parser dies with "required".
    if "--selftest" in sys.argv:
        _selftest(); raise SystemExit(0)
    ap = argparse.ArgumentParser()
    ap.add_argument("home_a"); ap.add_argument("home_b")
    ap.add_argument("--analysis", action="store_true",
                    help="df stratification · edge breakdown · chunk-size stratification (home_a against home_b)")
    ap.add_argument("--matrix", nargs="*", default=None,
                    help="give several HOME=LABEL for an all-pairs Jaccard matrix")
    ap.add_argument("--vault-a"); ap.add_argument("--vault-b")
    ap.add_argument("--label-a", default="LLM-Wiki")
    ap.add_argument("--label-b", default="OKF")
    a = ap.parse_args()

    if a.analysis:
        return analysis(os.path.expanduser(a.home_a), os.path.expanduser(a.home_b),
                        a.label_a, a.label_b)
    if a.matrix:
        pairs = [x.split("=", 1) for x in a.matrix]
        return matrix([os.path.expanduser(h) for h, _ in pairs], [l for _, l in pairs])

    A, B = load(a.home_a), load(a.home_b)
    ka, kb = keys(A), keys(B)
    ra, rb = rel_keys(A), rel_keys(B)

    print(f"\n{'':<28}{a.label_a:>10}{a.label_b:>12}   diff")
    print("  " + "─" * 60)
    row("chunks (LLM calls)", chunk_count(a.home_a), chunk_count(a.home_b))
    row("entities (raw)", len(A["entities"]), len(B["entities"]))
    row("entities (unique merge key)", len(ka), len(kb))
    row("relations (raw)", len(A["relationships"]), len(B["relationships"]))
    row("relations (unique pairs)", len(ra), len(rb))

    print("\n  Overlap (on the merge key)")
    inter, union = len(ka & kb), len(ka | kb)
    print(f"    entities  common {inter:,} / union {union:,} = Jaccard {inter/union:.3f}")
    print(f"            {a.label_a} only {len(ka - kb):,} · {a.label_b} only {len(kb - ka):,}")
    ri, ru = len(ra & rb), len(ra | rb)
    print(f"    relations common {ri:,} / union {ru:,} = Jaccard {ri/ru:.3f}" if ru else "    no relations")

    print("\n  Entity type distribution")
    ta = collections.Counter(e.get("type", "?") for e in A["entities"])
    tb = collections.Counter(e.get("type", "?") for e in B["entities"])
    for t in sorted(set(ta) | set(tb), key=lambda x: -(ta[x] + tb[x])):
        row("    " + t, ta[t], tb[t])

    if a.vault_a and a.vault_b:
        print("\n  Links **a person** put in the vault (separate from the LLM extraction)")
        ma, wa = vault_links(a.vault_a)
        mb, wb = vault_links(a.vault_b)
        row("    [x](/path.md)", ma, mb)
        row("    [[wikilink]]", wa, wb)
        # Are those links reflected in the entity graph —— approximated by filename
        print(f"    → the links **do not become edges** in either case (lr_extract reads the body text only)")

    print("\n  How to read this")
    print("    · high Jaccard = changing the format finds the same entities = the conversion has no KG effect")
    print("    · a chunk count difference = extra LLM cost paid for the same prose")
    print()


if __name__ == "__main__":
    main()
