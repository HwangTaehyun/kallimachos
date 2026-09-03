# Time-axis design — letting an LLM see how an entity changed over time

Written 2026-08-20 · **second revision** (dev-deep-review round 1 applied — 13 blockers)
Every basis is a measurement from this repository.  Estimates are marked `assumption`, unmeasured things `Gap`.

> **Most of the first edition's numbers were wrong.**  `lr_cache.jsonl` is append-only, and
> counting without removing `(doc,idx)` duplicates inflated the fragment count by **about
> 1.8×** (907 unique of 1,720 lines, 813 superseded).  Everything below excludes the
> superseded ones.  §6 keeps the correction history.

---

## 0. What the problem is (measured)

`group_nodes()` ([`src/lr_extract.py:439`](../src/lr_extract.py)) collects fragments like this
([`:336-343`](../src/lr_extract.py)):

```python
ents[k] = {..., "descriptions": [], "docs": set()}
ents[k]["descriptions"].append(d)      # a description fragment — a list
ents[k]["docs"].add(r["doc"])          # the source documents — a set
```

**The two are not paired.**  So `summarize_one(name, descs)` ([`:464`](../src/lr_extract.py))
receives a list of strings and flattens it with no time attached.  Relations (`rels`) have the same shape ([`:359-368`](../src/lr_extract.py)).

Not for want of data —— **it exists and is thrown away:**

| What exists | Where |
|---|---|
| chunk → document | `doc` in `lr_cache.jsonl` (⚠ only the latest `(doc,idx)` may be used) |
| document → date | frontmatter — **not in the DB.**  See §2.1 |

Document→date coverage, measured:

```
generated_at · captured · created  →  374/378 (98.9%)
+ updated                          →  376/378 (99.5%)
range 2026-04-09 ~ 2026-08-19 (132 days)
```

`updated` is "when a person edited the document", not when it was created.  **Use the three
keys by default and `updated` only as a last resort** (recorded in `date_src`, §2.1).

### The scale of the damage (measured, superseded excluded)

Entities with 10 or more fragments across 5 or more distinct dates: **44**.

Today those fragments blend into one description with no order.  The LLM has **no way to see**
"in April it was like this, in August like that".

> ⚠ **`Obsidian`, the first edition's evidence, was a bad example.**
> The first edition wrote this:
> ```
> 2026-04-09  "primary platform for Super Brain implementation"
> 2026-08-19  "the tool used for knowledge management and documentation"
> ```
> But **Obsidian itself did not change.**  Only the level of detail and the language differ.
> That is *description scatter*, not *entity change*, and mixing the two fills the timeline with noise.
>
> Confirmed by measurement after implementation —— the same prompt produced **0 changes** for
> `Obsidian`, and produced them for things that did change:
> ```
> MRR       2026-08-15  chosen as the primary metric, then judged unsuitable
> Traefik   2026-07-30  oauth2-proxy auth delegation · hot reload without downtime added
> nDCG@10   0 · Obsidian 0   ← what did not change produces nothing
> ```
> **Discrimination is the real basis for this design.**  Not "the descriptions look different".

---

## 1. The time model

The standard is to attach time to a fact, but **the methods diverge:**

| Method | Example | Character |
|---|---|---|
| interval | `(e, r, e, t_start, t_end)` | folds the same fact into one |
| point quadruple | `(v₁, v₂, e, τ)` + parallel edges | a separate edge per point in time |

> ⚠ **The first edition cited TG-RAG wrongly, as a basis for the interval model.**
> [RAG Meets Temporal Graphs (TG-RAG)](https://arxiv.org/abs/2510.13590) — published 2025-10-15,
> retrieved 2026-08-20 — uses **a single timestamp quadruple + parallel temporal edges**.
> It is not a basis for the interval model but **a counter-example**.  (Calling it "T-HiGra"
> was an error too — T-HiGra is [a separate Springer chapter](https://link.springer.com/chapter/10.1007/978-3-032-32643-0_2).)

**bi-temporal requires four times** ([Zep/Graphiti](https://arxiv.org/abs/2501.13956) —
published 2025-01-20, retrieved 2026-08-20):

```
valid_at / invalid_at        the span it was true in the world
created_at / expired_at      when the system learned it / retired it
```

**This design is not bi-temporal.**  `first_seen`/`last_seen` alone cannot express "this fact
is **no longer true**" —— `last_seen` is "last **mentioned**", not "ceased".  That reduction is
deliberate:

> **Decision** — stage 1 goes **uni-temporal (valid time only, no invalidation).**
> Adding invalidation means judging "this fact has ended", and there is as yet no way to
> measure the reliability of that judgement (§5).  `invalid_at` can be added later if needed.

> **Assumption** — the document date stands in for valid time.  It is broadly right for session
> records and wrong for retrospectives.  **That error has not been measured** —— and it is the
> ceiling on the validity of §4's point-in-time gold set (§4.5, priority 0).

---

## 2. The data model

### 2.1 Start at the storage layer — there is nowhere to put a date today

**The `documents` schema has zero date columns** ([`src/schema_v3.py:308-321`](../src/schema_v3.py)).
`mtime` exists but differs from the frontmatter date in **355/374 (94.9%)** (median 6 days, max
132 —— `wiki/overview.md` is fm 2026-04-09 vs mtime 2026-08-19).  It is not a substitute.

```
documents  + doc_date   string   "2026-06-25"
           + date_src   string   generated_at | captured | created | updated | none
           + no_llm     bool     for the §3.6 transmission gate (avoids re-parsing on every query)
```

`refs` is **not** put into `documents` —— see §3.3 (it parses the body, which is a different matter).

> ⚠ **Adding `sources` to `FM_KEEP` is a security decision.**
> [`schema_v3.py:67-71`](../src/schema_v3.py) nails it down: this list is both "what gets
> indexed locally" and **"what gets sent off the machine by `claude -p`"**.  `sources` holds
> only internal slugs, so sending it is judged harmless —— **but that judgement is recorded here explicitly.**

### 2.2 Attach a source to each fragment

```python
# before:  "descriptions": [],          "docs": set()      ← no pairing
# after:   "frags": [(doc, desc)],      "docs": set()      ← the pairing is kept
```

**The dedup key stays `desc`** (making it a tuple would count the same description in two
documents as 2, widening the group that passes `FORCE_LLM_SUMMARY_ON_MERGE`).
Measured: both ways give **305** (259 entities + 46 relations) —— no difference in this corpus,
but a change would invalidate the cost estimate, so the key is pinned.

Lines affected: `260 · 276 · 283 · 286 · 353-357 · 492` (`v.pop("descriptions")`).
**Applied to relations as well.**

### 2.3 Merging happens **twice**

After `lr_extract.group_nodes()`, [`entity_resolve.py:197 build_canon()`](../src/entity_resolve.py)
merges again —— called from [`schema_v3.py:943`](../src/schema_v3.py) (inside `build_graph()`),
and folded by `merged[k]["docs"] |= set(e["docs"])` at [`schema_v3.py:943`](../src/schema_v3.py).

Timeline combination rules in the second merge:

| | Rule |
|---|---|
| combine | concat in date order |
| duplicates | drop identical `(at, change)` |
| `first_seen`/`last_seen` | min / max |
| `doc` fails to resolve | leave that item's `doc` as `null` and **do not discard it** (`resolve()` discards silently) |

### 2.4 Schema additions (`lr_entities`)

```
+ first_seen   string   "2026-04-09"
+ last_seen    string   "2026-08-19"
+ timeline     string   JSON: [{at, change, doc}] — **only what actually changed**
```

Name correspondence:

| LLM output | Schema | MCP response |
|---|---|---|
| `profile` | `description` | `description` |
| `changes[]` | `timeline` (JSON-serialised) | `timeline[]` |
| — | `timeline[].doc` | resolved to `docs[].doc_id` |

**Update obligations**: add 3 rows to the `lr_entities` table in [`docs/ERD.md`](./ERD.md) ·
update the summary step in [`docs/PIPELINE.md`](./PIPELINE.md).

### 2.5 The summary prompt — the cache must be broken

The fragments are given **in date order** and `profile` + `changes` come back.  The point is
**"only what changed"** —— most of 123 fragments repeat the same thing, and including them all buries the signal.

> ⚠ **`_sum_key` does not look at the prompt.**
> [`lr_extract.py:606-616`](../src/lr_extract.py): `sha1(name + sorted(descs))`.
> Change the prompt and the key stays the same, so **everything hits the cache and `changes` never appears.**
> → put `PROMPT_VERSION` in the key and change the cached value to `{profile, changes}`.
> The existing `lr_summary_cache.jsonl` is invalidated.

**On failure**: a parse failure fills `profile` only and leaves `timeline=[]`.
**But it must not fall through quietly.**  That really happened during implementation ——

> `KAL_CLAUDE_RELAY` still pointed at `host.docker.internal` (for the container) while running
> on the host, so DNS did not resolve and **every** LLM call failed.  But `call_text` swallowed
> it with `except Exception: pass` and `summarize_one` fell back to concatenating fragments, so
> the log showed only **"305/305 done · 0 failures · 530s"**.  The output was rubbish and still
> looked like a summary to a human reader.  305 rubbish entries went straight into the cache.
>
> → `CALL_STATS` counts failures now and **dies** at a failure rate of 10% or more.  Better
> than surviving and serving rubbish.  A healthy run took 4,913s —— 530s was itself the signal.
**`first_seen`/`last_seen` are computed deterministically from the dates, outside the LLM
path** —— so an `as_of` lower bound survives an LLM failure.

### 2.6 Measured output (2026-08-20)

```
7,710 entities · first_seen filled 7,649 (99.2%)
entities with a timeline    65 · 112 changes      ← 0.85% of the whole
relations with a timeline   11
```

**That low 0.85% is the success metric.**  The point is to pick out "only what changed", so a
high number would mean description scatter had been mistaken for change.

```
Claude Code  05-14 raised to the main operating runtime → 06-23 the qmd memory architecture → 07-19 folded into Orca
Dokploy      07-11 VPS support beyond AWS → 07-12 API key management → 07-14 the Domains tab
taehyun      04-29 research begins → 06-27 ADR-001 owner → 07-12 Level 5 Program Lead
```

> ⚠ As the last line shows, **a person entity's timeline carries personnel information.**
> §3.6's transmission boundary applies to this data unchanged.

### 2.7 Cost

What gets re-summarised is entities with 8 or more fragments —— measured, **259 / 7,710 = 3.4%**
(at the merge stage.  The 8,179 in the `lr_entities` table is the figure **after** `build_canon`,
a different population.  The first edition mixed the two).

The **number** of calls does not rise, but **invalidating the cache makes the first run all 259 calls.**
Later runs add none.

---

## 3. The MCP API design

### 3.1 Design criteria

1. Few tools, each with a distinct role.
2. **Provenance on every response** —— what cannot be cited and checked cannot be trusted.  (a user requirement)
3. No tool that merely wraps another.
4. Point-in-time arguments are optional.  Omitted means now.
5. **stdio only.  No network listener is opened.**  This repository has no authentication and
   loopback binding is the only defence ([`docs/STACK.md`](./STACK.md) §7).
6. Follows the **[MCP 2026-07-28 revision](https://modelcontextprotocol.io/specification/2026-07-28/changelog)**
   (published 2026-07-28, retrieved 2026-08-20) —— an `outputSchema` per tool, results in
   `structuredContent`, `ttlMs`+`cacheScope` on `tools/list`.
   Prose conventions are the pre-2025-06 way.

> "Few tools" in §3.1 is **a design opinion, not a spec requirement.**  The MCP spec makes no
> recommendation about the number of tools.

### 3.2 Tokens are the real cost

Measured (the current DB):

```
kal_entity("obsidian")     66 docs[]        →  about 4.2k tokens
kal_neighbors("obsidian")  143 relations    → about 27k tokens   ← one response eats the context
```

> ⚠ The first edition said `timeline` was split out of `entity` to "save tokens".
> **Wrong** —— while it saves tens of tokens, `docs[]` spends thousands.
> The real reason for the split is **that the arguments differ** (`since`/`until`).

So:

```
docs[]     10 by default · newest first · + docs_total, docs_truncated
neighbors  limit 20 by default · doc_ids only, not docs[]
```

### 3.3 The provenance response fragment

```jsonc
"docs": [ { "doc_id": 123, "path": "raw/conversations/x.md",
            "title": "…", "date": "2026-06-25",
            "date_src": "captured", "origin": "session" } ],
"docs_total": 66, "docs_truncated": true,

"refs": [ { "id": "s1", "slug": "agent-termination-patterns",
            "doc_id": 456, "title": "…", "url": null } ],
"refs_status": "none" | "unresolved" | "ok"
```

> ⚠ **The first edition's `refs` would be permanently empty if implemented.**
> The frontmatter `sources:` holds **internal slugs** shaped `["s1:<slug>"]`, with no URL.
> Measured: of the 59 wiki documents carrying `sources:`, **0 have a URL in the frontmatter**; 30 have one in the body.
> There are only 16 `wiki/sources/` notes (10 of which hold an http URL).
>
> → `refs` is **a two-hop join**: the `sources:` slug → `wiki/sources/<slug>.md` → extract the
> URL from **that note's body**.  It is empty in most responses, so `refs_status` separates
> "no source" from "failed to resolve".

> 🔒 **`refs[].url` is unverified user content.**  A model that follows it automatically turns
> it into a prompt-injection path.  The response carries that warning with it.

### 3.4 The 5 tools

| Tool | When | Arguments |
|---|---|---|
| `kal_search` | natural-language exploration.  The main entry point | `query`, `top=20`, `origin?` |
| `kal_entity` | what a known name is + its sources + a change summary | `name`, `as_of?` |
| `kal_timeline` | every change | `name`, `since?`, `until?` |
| `kal_neighbors` | one hop in the graph | `name`, `min_degree?`, `limit=20` |
| `kal_doc` | citation checking — the source text | **`doc_id` only** (§3.6) |

**`as_of` was removed from `kal_search`** —— it was ambiguous between a document-date filter and
an entity-state filter (§4.5's example queries split across 3 tools), and state belongs to `entity`/`timeline`.

**The point-in-time argument rule — put this verbatim in each tool's description:**

```
as_of         one **state** at that point           (kal_entity)
since/until   the **list of changes** in that span  (kal_timeline)
```

### 3.5 Name resolution — failure is the default

Measured: `name_norm` is **only a lowercasing, not the `merge_key`** — of 7,620 rows,
**5,999 (79%) differ.**  `claudecode`, the first edition's example, is **not in the DB**
(it is `Claude Code`).  `KAL`, `claude-code` and `Taehyun` all miss too.

```jsonc
// when the exact match fails — candidates, not an error
{ "matched": null,
  "candidates": [ {"name":"Claude Code","type":"tool","degree":41,"doc_count":38} ] }  // ≤10
```

`name` is matched **case-insensitively** against `name_norm` plus the declared aliases
(4 in `aliases.yml`, 25 in vault frontmatter `aliases:`).

### 3.6 The transmission boundary — MCP is the **second** one

This repository already draws the distinction ([`lr_extract.py:289-297`](../src/lr_extract.py)):
`SKIP` is "index this locally?", `NO_LLM`/`NO_LLM_MARK` is "send this off the machine?" ——
**the two judgements are not the same.**

MCP is the second transmission boundary, where what was indexed is opened to an LLM.  So:

```
every tool filters documents by documents.no_llm at query time (not by SKIP).
```

**A field-level deny list** (rather than a list of paths):

| What to block | Why |
|---|---|
| `abs_path` | the absolute path `/Users/<user>/…` |
| `vector` | useless, and it only inflates the response |
| frontmatter `session_id` · `session_project` · `distilled_from` | `session_project` is an encoded **local path** (280 documents).  Employer names and infrastructure topology in the clear |

**`kal_doc` takes `doc_id` alone.**  This repository has already had and fixed the same
vulnerability ([`docs/STACK.md`](./STACK.md) §7 — `GET /api/runs/..%2F..%2F..%2Ftmp%2Fproof`
returned the file's contents verbatim).  A free path is not reopened.  On return, the
frontmatter is **rewritten through an allowlist**.

> **Gap** — the indexed documents themselves hold sensitive information.  Measured, ~49/376
> documents carry markers shaped like a secret or PII (masked card numbers, colleagues' real
> names, internal email).  `no_llm` is a per-document opt-out, not automatic detection —— and **who is to apply it has not been decided.**

---

## 4. Re-evaluating the benchmark

### 4.1 There are **two** harnesses and they must not be mixed

> ⚠ **The first edition made a cross-citation that `bench/README.md` (not published —— the gold labels hold a real person's name) forbids.**
> It recorded the "selection set A / validation set B split" as a property of `bench/`, but a
> measured grep finds **0** in `bench/README.md` and `hybrid_bench.py`, and 15 in [`src/tune_alpha.py`](../src/tune_alpha.py).

| | `bench/hybrid_bench.py` | `src/tune_alpha.py` |
|---|---|---|
| corpus | 97 documents / 1,577 chunks | 376 documents / 3,370 chunks |
| subject | the BM25+dense **2-component** fusion family | bm25 · chunk · entity · relation, **4-component** weights |
| judgements | 991 pairs · 94 documents · grades 0–3 | separate |
| statistics | Wilcoxon + BH (q=0.05), a pre-specified baseline | **an A/B split** (`--protocol`) |

**Each harness's strengths and limits have to be written separately.**  The first edition
transcribed the latter's protocol as the former's strength.

`bench/`'s real strength: it **caught a loading bug that was pushing nDCG to 1.0 by itself and
left the history.**  The unjudged rate falling 62.6% → 4.8% is the column that caught the accident.

> ⚠ **`bench/` is currently outside version control.**  `kallimachos/.gitignore:11` blocks it
> (the query set's gold labels hold a real person's name; added 2026-08-20).  And yet
> `bench/README.md:8` claims "it is inside version control now, so it will not be lost again".
> **A contradiction, and no reproducibility guarantee.**  It needs a separate fix (a private repo, or excluding only the named files).

### 4.2 The consumer is not a person

nDCG, MAP and MRR rank **topical relevance**.  This system's consumer is an LLM and the search
results go in as the **evidence** for an answer
([Redefining Retrieval Evaluation in the Era of LLMs](https://arxiv.org/abs/2510.21440) —
published 2025-10-24, retrieved 2026-08-20 ·
[Beyond Relevance: Utility-Centric Retrieval **in the LLM Era**](https://arxiv.org/abs/2604.08920) —
published 2026-04-10, retrieved 2026-08-20, a SIGIR 2026 tutorial):

> Relevance metrics do not directly measure whether the retrieved evidence **improves what is generated**.

**Decision** — relevance evaluation is not discarded.  **Its role becomes a regression gate**, and
ranking judgement moves to the answer level.  Per-document utility judgement is used **only as a
diagnostic** —— "is this alone enough to answer" assumes documents are independent, and scoring 0
for one that is useless alone but essential in combination inherits the very defect it should fix.

### 4.3 The number of queries — decided by statistical power, not by TREC convention

Currently 58 queries (49 lookup + 9 synthesis).  TREC convention is **50** topics (the 43 in
[TREC DL 2019](https://trec.nist.gov/pubs/trec28/papers/OVERVIEW.DL.pdf) is a 2019 per-task
figure — published 2019-11).

**A convention is a reporting standard, not a guarantee of power.**  This repository's own
calculation (L4) demands **130–170** for dz≈0.28 at 80% power:

```
 80 queries → 60% power      (35% when split)
100 queries → 70% power      (43% when split)
127 queries → 80% power      (253 needed if the split is kept)
```

> ⚠ The first edition's "raise it to 80–100 and split into 40–50 each" **lowers** power to
> **35–43%.**  127+ unsplit, or 250+ if the split is kept.  Otherwise the target effect size
> has to be **pre-declared** at Δ≥0.05 and n recalculated.

### 4.4 Priorities

| | Proposal | Why |
|---|---|---|
| **0** | **Measure the retrospective-document error** | A person reads 30–50 of the source documents behind the 44 drifts and measures the rate of `document date ≠ fact date`.  **Without that number the point-in-time gold set cannot be built** —— build the gold from document dates and the system cuts on the same dates, making it circular (it would only measure whether §3's `as_of` runs) |
| **1** | A time-axis query set (§4.5) | The current test set has **0** temporal queries |
| **2** | Answer-level evaluation | §4.2.  Fixed generator, fixed prompt, fixed top-k → scored against nuggets.  **The judge is a model from a different family**, and 20% is cross-judged by a person to record κ |
| **3** | 127+ queries | §4.3 |

**What not to do**: tear up the current harness.  The statistical procedure is sound.

### 4.5 How to build the time-axis queries

> ⚠ **The first edition's gold design reproduces the 2026-08-18 accident exactly.**
> `gold = the document at that time` and `gold = a date` are **a binary gold set with 0
> judged irrelevants (grade 0)**.  `condense()` strips the unjudged, so with none irrelevant
> everything left is relevant → **nDCG=1.0**.  The current qrels avoid that because there are on average **6.6** irrelevants per query (57 of 58 queries have one).

**The rule**: pool the time-axis queries too and judge **at least 5 irrelevants per query**.
Where that is impossible, **forbid** `condensed` and report raw nDCG + MRR + the unjudged rate only.

The metrics are temporal **QA**'s —— the **EM · F1** family.

> ⚠ The first edition's "the standard is MRR and Hits@k" is the standard for temporal KG
> **completion (TKGC)**, not temporal **QA (TKGQA)**.  The cited
> [emergentmind page](https://www.emergentmind.com/topics/temporal-qa-benchmarks)
> (last updated 2025-09-22, retrieved 2026-08-20) contains `MRR` and `Hits@` **0 times** and
> lists EM, F1, Temporal Overlap and MBA.  It is also an AI-generated aggregator, so it is
> Commentary grade —— it should be replaced by primary sources (TimeQA, TempReason, CronQuestions).

Three kinds of query:

```
① point lookup    "what state was X in as of 2026-05"     metric EM/F1
                  ⚠ the gold is definable only once §4.4 priority 0 is done
② change detection "when did X's role change"              metric EM (the date)
③ time-independent "what is BM25"        ← **the control**
```

**③ has to be measured for non-inferiority.**  Measuring "it did not get worse" by NHST makes
non-significance the default at 42% power (49 queries), and absence of evidence is not
absence of harm.  The observed CI half-width is ±0.018, so a regression smaller than that is
unfindable in principle. → **pre-declare** a margin δ (δ ≥ 0.02) and read the one-sided CI lower bound only.

**③ requires a snapshot.**  When §2.6 re-summarises 259 entities the entity and relation
vectors change, 2 of the 4 components move with them, and **the time-independent queries'
scores change too.**  Before re-summarising, tag and keep `lr_entities` and the vector tables,
and compare the two snapshots with identical embeddings and weights.  (Reproducibility here was already lost once, to a `/tmp` wipe.)

**The set's acceptance gate**: unjudged@10 < 10%.  In the operating corpus, 75% of the 376 documents are unjudged.

**The test family**: time-axis metrics **do not join** the existing Wilcoxon+BH family ——
joining it widens what is corrected for and eats the existing power.  A separate family, a new qrels file.

---

## 5. Unresolved

| | Item |
|---|---|
| **Priority 0** | **The retrospective-document error is unmeasured** —— §4.4 priority 0.  Without it the point-in-time gold set is circular |
| | **The reproducibility of the change verdict is unmeasured.**  An LLM picks "what changed", and rerunning the same input loses 39% of the entities (private research note "OKF/OpenWiki vs LLM-wiki KG experiment" §3.2 —— measured 2026-08).  A majority vote may be needed |
| | **Who applies `no_llm`** is undecided (§3.6 Gap).  There is no automatic detection |
| | **There is no migration or rollback section.**  Adding a column = a full `mode="overwrite"` rebuild.  Which step of `rebuild_all.sh` it starts from, how long it takes, the DB backup, and backward compatibility for existing consumers (`plugin/`, `web/`, `api/`) reading an old DB without the new columns are all undecided |
| | **The MCP runtime is undecided** —— the language (Python?  an extension of `api/main.go`?), where it is deployed, and how it coexists with `kal_lock.py`'s exclusive write lock |
| | **`bench/` reproducibility** —— it is outside version control while the README claims the opposite (§4.1) |

---

## 6. Correction history

### Revision 1 → 2 (dev-deep-review round 1, 13 blockers)

| First edition | Actually |
|---|---|
| obsidian fragment count 224 · 60 | **123 · 44** —— the append-only cache's 813 superseded lines were not removed |
| date coverage 376/378 | **374/378** (on the three keys).  376 includes the undocumented `updated` |
| maximum span 130 days | **132 days** |
| re-summary 259/7,620 = 3.4% | wrong denominator —— 7,620 is **after** `build_canon`, and the threshold applies **before** (7,710) |
| "0 additional calls" | invalidating the cache makes **the first run all 259 calls** |
| `refs` ← frontmatter `sources` | **0** URLs in frontmatter.  It has to be a two-hop join |
| `bench/` does the A/B split | that is `tune_alpha.py`'s.  **A cross-citation the README forbids** |
| 80–100 queries | **lowers** power to 35–43%.  127+ needed |
| time-axis gold = the document at that time | **reproduces the nDCG=1.0 bug.**  Irrelevant judgements are required |
| the standard is MRR and Hits@k | that is TKGC's.  Temporal QA uses **EM and F1** |
| T-HiGra as the basis for the interval model | it is **TG-RAG**, and it is **a counter-example** (single timestamp + parallel edges) |
| bi-temporal | `first_seen`/`last_seen` are **uni-temporal**.  No invalidation |
| splitting `timeline` out saves tokens | `docs[]` costs thousands.  The reason is **that the arguments differ** |
| the example entity `claudecode` | **not in the DB** (`Claude Code`).  `name_norm ≠ merge_key` for 5,999/7,620 |
