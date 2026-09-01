<!-- snapshot-doc -->
> **This document is a snapshot from before 2026-08-17.** It was written against the corpus of
> that time (1,406 chunks · 2,598 entities) and the figures are not updated — the point is to
> preserve the reasoning as it stood. For the current state see [`ERD.md`](ERD.md) and
> [`../README.md`](../README.md).

# Knowledge DB design — a search store for a personal knowledge vault

> Target: `~/github/HwangTaehyun/super-brain` — a 97-document markdown vault mixing Korean and English
> Store: LanceDB (file-based, zero server processes)
> Schema version: 3 · written 2026-08-17

---

## 0. The problem this DB solves

Make vault documents searchable three ways at once.

| Method | Queries it is good at | Storage form |
|---|---|---|
| **keyword (BM25)** | `"역색인"` · `"pm2 cwd 함정"` — exact terms | inverted index (postings) |
| **semantic (dense)** | `"a data structure that stores words reversed"` — paraphrase | 384-dimensional vectors |
| **relational (graph)** | `"how does US-China competition feed into the scenario"` — causal | entity and relation vectors |

The three results are normalized and fused by a weighted sum (convex combination). Measurements
show the three-way combination is significantly better than any single component
(nDCG@10 +0.052, p=0.0004, dz=0.57).

---

## 1. Full ERD

```
 ┌─────────────────────────────────────────────────────────────────────┐
 │  meta                                                   15 rows     │
 │  key · value    — the DB knows its own configuration                │
 │  embedding_model / embedding_dim / chunk_chars / fts_tokenizer /     │
 │  bm25_k1 / bm25_b / doc_id_scheme / relations_directed / …           │
 └─────────────────────────────────────────────────────────────────────┘

 ┌─────────────────────────────────────────────────────────────────────┐
 │  documents                                              97 rows     │
 │  ─────────────────────────────────────────────────────────────────  │
 │  doc_id        int32    PK = crc32(path) & 0x7FFFFFFF   [BTREE]     │
 │  path          string   "raw/articles/AI 2027.md"       [BTREE]     │
 │  abs_path      string   absolute path — open the file from a result │
 │  title         string   frontmatter title                           │
 │  folder        string   "raw/articles"                  [BITMAP]    │
 │  size · mtime  int64    incremental decision                        │
 │  content_hash  string   sha256[:16] — rename detection              │
 │  indexed_at    int64                                                │
 └───┬────────────────────┬─────────────────────┬──────────────────────┘
     │ doc_id (N:1)       │ doc_ids (N:M)       │ doc_ids (N:M)
     ▼                    ▼                     ▼
 ┌───────────────────────────┐   ┌───────────────────────────────────────┐
 │ chunks          1,406 rows│   │ lr_entities               2,598 rows  │
 │ ───────────────────────── │   │ ───────────────────────────────────── │
 │ chunk_id  int64 PK        │   │ entity_id    int32  PK                │
 │   = doc_id*10000 + seq    │   │ name         string                   │
 │ doc_id    int32  [BTREE]  │   │ name_norm    string        [BTREE]    │
 │ seq       int32           │   │ type         string        [BITMAP]   │
 │ char_start int32          │   │ type_raw     string   LLM original    │
 │   source offset (highlight)│  │ description  string        [FTS]      │
 │ text      string [FTS]    │   │ doc_ids      list<int32>   [LABEL]    │
 │ vector    f32[384]        │   │ chunk_ids    list<int64>   [LABEL]    │
 └───┬───────────────────────┘   │ degree       int32                    │
     │ chunk_id                  │ vector       f32[384]                 │
     ▼                           └────────┬──────────────┬───────────────┘
 ┌───────────────────────────┐            │ src_id       │ tgt_id
 │ ix_postings  843,287 rows │            ▼              ▼
 │ ───────────────────────── │   ┌───────────────────────────────────────┐
 │ term_id   int32  [BTREE]  │   │ lr_relations              4,440 rows  │
 │ chunk_id  int64  [BTREE]  │   │ ───────────────────────────────────── │
 │ tf        int32           │   │ rel_id       int32  PK                │
 │ positions list<int32>     │   │ src_id·tgt_id int32 FK→entities[BTREE]│
 │ pos_truncated bool        │   │ src_name·tgt_name string  convenience │
 └───┬───────────────────────┘   │ directed     bool   always False      │
     │ term_id                   │ keywords     list<str>     [LABEL]    │
     ▼                           │ description  string        [FTS]      │
 ┌───────────────────────────┐   │ weight       float32                  │
 │ ix_terms      73,620 rows │   │ doc_ids      list<int32>   [LABEL]    │
 │ term_id int32 PK          │   │ chunk_ids    list<int64>              │
 │ term    string  [BTREE]   │   │ vector       f32[384]                 │
 │ df      int32   doc count │   └───────────────────────────────────────┘
 │ idf     float32           │
 └───────────────────────────┘   ┌───────────────────────────────────────┐
 ┌───────────────────────────┐   │ stale_docs                            │
 │ ix_doclen     1,406 rows  │   │ doc_id · path · reason · marked_at     │
 │ chunk_id [BTREE]          │   │ queue of documents needing LLM re-extraction │
 │ num_tokens  length denom  │   └───────────────────────────────────────┘
 └───────────────────────────┘

   38.1 MB on disk · 0 server processes · full build 44s · incremental 7s
```

### Cardinality — the two arrows differ

```
   documents ──doc_id──▶ chunks          N:1   a single integer
      A chunk belongs to exactly one document. 14.5 chunks per document on average

   documents ◀─doc_ids─▶ lr_entities     N:M   a list of integers
      An entity spans documents. e.g. 'OpenAI' → 16 documents
```

Why the N:M is an **array column + LabelList index** rather than a normalized join table:
LanceDB is columnar and weak at joins, and an array is read in one go.
`array_has(doc_ids, 31)` filters, so the reverse lookup works without a join too.

---

## 2. Key design

| Key | Rule | Why |
|---|---|---|
| `doc_id` | `crc32(path) & 0x7FFFFFFF` | **The same path always gives the same value.** Existing ids are unchanged when files are added or removed |
| `chunk_id` | `doc_id * 10000 + seq` | Dependent on the document. Chunk ids do not change while the document does not |
| `term_id` | dictionary sort order | `ix_*` is rebuilt in full, so stability is not needed |
| `entity_id` | order after merging | LLM re-extraction refreshes everything anyway |

> ### Why not sequential — the v2 failure
>
> v2 used `doc_id = len(rows)`, an alphabetical sequence number. Adding one
> `raw/articles/AAA.md` **shifted the doc_id of 95 documents**, and that invalidated
> `chunks.doc_id` 1,406 rows + `entities.doc_ids` 2,624 rows + `relations.doc_ids` 4,458 rows =
> **8,488 rows at once.**
> And it fails silently — `'Daniel Kokotajlo'`'s `doc_ids=[2,…]` starts pointing at `AAA.md`
> after a rebuild, with no error.
>
> After switching to `crc32(path)`, the same operation changed **zero** document ids.
> Hash collisions are checked at build time and fail immediately (with 97 documents in a 2³¹
> space, effectively zero).

---

## 3. Indexes

```
   documents      path[BTREE]  folder[BITMAP]  doc_id[BTREE]
   chunks         doc_id[BTREE]  chunk_id[BTREE]  text[FTS ngram(2,3)]
   ix_terms       term[BTREE]
   ix_postings    term_id[BTREE]  chunk_id[BTREE]
   ix_doclen      chunk_id[BTREE]
   lr_entities    type[BITMAP]  name_norm[BTREE]
                  doc_ids[LABEL_LIST]  chunk_ids[LABEL_LIST]
                  description[FTS ngram(2,3)]
   lr_relations   src_id·tgt_id[BTREE]  doc_ids·keywords[LABEL_LIST]
                  description[FTS ngram(2,3)]
```

**No vector index is built.** At a few thousand vectors brute force is already sub-millisecond,
and on 811 vectors measured there was zero accuracy difference against HNSW. All you gain is
approximation error. `IvfFlat(cosine)` / `HnswSq` become worth considering from hundreds of
thousands of vectors.

### Tokenizer — why ngram(2,3)

```
   "역색인을"  →  역색 · 색인 · 인을 · 역색인 · 색인을
   "역색인"    →  역색 · 색인 · 역색인
                  └─ 3 overlap → match succeeds
```

With whitespace splitting (`simple`), `"역색인을"` and `"역색인"` are different tokens and it
fails. This solves the Korean particle problem without a dictionary.

| Tokenizer | overall R@5 | ko-morph R@5 | Note |
|---|---|---|---|
| simple (whitespace) | 0.706 | 0.567 | |
| icu | 0.691 | 0.527 | |
| **ngram(2,3)** | **0.754** | **0.763** | adopted |
| lindera/ko-dic (morphological) | 0.722 | 0.763 | ko-crosslingual drops to 0.646 |

Why the morphological analyser lost: ko-dic is a general Korean dictionary, so loanwords and
technical terms like `제텔카스텐` and `토크나이저` are absent and get decomposed wrongly. ngram
has no dictionary, so it does not have that problem at all.

**The cost**: the index is larger than the source (data 2.8MB vs `_indices` 3.9MB), because a
four-character word becomes five tokens.

---

## 4. Why there are two BM25 inverted indexes

```
   ① the [FTS] on chunks.text     LanceDB built-in. Used by the real search. 3.5ms
                                  compressed binary (FST + delta/varint postings)
                                  ❌ cannot be inspected with SQL

   ② ix_terms / ix_postings / ix_doclen    built by hand. For debugging and tuning
                                  uncompressed ordinary tables. 10.0MB (26% of the total)
                                  ✅ queryable with SQL
```

Things only ② makes possible:

1. **Explaining "why this score"** — `역색인` has df=21, idf=4.296, and which chunk has tf=4
2. **k1 · b experiments** — the built-in is fixed. ② allows verifying things like `k1=0` (TF off)
3. **Inspecting the IDF distribution** — 32% of terms have DF=1, max DF=1,413 → confirming stop
   words die out automatically
4. **Position-based phrase search** — up to 48 positions stored in full

> **Kept deliberately.** It is not on the search path so it costs no performance, and being able
> to explain "why this result" is one of this DB's design goals.

### What ② verified — decomposing BM25

A commonly repeated claim about Korean RAG ("with short, uniform chunks, TF is neutralized and
BM25 degenerates into an IDF lookup") was tested directly.

```
   variant                  nDCG@10    R@20     MRR    vs full
   ──────────────────────────────────────────────────────────
   full  k1=1.2 b=0.75       0.613   0.758   0.871    +0.000
   no-len  b=0               0.611   0.760   0.880    −0.003  ← length norm pointless ✓
   binary TF≡1               0.582   0.738   0.829    −0.031
   no-tf   k1=0 (IDF only)   0.566   0.735   0.838    −0.047  ← TF is alive ✗
```

- **Length normalization is pointless** — chunk length has a coefficient of variation of 0.177
  and a 10th–90th percentile ratio of 1.1×. The claim holds.
- **TF is not neutralized** — turning it off costs −0.047. 72.3% have TF=1, but 27.7% have TF≥2.
  ngram splits a word into several pieces, which leaves TF with discriminating power. The claim
  does not hold.

---

## 5. Knowledge graph (lr_*)

An LLM reads a chunk and **generates** entities and relations. Same structure as the LightRAG
pipeline.

```
   97 documents
     │  1200-token (2400-char) chunking · overlap 200        → 316 chunks
     ▼
   316 LLM calls (Haiku, 33 min)
     │  JSON schema:
     │    entity      {name, type, description}
     │    relationship{source, target, keywords, description}
     ▼
   merge — identical names combine descriptions and accumulate sources
     ▼
   2,598 entities · 4,440 relations
```

### Normalization

**Types** — the LLM invents types that were never declared. `TYPE_MAP` folds them, and anything
still unmatched becomes `other`. The original is preserved in `type_raw`.

> **What follows is a record from around 2026-06.** The prompt declared 7 types then and there
> were about 2,600 entities. There are now **10** types and 7,620 entities — and why it went from
> 7 to 10 matters more than this document. For the current list and its reasoning see
> [`PIPELINE.md`](./PIPELINE.md) §extraction; for the schema, [`ERD.md`](./ERD.md).
> This is kept to show the shape the folding rules had.

```
   system·application·platform·framework·infrastructure·hardware → tool
   activity → event    process·practice·plan → method
   goal·feature·capability·domain·context·topic·principle·reference·format·artifact → concept
   group → organization
   everything else → other

   result at the time:  concept 1,082 · tool 431 · method 409 · pattern 406
                        organization 106 · event 83 · person 79 · other 2
```

With only 2 landing in `other`, the mapping covers nearly everything. Without this normalization
the `type[BITMAP]` filter is meaningless (you cannot decide "what is the difference between
system and tool?").

**Names** — whitespace folding plus removal of parenthetical annotations
(`"qmd (Tobi Lütke)"` → `"qmd"`). This merged 2,624 → 2,598 entities and 4,458 → 4,440 relations.

### Relations are undirected

At extraction the merge key is `tuple(sorted([src, tgt]))`, so there is no direction information.
Yet the table stores `src_id`/`tgt_id`, which makes it look directed.

→ The `directed` column is **explicitly `False`**, and meta records
`relations_directed = "false (canonical sorted src/tgt)"`.
This is to stop traversal code from trusting a direction that is not there.

### chunk_ids — where the evidence is

`entities.chunk_ids` / `relations.chunk_ids` point at "which chunk this entity came from". That
lets an answer cite the **exact position** rather than just the document.

```
   'Daniel Kokotajlo' → chunk_ids [3934398190010, 8645318970000, …]
     chunk …0010  raw/articles/AI 2027.md  seq=10
     chunk …0000  wiki/entities/daniel-kokotajlo.md  seq=0

   fill rate:  entities 1,786/2,598 (69%) · relations 4,100/4,440 (92%)
```

> **Assumption** — the chunk attribution from the original extraction was lost during merging and
> was **reconstructed after the fact by matching body text**. Extraction used 2400-char chunks and
> storage uses 500-char chunks, so a remapping was needed regardless; that makes it more precise,
> but there is no guarantee it matches the original. The 31% left unfilled are cases where the
> LLM paraphrased an entity name that does not appear literally in the text.

---

## 6. Incremental sync

### Decision — `diff_vault()`

```
   added      a new doc_id
   modified   same doc_id, different content_hash
   deleted    in the DB, no file
   renamed    the same content_hash exists on both the deleted and added side
              → removed from added/deleted and classified as renamed
   unchanged  identical content_hash
```

### Application — `sync_v3.py`

| Table | Handling |
|---|---|
| `documents` | upsert added/modified · delete removed · rename = delete + insert |
| `chunks` | delete only that `doc_id`'s chunks → re-chunk → re-embed → insert |
| `ix_*` | **full rebuild** — `df`/`idf` are global statistics, so partial updates are inaccurate. 5–7 seconds at 1,400 chunks |
| `lr_*` | drop dead `doc_id`/`chunk_id` references + record in `stale_docs` |

**Measured**

```
   ▶ one document added
     documents  0 deleted · 1 upsert                    0.0s
     chunks     2 rows regenerated                      0.4s
     ix_*       rebuilt: terms 73,630 postings 843,361  6.5s
     lr_*       0 ghost references · 1 marked stale     0.3s
     total 7.2s                                    (a full build is 44s)
```

### The knowledge graph cannot be refreshed automatically — an explicit limit

When a document changes, its entities and relations have to be re-extracted, and that is an
**LLM call**. Automating it would incur unintended cost, so it is not automated. Instead:

```
   recorded in the stale_docs table
     doc_id · path · reason(added|modified|renamed) · marked_at

   → run refresh_kg.py (lr_extract alone only refreshes lr_kg.json — corrected 2026-08-18)
```

### Integrity — all 9 checks pass after an incremental apply

```
   ✅ chunks.doc_id → documents orphans          0
   ✅ ix_doclen ↔ chunks mismatches               0
   ✅ ix_postings.chunk_id orphans                0
   ✅ entities.doc_ids / chunk_ids dead refs      0 / 0
   ✅ relations.doc_ids / chunk_ids dead refs     0 / 0
   ✅ duplicate chunk_id · duplicate doc_id       0 / 0
```

---

## 7. The search path

### Three components

```
   BM25            FTS(ngram 2,3) on chunks.text → _score
   dense chunk     cosine on chunks.vector → cos = 1 − _distance/2
   relation vector cosine on lr_relations.vector → relation.doc_ids
   (entity vector) lr_entities.vector → entity.doc_ids + 1-hop
```

### Fusion — convex combination

```python
def cc(parts, weights):
    norm = [minmax(p) for p in parts]           # each component to 0..1
    keys = set().union(*norm)
    return {k: sum(w * n.get(k, 0.0) for n, w in zip(norm, weights)) for k in keys}
```

Why normalization is mandatory: BM25 runs 12–42, cosine 0.83–0.86. Adding them raw makes BM25 49×
larger, crushing the vectors. The weights become meaningless.

> LanceDB's built-in `LinearCombinationReranker` **does not normalize**
> (`weight*vector_score + (1-weight)*fts_score`, raw scores). Its `weight` parameter therefore
> does not behave as intended — beware. The built-in `RRFReranker` uses ranks only, so it does not
> have this problem.

### Measured results (58 queries · 991 fully-judged pairs · nDCG@10)

```
   system                   nDCG@10    R@20     MRR  |  synthesis  lookup
   ────────────────────────────────────────────────────────────────
   BM25+dense (CC .8)       0.696   0.836   0.971    0.686   0.698
   relation vector alone    0.746   0.929   0.927    0.729   0.750
   entity+relation          0.749   0.927   0.964    0.708   0.756
   all four fused           0.748   0.889   0.971    0.703   0.756
                                                     p=0.0004 · dz=0.57
```

**Which metric to look at depends on the use:**

```
   "open one document for me"        → MRR.  chunks and entities are strong (0.971–0.975)
   "gather everything related"       → R@20. relations are strong (0.929 vs 0.836)
   "throw the top 20 at an LLM"      → R@20 is decisive. What retrieval misses, the LLM never sees
```

---

## 8. Limitations — on the record

| # | Limitation | Impact |
|---|---|---|
| L1 | **The judge is the system's author.** All 991 answer-key pairs were graded by the author. κ not measured | The credibility of every result hangs here |
| L2 | 593 pairs of the answer key came from a **rule-based first pass** (per-query match terms, then graded by frequency and heading) | The choice of match terms can manufacture the result |
| L3 | A single embedding model, e5-small. MIRACL-ko 61.2 (bge-multilingual-gemma2 74.1) | A stronger model could change the outcome |
| L4 | `chunk_ids` reconstructed after the fact (69% / 92%) | May differ from the original attribution |
| L5 | Incremental KG refresh goes as far as marking stale. Actual re-extraction is manual | The KG goes stale when documents change |
| L6 | `ix_*` occupies 26% of disk but is unused by search | Intended (for debugging) |
| L7 | End-to-end QA (answer quality) not measured | Good retrieval does not guarantee good answers |
| L8 | n=58. Effective n is 21–58 depending on the test | Differences around +0.02 cannot be settled |

---

## 9. Files

```
   schema_v3.py     full build (44s).  schema definition · index configuration · normalization rules
   sync_v3.py       incremental apply (7s).   decide → partial update → maintain integrity
   lr_extract.py    LLM entity/relation extraction (33 min, claude CLI).  re-runs only on stale_docs
   manual_index.py  builds ix_* directly + ManualBM25 (k1 · b adjustable)
   bm25_ablation.py BM25 component decomposition
   lr_bench.py      compares 6 configurations + Wilcoxon+BH significance
```

### Reproducing

```bash
cd ~/.kal
.venv/bin/python schema_v3.py     # first full build
.venv/bin/python sync_v3.py       # incremental afterwards (exits immediately if nothing changed)
.venv/bin/python sync_v3.py --dry-run   # decision only
```

Environment: lancedb 0.37.1 · sentence-transformers 5.7.0 · pyarrow · numpy 2.5.2

---

## 10. References

- Thakur et al., *BEIR: A Heterogeneous Benchmark for Zero-shot Evaluation of IR Models*, https://arxiv.org/pdf/2104.08663 — the basis for nDCG@10 as the primary metric — published 2021-04, retrieved 2026-08-17
- Cormack, Clarke & Buettcher, *Reciprocal Rank Fusion*, SIGIR 2009, https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf — published 2009
- Bruch, Gai & Ingber, *An Analysis of Fusion Functions for Hybrid Retrieval*, ACM TOIS, https://arxiv.org/abs/2210.11934 — CC > RRF, recommends TM2C2 — published 2022-10 (v2 2023-05), retrieved 2026-08-17
- Otero, Parapar & Barreiro, *Towards Reliable Testing for Multiple IR System Comparisons*, ECIR 2025, https://arxiv.org/abs/2501.03930 — Wilcoxon+BH — published 2025, retrieved 2026-08-17
- HKUDS, *LightRAG*, https://github.com/HKUDS/LightRAG — entity/relation extraction schema and the local/global paths — retrieved 2026-08-17
- Microsoft, *GraphRAG*, https://github.com/microsoft/graphrag — read at commit 60668ba. Confirmed parquet storage + LanceDB vector store — retrieved 2026-08-17
- moonzoo, *\[RAG\] Sparse Vector 방식 비교 (BM25 VS BM25+형태소 VS SPLADE VS BM42)* — the claim tested in §4 — published 2025-11-04, retrieved 2026-08-17
