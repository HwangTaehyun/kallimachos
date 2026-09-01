<!-- snapshot-doc -->
> **This document is a snapshot from before 2026-08-17.** It was written against the corpus of
> that time (1,406 chunks · 2,598 entities) and the figures are not updated — the point is to
> preserve the reasoning as it stood. For the current state see [`ERD.md`](ERD.md) and
> [`../README.md`](../README.md).

# Hybrid retrieval benchmark methodology — the super-brain vault

> Subject: a 97-document personal knowledge vault mixing Korean and English. The evaluation
> protocol and measured results for hybrid keyword (BM25) + semantic (dense) retrieval.
> Written 2026-08-16.

---

## 0. Why a separate methodology is needed

Standard IR evaluation (nDCG / pooling / qrels) measures "which system is better". Hybrid
retrieval adds a further question on top: **how do two components interact inside a single
system.** That question cannot be answered by comparing systems, and it separately demands four
things:

| Requirement | Reason |
|---|---|
| component ablation | Even when fusion wins, you do not know which component earned it. Single-component performance has to be in the same table |
| fusion-method comparison | RRF and convex combination make different assumptions. Measuring only one makes the conclusion contingent on the method |
| query-type breakdown | BM25 is strong on lexical matches, dense on paraphrase. An average erases that contrast |
| significance testing | With few queries (n<100), a 0.02 difference may be noise |

---

## 1. Systems under test

2 components + 12 fusions = **14**. All start from the **same candidate pool** (top 100 chunks
per component) and differ only in the fusion — fixing candidate generation is what isolates the
fusion effect.

| # | System | Definition |
|---|---|---|
| 1 | BM25 alone | LanceDB FTS, `base_tokenizer=ngram(2,3)`, stop-words / stemming / ascii-folding all off. `_score` ≥ 0 |
| 2 | dense alone ★baseline | `intfloat/multilingual-e5-small`, 384d, `query:` / `passage:` prefixes. `_distance` (unit-vector L2²) converted to `cos = 1 − dist/2`, range [−1, 1] |
| 3–5 | RRF k=10/30/60 | `score(d) = Σ 1/(k + rankᵢ(d))`. Ignores scores, uses ranks only |
| 6–10 | CC emp α=0.3/0.5/0.7/0.8/0.9 | **empirical** min-max (sample min/max) then `(1−α)·bm25 + α·dense` |
| 11 | CC zscore α=0.5 | z-score normalization then the same weighted sum |
| 12–14 | **TM2C2** α=0.5/0.7/0.8 | **theoretical** min-max — BM25 floor fixed at **0**, cosine floor at **−1**. The configuration Bruch et al. recommend |

**Missing-value handling** — a document present in only one list is filled with the other side's
**normalized floor** (0 after empirical min-max, 0 for TM2C2, the observed minimum for z-score).
Without stating this, the CC results do not reproduce.

**Chunk → document folding** — every system uses the **maximum** score per document. Using the
sum favours long documents and breaks fairness between systems.

---

## 2. Query set

| Type | n | Judgement | Origin |
|---|---|---|---|
| lookup | 49 | binary (gold) | written by hand, 7 categories (ko-exact / ko-morph / ko-semantic / ko-crosslingual / en-exact / proper-noun / mixed) |
| synthesis | 9 | **graded 0–3** | queries written by hand, answers derived from the link sets of MOC pages inside the vault, then re-graded |

Why the synthesis answers come from MOCs: if the evaluator invents the answers, the evaluator's
own retrieval model leaks into them. Using link sets the vault owner had already curated blocks
that bias.

---

## 3. Building the judgements (qrels) — TREC pooling

```
1. 5 systems × top 10  →  union = the pool
2. existing gold joins the pool too (so answers no system found are not lost)
3. only (query, document) pairs in the pool are judged. 20.9 per query on average, 1,213 total
4. anything outside the pool counts as non-relevant (0)  ← the pooling assumption
5. [R1 correction] judge the unjudged among the top 10 of the 9 systems under test (below)
```

### 3.1 Pooling-bias correction (found in deep-review Round 1)

The 5 systems that built the pool (chunk / global / local / mix / all) **differ from the 9
systems being evaluated.** Documents only the evaluated systems find go unjudged and score 0
automatically → the penalty is asymmetric across systems.

Measured unjudged rate (9 synthesis queries, top 10):

| System | Unjudged rate | Count |
|---|---|---|
| BM25 alone | **17.8%** | 16 |
| dense alone | 12.2% | 11 |
| CC minmax α=0.7 | 8.9% | 8 |
| RRF k=60 | 6.7% | 6 |

BM25 was taking **2.7×** the penalty of RRF. Since conclusion 1 ("fusion > BM25, significant")
might be a measurement artefact, **all 29 unjudged pairs were judged**.

Result: **26 of the 29 really were grade 0**. Only three were non-zero:

| Query | Document | Grade |
|---|---|---|
| second-brain tool review | `wiki_sources_hermes-kallimachos-adr` | 3 |
| local markdown search tool setup | `wiki_entities_secall` | 2 |
| deployment pitfalls | `raw_conversations_hermes-kallimachos-adr` | 1 |

After correction, 246 pairs total (grade 3: 49 / 2: 29 / 1: 50 / 0: 118). **The conclusion did
not change** — Δ against BM25 went +0.110 → +0.108, p(BH) 0.0320 → 0.0342. The §6 table shows
post-correction figures.

> This section is the most important one in this document. That the bias was *found, measured,
> corrected, and the conclusion still survived* is what makes the conclusion trustworthy. Had
> only the pre-correction numbers been reported, this conclusion would be indefensible.

**Grade definitions**

| Grade | Definition |
|---|---|
| 3 | This document alone answers the core of the query |
| 2 | Carries a substantial part of the answer. Without it the answer is incomplete |
| 1 | Peripheral. Provides context but is not the answer |
| 0 | Irrelevant |

### 3.2 What was actually judged and what was not (**corrected** in Round 2)

| | Pairs | Status |
|---|---|---|
| 9 synthesis queries | **264** | graded 0–3, complete |
| 49 lookup queries | **727** | graded 0–3, complete |
| **total** | **991** | 94 documents · grade distribution {0: 384, 1: 241, 2: 254, 3: 112} |

> ⚠ **The previous version of this section said "the 996 lookup pairs are only in the pool and
> were never judged". That is false.**
> `full_qrels.json` was last modified 2026-08-17 05:05 and this document at 17:23 the same day —
> **the judgements existed 12 hours before the document.** The problem was not missing
> judgements; it was that `load_tasks()` in `bench/hybrid_bench.py` (not published — the answer
> key contains real personal names) **ignored** that file for lookup queries and used only 3.1
> binary gold entries per query (loading 416 of 991 pairs).
> [`tune_alpha.py:41`](../src/tune_alpha.py) in the same repository read all 991 from the start —
> correct loading code sat right next to it, and only this side was wrong.
>
> The result was a silent collapse. The binary gold contains **no judged non-relevant (grade 0)
> entries at all**, so condensed scoring — which strips the unjudged — leaves only relevant
> documents → nDCG = 1.0. Measured: **47 of the 49 lookup queries came out at exactly 1.0.**
> The condensed column was recall@100 in disguise, measuring no ordering whatsoever. Lookup is
> 49 of 58 (84%), so the whole table sat under it.
>
> Found in the 2026-08-18 adversarial review. All 991 pairs are loaded now.

---

## 4. Metrics

| Metric | Role | Basis |
|---|---|---|
| **nDCG@10** | primary | Adopted because BEIR (2021) argues it works for both binary and graded. As of 2026 MTEB retrieval still has `main_score` = ndcg_at_10 |
| **nDCG@10 condensed** | **robustness** | Remove unjudged documents from the ranking and re-score. §5.1 |
| R@20 | secondary | k ≪ 97 documents. The first version's R@100 exceeded the corpus and was a degenerate metric |
| MRR | secondary | Distance to the first correct answer |
| unjudged % | **diagnostic** | Share of the top 10 absent from qrels. A direct measure of judgement incompleteness |

`DCG@k = Σᵢ (2^relᵢ − 1) / log₂(i+1)` — exponential gain (the TREC/BEIR convention). Linear gain
narrows the distance between grades 3 and 2 and can change the conclusion, so it has to be
stated.

---

## 5. Significance testing

- **Test**: two-sided Wilcoxon signed-rank (paired per query). For n≤20, an exact test over all
  sign combinations; otherwise a normal approximation with **tie and continuity corrections**.
- **Multiple-comparison correction**: Benjamini-Hochberg FDR, q=0.05.
- **Baseline system**: **`dense alone`, specified in advance.** 13 comparisons.
- **Effect size**: bootstrap 95% CI of the paired mean difference (10,000 resamples) alongside
  Cohen's dz.
- **Effective sample size**: Wilcoxon discards queries with zero difference, so n_eff is reported
  per comparison.

> ### Why the baseline is specified in advance (Round 1 BLOCKER)
>
> The first version **used the highest-nDCG@10 system as the baseline.** That is using the argmax
> of the same data as the reference, and BH corrects for multiple comparisons but **not for
> winner selection.**
>
> Concretely, changing only the baseline from 1st (CC α=0.7) to 2nd (RRF k=30, a gap of 0.0045)
> made 2 of the first version's 4 "significant" results vanish — CC α=0.5's p(BH) went
> **0.0383 → 0.5237**. The first version's significance table was a product of baseline choice.
>
> `dense alone` is the only principled baseline choosable before seeing results — it is the
> strongest **single component**, and "is fusion better than a component" is this benchmark's
> question.

Wilcoxon + BH was chosen because recent IR evaluation work reports that this combination holds
Type I error at the significance level while having the best power. That said, Wilcoxon has been
criticized as unsuitable for hypotheses about *mean* effects (Urbano et al., SIGIR 2019), so
bootstrap CIs are reported alongside.

### 5.1 Handling incomplete judgements — the condensed list

The judgement pool is not complete. Scoring unjudged documents as 0 measures "which system
retrieved many judged documents", so the standard response, the **condensed list** (Sakai), is
reported alongside — unjudged documents are **removed** from the ranking and it is re-scored.
Agreement between the two means the result is robust; divergence means judgement incompleteness
is manufacturing the conclusion.

> ⚠ **Condensed is only meaningful when judged non-relevant documents are in the pool.** If every
> judgement is "relevant", what remains after stripping is entirely relevant and nDCG is 1.0
> regardless of order — it becomes recall, not a metric. The previous version of this document
> carried values produced in exactly that state (see §3.2). With all 991 pairs loaded, 48 of the
> 49 lookup queries have judged non-relevant entries, so condensed works as a metric again.
>
> Unjudged rate: dense 62.6% → **4.8%** · CC α=0.8 61.9% → **0.2%** · BM25 61.6% → **21.2%**

---

## 6. Results (n=58, **re-measured 2026-08-18 after loading all judgements**)

> The previous version's table came from a state loading 416 of 991 pairs (§3.2). Below is a
> re-measurement with everything loaded, and **the conclusions changed in several places.** What
> changed and how is in "How to read it" immediately below.

```
system                  nDCG@10  condensed    R@20     MRR    unjudged%  |  synthesis  lookup
-------------------------------------------------------------------------------------
BM25 alone               0.614      0.694   0.756   0.874    21.2%     0.536  0.628
dense alone              0.657      0.676   0.825   0.939     4.8%     0.677  0.653
RRF k=10                 0.688      0.715   0.843   0.951     8.1%     0.645  0.697
RRF k=30                 0.689      0.716   0.833   0.951     8.6%     0.648  0.697
RRF k=60                 0.690      0.718   0.833   0.951     8.8%     0.654  0.696
CC emp α=0.3             0.660      0.707   0.834   0.886    11.9%     0.591  0.673
CC emp α=0.5             0.689      0.708   0.849   0.945     6.4%     0.643  0.697
CC emp α=0.7             0.688      0.693   0.840   0.956     1.0%     0.665  0.692
CC emp α=0.8             0.690      0.691   0.836   0.956     0.2%     0.686  0.691
CC emp α=0.9             0.676      0.687   0.837   0.945     2.6%     0.685  0.675
CC zscore α=0.5          0.681      0.699   0.846   0.928     6.4%     0.615  0.693
TM2C2 α=0.5              0.654      0.701   0.833   0.899    14.8%     0.586  0.666
TM2C2 α=0.7              0.658      0.702   0.833   0.898    14.3%     0.603  0.668
TM2C2 α=0.8              0.662      0.703   0.833   0.898    13.1%     0.616  0.670
```

**Significance — baseline `dense alone` (pre-specified), Wilcoxon+BH (q=0.05), bootstrap 95% CI alongside**

| Against | Δ nDCG@10 | 95% CI | dz | n_eff | p(BH) | Verdict | condensed p(BH) |
|---|---|---|---|---|---|---|---|
| BM25 alone | -0.043 | [-0.105,+0.020] | -0.17 | 58 | 0.4274 | tie | 0.3331 tie |
| RRF k=10 | +0.032 | [-0.004,+0.068] | 0.23 | 57 | 0.1798 | tie | 0.0665 tie |
| RRF k=30 | +0.032 | [-0.003,+0.068] | 0.23 | 58 | 0.1543 | tie | **0.0402 significant** |
| RRF k=60 | +0.033 | [-0.003,+0.069] | 0.24 | 58 | 0.1543 | tie | **0.0402 significant** |
| CC emp α=0.3 | +0.004 | [-0.044,+0.050] | 0.02 | 55 | 0.9432 | tie | 0.1926 tie |
| CC emp α=0.5 | +0.032 | [+0.000,+0.063] | 0.26 | 52 | 0.1543 | tie | 0.1118 tie |
| **CC emp α=0.7** | **+0.031** | **[+0.009,+0.052]** | 0.36 | 52 | **0.0353** | **significant** | 0.0822 tie |
| **CC emp α=0.8** | **+0.033** | **[+0.019,+0.050]** | 0.55 | 48 | **0.0013** | **significant** | **0.0402 significant** |
| **CC emp α=0.9** | **+0.019** | **[+0.009,+0.031]** | 0.45 | 39 | **0.0060** | **significant** | **0.0402 significant** |
| CC zscore α=0.5 | +0.024 | [-0.010,+0.058] | 0.18 | 52 | 0.2725 | tie | 0.1999 tie |
| TM2C2 α=0.5 | -0.003 | [-0.053,+0.046] | -0.02 | 57 | 0.9432 | tie | 0.1999 tie |
| TM2C2 α=0.7 | +0.001 | [-0.048,+0.049] | 0.00 | 57 | 0.9432 | tie | 0.1926 tie |
| TM2C2 α=0.8 | +0.005 | [-0.042,+0.052] | 0.03 | 57 | 0.8985 | tie | 0.1926 tie |

> ⚠ **The CI column is uncorrected and the p column is BH-corrected.** Reading 13 rows side by
> side makes it easy to read a row whose CI just clears 0 as "nearly significant", but that CI is
> pre-correction. Also, the bootstrap CI is an interval on the **mean** difference while Wilcoxon
> is a **rank** test — they do not measure the same thing to begin with, so disagreement between
> them is not a contradiction but an irrelevance.
> (2026-08-18 review, evalcheck lens)

### How to read it — including conclusions **reversed** from the first version

1. **Item 1 of the previous version is withdrawn.** What it said — "all 13 comparisons tie under
   condensed — this is the single most important result of this benchmark and means the
   judgement set is too incomplete" — was **wrong.** The judgement set was sufficient (991
   pairs), the code read only 416 (§3.2), and in that state condensed was recall rather than a
   metric. With everything loaded, **4 are significant** under condensed: RRF k=30 · k=60
   (p=0.0402), CC α=0.8 (0.0402), CC α=0.9 (0.0402).

2. **Under raw, the significant ones are CC α=0.7 · 0.8 · 0.9** (+0.031 / +0.033 / +0.019 against
   dense, p=0.0353 / 0.0013 / 0.0060). So **this data does support saying CC fusion beats dense
   alone.** The three RRF variants tie under raw but two are significant under condensed, so the
   two metrics disagree. BM25 is −0.043 against dense and a tie (p=0.4274).

3. **TM2C2 is worse than empirical CC** (−0.027 to −0.031, all ties). The theoretical min-max
   normalization the literature recommends brings no gain on this corpus. BM25 score
   distributions vary widely by query (max 20.4, with much smaller queries too), so the fixed
   floor of 0 appears to destroy information instead. **Gap — cause unverified.**

4. **The R@k differences are not an effect of the fusion formula.** The candidate sets of all 13
   fusions were **confirmed identical in code** (`True`). It is the union of the two components'
   top-100, independent of fusion method. The first version's "the certain contribution of fusion
   is candidate generation" was directionally right but misattributed — that is **the effect of
   using two components**, not of the fusion formula.
   (The first version's R@100 was degenerate on a 97-document corpus. Replaced with R@20.)

5. **"α matters more than the fusion family" is also withdrawn — the direction reversed.**
   With all judgements loaded, the difference-of-differences test gives |Δα(0.7−0.3)|=+0.0271 vs
   |Δfamily(CC−RRF)|=−0.0020, **p=0.0548 → indistinguishable**. The previous version's p=0.0164
   is a 416-pair value. The test construction itself (difference-of-differences) is still right —
   the fix to the first version's difference-of-significance error stands. But **this data cannot
   say whether α or the family matters more.**

6. **The optimum α is around 0.8.** The first version's grid {0.3, 0.5, 0.7} had its optimum on
   the boundary, so 0.8 and 0.9 were added. Under raw, 0.8 is best (0.690) and 0.9 drops (0.676).
   But it ties with RRF k=60 (0.690) and, per item 5, the family difference cannot be separated
   from the α difference — so **"80% on the dense side" should be read as the highest point
   observed on this corpus, not as a recommendation.**

7. **No conclusion is drawn for synthesis (n=9).** It is reported in the table but not tested —
   at n=9 there is no power to argue about a 0.05 difference.

---

## 7. Limitations

Reflecting deep-review Rounds 1 and 2. **L2 and L5 now dominate the strength of the conclusions**
(L1 was a code defect and is resolved).

| # | Limitation | Impact | Mitigation |
|---|---|---|---|
| ~~L1~~ | ~~incomplete judgements~~ → **resolved.** All 991 pairs existed; the defect was `hybrid_bench.load_tasks()` loading only 416 for lookup queries (§3.2) | The previous version's "all ties" conclusion was a product of this defect. After full loading: raw 3/13 · condensed 4/13 significant | Fixed 2026-08-18. The remaining limitation is that judgements cover only **94 documents** → L12 |
| **L2** | **One judge (Claude), and the same party as the system's author.** Agreement (κ) cannot be measured | Grading bias possible, particularly across all 246 synthesis pairs | Cross-judgement by the user or a second judge |
| L3 | The 5 systems that built the pool (chunk / global / local / mix / all) **do not overlap the 14 evaluated at all** | Synthesis unjudged rate is **asymmetric**: BM25 17.8% vs RRF 6.7%. Partly mitigated by judging 29 more pairs in §3.1 | Rebuild the pool from the evaluated systems |
| **L4** | n=58 (synthesis n=9). Effective n per test is **raw 39–58 · condensed 33–58** (after full loading; the previous version's "21–47" is a 416-pair figure) | Underpowered. Catching a +0.02 effect against dense at dz≈0.28 with 80% power needs n≈102 non-ties, which given the observed tie rate means **130–170 queries** (currently 58) | Expand the query set |
| **L5** | **A single embedding model, e5-small (2023, 384d).** Bottom tier by 2026 standards | **The conclusion is conditional.** A weak dense component lowers the baseline in the "fusion > dense" comparison. A modern encoder (bge-m3, Qwen3-Embedding) could reverse the result | One sweep with a current model |
| L6 | The tokenizer is fixed at ngram(2,3). Morphological analysers not compared | The ceiling of the BM25 component is unknown | Add lindera/ko-dic |
| L7 | No reranker (cross-encoder) | Differs from a real deployment configuration | Follow-up |
| L8 | End-to-end QA (answer quality) not measured | Good retrieval does not guarantee good answers | LoCoMo / RAGAS-style |
| L9 | Queries written by the corpus owner, who knows the corpus | May differ from the real query distribution | Collect queries from logs |
| L10 | Latency, throughput, and index size not measured | The hybrid-specific benchmark (*Balancing the Blend*, 2025) treats these as mandatory | Follow-up |
| L11 | α swept on the test set | Not held out, so α=0.8 is **optimal for this query set** | Re-verify after splitting queries |
| **L12** | **Judgements cover only 94 documents.** That is nearly all of this benchmark's 97-document corpus, but **75% of the production DB (376 documents) has no judgements** | The figures in this table are not production search quality. Production weights come from a separate experiment ([`tune_alpha.py`](../src/tune_alpha.py)), whose own limitations are in the PRESETS comment in [`kal_search.py`](../src/kal_search.py) | Pool and judge session documents |
| **L13** | **CIs are uncorrected while p-values are BH-corrected** — mixed in one table. And the bootstrap CI is about the mean while Wilcoxon is about ranks, so they do not measure the same thing | Easy to misread an adjacent row as "nearly significant" | Label the columns explicitly (done), or use simultaneous confidence bands |

---

## 8. Reproducing

```bash
cd ~/.kal/bench
.venv/bin/python hybrid_bench.py   # table + significance + interaction + candidate-set verification
# output: ~/.kal/bench/hybrid_results.json (per-query raw data, 14 systems × 58 queries)
# repository copy: ../bench/  (harness, answer key and results are under version control)
```

> The previous version told you to `cd ~/.kal` and use `./.uvenv/bin/python`. Neither exists.
> They are leftovers from when all inputs lived under `/tmp/vault-bench/`, and when macOS cleared
> `/tmp` **this benchmark was unreproducible for a while** (found in the 2026-08-18 review).

Dependencies:

| Resource | Contents |
|---|---|
| LanceDB table `chunks` | **1,577 rows** / 97 unique documents, 500-char chunks, `text_idx` FTS (ngram 2-3) |
| `full_qrels.json` | **991 pairs** graded 0–3 (synthesis 264 · lookup 727) · 94 documents |
| `testset_v2.json` | 49 lookup queries (binary gold is a fallback for queries absent from `full_qrels`) |
| `testset_highlevel.json` | 9 synthesis queries |
| Environment | lancedb 0.37.1 · sentence-transformers 5.7.0 · numpy 2.5.2 · tantivy 0.26.0 |

> §8 of the first version recorded the table size as **811 rows**. 811 was the discarded
> 1000-char chunk configuration. The real figure is 1,577. Also `list_indices()` shows
> `num_unindexed_rows=6` — 6 chunks are invisible to BM25. The impact is negligible but it is on
> the record.

---

## 9. References

**Evaluation metrics and protocol**
- Thakur et al., *BEIR: A Heterogeneous Benchmark for Zero-shot Evaluation of IR Models*, https://arxiv.org/pdf/2104.08663 — the basis for adopting nDCG@10 as primary (§3.3 justifies binary/graded) — published 2021-04, retrieved 2026-08-16
- MTEB / MMTEB, https://github.com/embeddings-benchmark/mteb — includes the 18 BEIR datasets and keeps retrieval `main_score` = `ndcg_at_10`. No successor convention as of 2026 — continuously updated, retrieved 2026-08-16
- *Balancing the Blend: an evaluation protocol for hybrid retrieval*, https://arxiv.org/abs/2508.01405 — 11 datasets × 4 paths × 15 combinations. Keeps nDCG@10 but makes **latency mean/P99, throughput and index size mandatory**. The basis for L10 here — published 2025-08 (v2 2025-11), retrieved 2026-08-16

**Fusion methods**
- Cormack, Clarke & Buettcher, *Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods*, SIGIR 2009, https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf — the definition of RRF, k=60 — published 2009
- **Bruch, Gai & Ingber, *An Analysis of Fusion Functions for Hybrid Retrieval*, ACM TOIS, https://arxiv.org/abs/2210.11934** — CC > RRF and RRF's parameter sensitivity. Recommends **TM2C2** (theoretical min-max: BM25 floor 0, cosine floor −1). The basis for systems 12–14 here — published 2022-10 (v2 2023-05), retrieved 2026-08-16
- *From BM25 to Corrective RAG: Benchmarking Retrieval Strategies*, https://arxiv.org/pdf/2604.01733 — CC α=0.5 Recall@5 0.726 vs RRF k=60 0.695. Prior art for CC beating RRF — published 2026-04, retrieved 2026-08-16

**Statistics**
- Otero, Parapar & Barreiro, *Towards Reliable Testing for Multiple IR System Comparisons*, ECIR 2025, https://link.springer.com/chapter/10.1007/978-3-031-88711-6_27 (preprint https://arxiv.org/abs/2501.03930) — "Wilcoxon plus the Benjamini-Hochberg correction yields Type I error rates according to the significance level … while being the best test in terms of statistical power" — published 2025, retrieved 2026-08-16
- Urbano, Lima & Hanjalic, *Statistical Significance Testing in Information Retrieval: An Empirical Analysis of Type I, Type II and Type III Errors*, SIGIR 2019, https://arxiv.org/abs/1905.11096 — Wilcoxon and sign tests are unsuitable for hypotheses about *mean* effects. The basis for reporting bootstrap CIs alongside — published 2019-05, retrieved 2026-08-16
- Sakai, *On Fuhr's Guideline for IR Evaluation*, SIGIR Forum, http://www.sigir.org/wp-content/uploads/2020/06/p14.pdf — the need for multiple-comparison correction — published 2020, retrieved 2026-08-16
- *On IR metrics designed for evaluation with incomplete relevance assessments*, https://link.springer.com/article/10.1007/s10791-008-9059-7 — limits of the pooling assumption, the condensed-list family of responses — published 2008, retrieved 2026-08-16

**Judgement**
- *Benchmarking LLM-based Relevance Judgment Methods*, https://arxiv.org/pdf/2504.12558 — validity of LLM judges. Background for L2 — published 2025-04, retrieved 2026-08-16

**Embedding models (relevant to L5)**
- Wang et al., *Multilingual E5 Text Embeddings*, https://arxiv.org/abs/2402.05672 — the e5-small family used here — published 2024-02, retrieved 2026-08-16
- *Qwen3 Embedding*, https://arxiv.org/abs/2506.05176 — top-tier open multilingual as of 2026. A candidate for mitigating L5 — published 2026-06, retrieved 2026-08-16

---

## 10. Revision history

| Version | Date | Change |
|---|---|---|
| first | 2026-08-16 | 9 systems · RRF/CC · significance against the winner as baseline |
| **revision 2** | 2026-08-18 | Reflects dev-deep-review Round 2. **1 BLOCKER — a judgement-loading defect.** `hybrid_bench.load_tasks()` ignored `full_qrels.json` (727 pairs) for lookup queries and loaded only 152 binary gold entries (991 → 416). The binary gold contains no judged non-relevant entries, so condensed nDCG came out at **exactly 1.0 for 47 of 49 lookup queries**, and "all 13 tie" in that state was the basis for §6 conclusion 1 and §7 L1. Re-measured after full loading: unjudged rate 62.6%→4.8% (dense), significant raw 0→3 · condensed 0→4. **Conclusions 1 and 5 withdrawn** (the interaction test flipped direction, p=0.0164 → 0.0548). L1 resolved, L12 and L13 added, L4's effective n updated. Corrected §8, which pointed at paths that do not exist (`~/.kal` · `.uvenv`) |
| **revision 1** | 2026-08-16 | Reflects dev-deep-review Round 1. **2 BLOCKERs fixed** — ① baseline moved from the winner's argmax to a pre-specified `dense alone` ② condensed-list evaluation reported alongside. Added 3 TM2C2 variants + α 0.8/0.9 (14 systems). R@100 → R@20. Wilcoxon tie correction. Bootstrap CI, dz and n_eff reported. Conclusion 5's basis replaced with an interaction test. §3.1 pooling-bias correction (29 pairs judged). §8 table size corrected 811 → 1,577 |
