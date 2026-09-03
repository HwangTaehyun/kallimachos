> ⚠ **This is a copy from the CLI era.** The current canonical version is
> [`skills/kal-recall/SKILL.md`](../skills/kal-recall/SKILL.md) (5 MCP tools). This document
> pairs with the older version installed at `~/.claude/skills/kal-search/`, and is kept only as a
> **fallback for environments that cannot use MCP**.
>
> What differs: the MCP version **always** carries sources (`docs`, `refs`) in the response and
> has a time axis (`first_seen`, `timeline`, `as_of`). The CLI version has neither.

---
name: kal-search
description: Search taehyun's personal knowledge DB — the super-brain vault (98 curated notes) plus 278 session documents (376 documents total), indexed in LanceDB with BM25, dense vectors, and an 8k-entity LLM-extracted knowledge graph. Triggers when the user asks about their own notes, past decisions, or prior conversations — "내 노트에서 찾아줘", "전에 뭐라고 정리했지", "우리가 저번에 뭐 결정했더라", "vault 검색", "세션에서 찾아줘", "예전에 이거 어떻게 했지", "what did I write about X", "search my brain" — or when answering needs grounding in the user's own prior work rather than general knowledge.
allowed-tools: Bash Read Grep Glob
metadata:
  author: Taehyun
  version: 2.0.0
  db_path: ~/.kal/db
  src_path: /path/to/kal/src
---

# kal-search — searching the personal knowledge DB

A **weighted fusion of four components**: BM25 · chunk vectors · entity vectors · relation
vectors. Every parameter was set by measurement against a 58-query, 991-pair answer key.

---

## When to use it

| Use | Do not use |
|---|---|
| "how did I organize the inverted index" | "what is an inverted index" (general knowledge) |
| "how did I fix that PM2 problem last time" | "explain how to use PM2" |
| "what did I do about the terraform state file" | questions that need a web search |
| "what was our reasoning for that decision" | codebase exploration (Grep/Glob) |

**The test**: if the answer has to be inside *the user's own past work*, this is the skill.

---

## Running it — this one line is enough

```bash
cd /path/to/kal/src
.venv/bin/python kal_search.py "query" --snippets --json
```

The defaults are the optimum. **Do not change the mode** — see the evidence below.

| Option | Meaning |
|---|---|
| (none) | `default` weights · top 20 · measured optimum |
| `--origin vault` | curated notes only (98 documents) |
| `--origin session` | distilled session documents only (278 documents) |
| `--top N` | adjust the count (default 20) |
| `--snippets` | include supporting fragments |
| `--json` | machine-readable |
| `--mode graph` | only right after re-extracting the KG (see below) |

---

## Why these values — all measured

### Weights

```
   mode       BM25   chunk  entity  rel     control  overall
   default    0.18   0.05   0.18   0.59   0.786   0.804   ← default
   graph      0.20   0.00   0.30   0.50   0.784   0.803
   keyword    1.00   0.00   0.00   0.00     —       —
   vector     0.00   1.00   0.00   0.00     —       —
   legacy     0.20   0.80   0.00   0.00   0.658   0.706   before tuning

   default vs legacy   control Δ+0.128 · overall Δ+0.097
   honest protocol     test-set Δ median +0.117 · significant in 19 of 20 splits · BH 139/146 survive
   held-out            queries split in half · Δ+0.108 on set B → generalization confirmed
```

**Relation vectors dominate** — every top combination gives relations 0.40–0.60.

**Why `graph` is not the default**: dropping chunk vectors entirely collapses when the knowledge
graph is stale. The KG is not refreshed automatically by `sync_v3.py` (it needs LLM extraction).
Measured:

```
                healthy KG   stale KG   drop
   chunk 0.00     0.803       0.706    -0.097   (at 50% stale)
   chunk 0.05     0.804       0.714    -0.090   ← default
   chunk 0.20     0.787       0.724    -0.063

   when healthy   a tie (p=0.967)
   0.20 only wins past 50% stale. A rebuild takes ~10 minutes, so that state is rare.
```

→ When performance ties, take the resilient side. Use `--mode graph` **only right after running
`lr_extract.py`**.

### How many to take

```
    N   Recall@N
    5      0.474
   10      0.788    ← misses 21% of the relevant documents
   20      0.939    ← saturated. the default
   30      0.967    only +0.028
```

### Internal parameters (for reference)

```
   candidate depth   BM25 300 · chunk 300 · entity 30 · relation 40
                     raising entity/relation to 60/80 makes it worse: 0.769 → 0.756
                     (RRF accumulates, so weakly-matched documents push the top down)
   1-hop expansion   1.0   turning it off costs -0.039 (p<0.001). Pulling in documents that
                     mention the entities at both ends of a relation is what graph search
                     actually does
```

### No per-query mode switching

```
   always default   0.776
   auto (rules)     0.759   ← the old way of picking a mode from the query shape
   oracle ceiling   0.792   even knowing the best mode per query
   (these three were measured in the chunk-0.20 era — kept as-is so they compare to each other)

   auto vs best-fixed  Δ-0.017  p=0.0027  → the rules hurt
```

Even the oracle ceiling is only +0.016, so mode switching is not a useful lever on this corpus.
That is why `auto` / `lookup` / `synth` from v1 were removed.

---

## Workflow

### 1 — Search

```bash
.venv/bin/python kal_search.py "query" --snippets --json
```

### 2 — Read the results

```
   📓 vault    notes the user curated by hand.        high confidence
   💬 session  Claude conversations distilled by LLM. decisions and reasoning survive; nuance is lost
```

Session documents are **distillations, not transcripts** (the brain-ingest format). Only
conclusions, decisions, and reasoning remain; utterance transcripts and tool logs are stripped.
`doc_type` tells you the character — `decision` · `investigation` · `analysis` · `design` ·
`plan` · `discussion`.

**Cite the vault first.** Use sessions to reinforce with "this is what was decided at the time".

### 3 — Answer with evidence attached

```
   · cite by path:  raw/conversations/sessions/<slug>.md
   · use abs_path when you need to open the file
   · state the point in time on session citations — the `captured` date in front matter
   · for the raw source, session_id → ~/.claude/projects/**/<id>.jsonl
   · if the search is empty, say so — do not invent
```

### 4 — If that is not enough

```bash
kal_search.py "query" --origin vault --top 20      # curated only
kal_search.py "query" --origin session --top 30    # sessions, wider
Read <abs_path>                                    # the whole document
```

---

## When the results are bad

| Symptom | What to do |
|---|---|
| only irrelevant documents | rephrase in the terms the user would have used, and search again |
| a relevant document is missing | `--top 30` (Recall 0.967) |
| only sessions come back | check with `--origin vault`, then report "it is not in the vault" |
| recent work does not appear | `sync_v3.py`, then re-run `lr_extract.py` |

---

## DB composition

```
   documents      1116    vault 98 + session 278
   chunks       9,329    500 chars · e5-small 384d · FTS ngram(2,3)
   lr_entities  25,372    artifact 7,143 · method 4,175 · concept 3,822 · tool 2030 · failure 3545 …
   lr_relations 35,158   undirected
   ix_*                  raw BM25 inverted index (for debugging; not used by search)
```

```bash
python sync_v3.py --dry-run   # judgement only
python sync_v3.py             # incremental (KG is not refreshed — recorded in stale_docs)
python lr_extract.py          # re-extract the KG (resumable, ~3 hours)
./rebuild_all.sh              # the whole thing
```

---

## Do not

- **Do not invent results.** If the search is empty, say it is empty.
- **Do not treat session documents as fact.** They are LLM distillations and carry the guesses
  and errors of that moment. Cite them with the `captured` date.
- **`why_captured` and `doc_type` are LLM suggestions** — not yet user-approved. The review list
  is `~/.kal/distilled/_distill_review.tsv`.
- **Do not edit vault files through this skill.** Editing happens via `brain-ingest` or directly.
- **Do not change the mode on a whim.** The default is the measured optimum.
