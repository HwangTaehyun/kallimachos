# The pipeline — from conversation logs to a searchable knowledge DB

The whole path that turns `~/.claude/projects/**/*.jsonl` and the super-brain vault into one
LanceDB.  It records **what each step does, why it comes in that order, and what has to be
re-run from where when something changes**.

> ⓘ **Step 3 has moved.**  Documents now land in an openwiki bundle rather than in the vault, and
> a second source —— any Markdown tree, not just one vault —— enters at the same point.  That path is
> [`OPENWIKI-PIPELINE.md`](OPENWIKI-PIPELINE.md).  Everything else on this page —— masking, the three
> caches, `group_nodes`, indexing, search weighting —— is unchanged and current.

---

## The whole picture

```
   ┌─ input ──────────────────────────────────────────────────┐
   │  ~/.claude/projects/**/*.jsonl      raw Claude Code sessions │
   │  super-brain/wiki/**, raw/**        notes the user curated   │
   └──────────────────────────────────────────────────────────┘
                             │
   Step 1  ingest_sessions.py      secret masking · transcript extraction
                             ▼   ~/.kal/sessions/session_docs.json
   Step 2  distill_sessions.py     LLM distillation → brain-ingest format documents
                             ▼   ~/.kal/distilled/*.md        [cache: .done]
   Step 3  promote_distilled.py    move into the vault + a wiki corpus page + a commit
                             ▼   vault/raw/conversations/sessions/*.md
   Step 4  lr_extract.py           entity and relation extraction + profile summary + **the time axis**
                             ▼   ~/.kal/lr_kg.json    [cache: lr_cache, lr_summary_cache]
   Step 5  schema_v3.py            chunking · embedding · inverted index · the graph tables
                             ▼   ~/.kal/db
   Step 6  tune_alpha / ablate     measuring the weights and parameters
   Step 7  export_graph / webgl    the visualisation artifacts
                             ▼
   ┌─ reading ────────────────────────────────────────────────┐
   │  kal_search.py       a person, at the CLI                 │
   │  /kal-search skill    Claude, through the CLI             │
   │  kal_mcp.py          Claude, **directly over MCP**  ← 5 tools │
   └──────────────────────────────────────────────────────────┘
```

**The reading layer is not a table.**  Everything up to Step 5 is storage
([`ERD.md`](./ERD.md)); reading joins on top of it each time.  Change storage and a re-index
is needed (64 seconds); change reading and a restart is enough.

```
   kal_search      chunks (vector + BM25) → documents
   kal_entity      lr_entities → documents → vault files (refs)
   kal_timeline    lr_entities → documents
   kal_neighbors   lr_entities + lr_relations → documents
   kal_doc         documents → vault files
```

Only `refs_of` and `kal_doc` read **files outside the DB**.  That is where a boundary is needed
(`kal_doc` takes `doc_id` alone, and a `refs` slug is validated against `[\w-]{1,64}` —— both
places were really breached once).

**Only `kal_search` loads the embedding model.**  The other four do not —— `kal_entity` 150ms,
`kal_timeline` 121ms, `kal_neighbors` 13ms, while `kal_search`'s first call alone takes 5.0
seconds (470MB of model into memory).  It is **once per process**, so in a long-lived process
like MCP it is 78ms after that.  The CLI is a new process per call and pays it every time.

> ⚠ **The embedding model is pinned offline** (`HF_HUB_OFFLINE=1`).
> The weights are already local (470MB), and yet `sentence-transformers` asks huggingface "is
> this current?" on every load —— measured, **29 HEADs · 0 GETs · 7.5 seconds**.  Round trips
> that check rather than fetch.
>
> **Safety, not speed, is the real reason.**  If the model updates quietly, the 3,370 stored
> chunk vectors and the query vector become **the output of different models**.  The dimension
> (384) is the same, so the cosine computation runs without error and only the results are
> ruined.  `meta.embedding_model` pins the name —— and **the revision was left unpinned**.
>
> To change the model, do it deliberately: fetch once with `HF_HUB_OFFLINE=0`, then re-embed
> all of Step 5.

### This repository is itself a Claude plugin

```
.claude-plugin/plugin.json   the manifest
.mcp.json                    ${CLAUDE_PLUGIN_ROOT}/src/kal_mcp.py
skills/kal-recall/SKILL.md   tells the model when to use it
```

```bash
claude --plugin-dir /path/to/kal
```

`src/` has to be **inside** the plugin root for a marketplace install to work —— it used to sit
under `claude-plugin/` pointing outwards with `../src/`, so a clone alone did not run.

### Running standalone with Docker

```bash
docker compose -f docker-compose.kal.yml run --rm kal src/schema_v3.py
```

The `Dockerfile` **bakes the embedding weights in at build time.**  The container runs without
a network from the moment it starts —— the same reason as the offline pin above.  The vault is
mounted read-only and the DB and cache go in a named volume (`kal-data`).  Losing
`lr_cache.jsonl` costs 4,913 seconds to re-extract.

In one line: `./rebuild_all.sh`

---

## Step 1 — session extraction and secret masking

```bash
python ingest_sessions.py
```

Reads `~/.claude/projects/**/*.jsonl` and takes only the `user` and `assistant` bodies.  Tool
calls and their output are already excluded at this step.

**Why masking comes first.**  The session logs held real credentials:

```
   Slack tokens      52 · across 3 projects
   AWS access keys   32 · across 4 projects
   Anthropic keys    12 · across 3 projects
   OpenAI keys        2 · across 1 project
```

Deleting them after indexing is already too late —— they have spread into chunks, vectors and
the knowledge graph.  Undoing it means a full rebuild.  So this sits **at the very front**.

```
   sk-ant-api* · sk-proj-* · AKIA* · xox[baprs]-* · ghp_* · JWT ·
   PEM private keys · Bearer tokens · Slack webhooks   →   [REDACTED:TYPE]
```

Four were actually masked — the other 94 sat in tool output and attachments and were already
dropped at the parser stage.  The output is rescanned with the same patterns as the first scan
to confirm **0 remaining**.

> ⚠️ This step only keeps them out of the index.  The originals in `~/.claude/projects/*.jsonl`
> are untouched.  **Rotating the 32 AWS keys is recommended.**

The output `~/.kal/sessions/session_docs.json` — 464 sessions · 12.1M characters.  It is a
transcript and **not an index target.**  It is kept only for comparison against the source.

---

## Step 2 — LLM distillation (the brain-ingest format)

```bash
python distill_sessions.py --workers 8
```

A transcript must not be indexed as-is.  The `raw/conversations/` rules of the `brain-ingest`
skill forbid it — *no speech transcripts · no tool logs · no several topics in one document*.

So an LLM (haiku) reads the session and rewrites it as a document holding **only the
conclusions, decisions and reasons**.  Several topics split into at most 3 documents.

```yaml
title: "Establishing the performance framework — meets/exceeds criteria per level"
type: conversation
captured: 2026-07-14
origin: claude-session
doc_type: decision      # plan|analysis|design|discussion|decision|retro|investigation
why_captured: "to reuse when assessing Lv2/Lv3/Lv4 consistently from here on"
tags: [performance-review, exceeds-criteria]
session_id: d35ba5db-…  # for tracing back to the source
```

### Cache ①  `~/.kal/distilled/.done/<session_id>`

One empty file means "this session is done".  A rerun skips any session carrying the marker.

**Why it is needed.**  464 sessions × an LLM call = 54 minutes.  Interrupted halfway means starting over, which is not tolerable.

**Important — a failure leaves no marker.**  At first the marker was written without
distinguishing success from failure, and the result was that **41 of 180 sessions (23%)
disappeared silently.**  The median skipped session was a perfectly good 9,266-character
conversation, and calling it once by hand succeeded immediately.  The cause was a transient
failure under 12 concurrent workers, with no retry.

```
   ok    a document was produced   → the marker is written
   skip  the LLM judged it small talk → the marker is written
   fail  the call failed or the format broke  → no marker.  Just run again to retry
```

### The exclusion list

```python
EXCLUDE_PROJECTS = ("quad",)      # matched as a substring of session_project
```

Add here to leave a project out of the corpus.  The original transcripts are still in Step 1's
output, so editing the list alone is enough to undo it.

### The after-the-fact review list

`~/.kal/distilled/_distill_review.tsv` — every `slug · title · doc_type · why_captured ·
session_id`.

`brain-ingest` requires "the user confirms the classification and why_captured", and hundreds
of them cannot be asked one by one.  This is the compromise: **the LLM proposes and this list
is reviewed afterwards.**  The list is built by **walking the real files on disk**, not from
memory — to prevent the accident where a resumed run only contains that round's share.

---

## Step 3 — moving into the vault

> ⚠ **Superseded, 2026-09-02.** Distilled documents now go into an **openwiki bundle**
> (`openwiki_emit.py`, `just openwiki-sessions`), not into the vault, and the indexer reads the
> bundle. The destination changed for one reason: the vault is a place a person edits, so writing
> generated documents into it made "what did I write" and "what was generated" the same question.
> The bundle answers it by construction —— ownership is a frontmatter line, and a page this tool
> did not write is never touched. See [`OPENWIKI-PIPELINE.md`](OPENWIKI-PIPELINE.md).
>
> `promote_distilled.py` still runs and still passes its self-checks; it is the older path, kept
> because it is what the measurements below were taken against.

```bash
python promote_distilled.py            # --dry-run · --no-commit
```

Moves the distilled documents to `vault/raw/conversations/sessions/`, makes one wiki corpus
page, updates the index and log, and commits.  **It is idempotent** — a rerun empties the target
folder and refills it, and the log block is replaced rather than appended to.

### How much of brain-ingest's 10 INGEST steps is actually done — honestly

| Step | | Basis |
|---|---|---|
| 1 read the whole source | ✅ | the LLM reads all of it |
| 2 establish `why_captured` | △ | the LLM proposes + the review list |
| 3 discuss the takeaway | ❌ | hundreds of sessions cannot be discussed one by one |
| 4 a `wiki/sources/` summary | △ | **one corpus page** only |
| 5 update existing pages | ❌ | an automatic merge quietly covers a contradiction |
| 6 new entity pages | ❌ | the `lr_entities` knowledge graph plays that role instead |
| 7 `wiki/index.md` | ✅ | |
| 8 `wiki/log.md` | ✅ | |
| 9 lint | ❌ | |
| 10 report | ✅ | |

Why 4 was reduced to one corpus page: putting hundreds into `wiki/sources/` would bury the 98
hand-curated notes.  That breaks the gold-in/gold-out principle itself, so shrinking the scale
was judged the more faithful reading.  **A compromise, not full compliance.**

To undo it:
```bash
git -C ~/github/HwangTaehyun/super-brain revert --no-edit <sha>
rm -rf ~/github/HwangTaehyun/super-brain/raw/conversations/sessions
```

---

## Step 4 — knowledge-graph extraction

```bash
LR_WORKERS=8 python lr_extract.py       # --restart · --no-summary
```

**It splits into two sub-steps whose costs are completely different.**

### 4-a  extraction (LLM · expensive)

All of `wiki/**` + `raw/**` is cut into 2,400-character chunks (overlap 200), and an LLM reads
<!-- CHUNK_CHARS = 2400 · verify_docs.py checks this line against the source -->
each chunk and emits one JSON.

```json
{"entities":      [{"name":"AI language model","type":"tool","description":"…"}],
 "relationships": [{"source":"…","target":"…","keywords":"…","description":"…"}]}
```

**The description is not a sentence from the source but a summary the LLM writes from that
chunk alone.**  A ceiling of 20 entities and 25 relations per chunk.

**There are 10 types** (reworked 2026-08-21, `lr_extract.ENTITY_TYPES`):

```
artifact  files · paths · config keys · schema fields    tool     software that is run or called
project   a repository or product with a boundary        method   a technique or procedure (the old pattern included)
failure   errors · bugs · failure modes                  decision something chosen when there were alternatives
metric    measurements · benchmarks                      event    something that happened at a point in time
actor     people · teams · organisations                 concept  ★ what fits none of the above
```

Why it changed —— under LightRAG's default 7, `concept` became **45.6%** (3,473/7,620).  Worse,
**the types distinguished nothing**: the median degree was 2 for every type, so knowing the
label told you nothing about that node's role.

The cause is the corpus.  **77.3% of this vault is Claude Code session logs** (work records),
while LightRAG's default types are for general prose, where `person`/`organization` are first-
class citizens.  There was nowhere to put **the errors, files, numbers and decisions** that are
the protagonists of a work record, so they all fell into `concept`.

What matters in the prompt is not the list but **the order and the counter-examples**.  "Read
the list in order and take the first that fits.  `concept` is the last resort" + a "this is not
one" example for each type.  The old prompt said only `must be one of: [the list]`, so it never
said **when not to use `concept`**, and everything ambiguous went there.

Two were merged:
  · `pattern` → `method`.  The LLM split them 16.2%/14.9%, not because there is a boundary but
    because it could not tell them apart.  A near-synonym pair works against the goal of "the
    type predicts the structure".
  · `person` + `organization` → `actor`.  Together only 5.2%, but **every personal-data exposure
    comes from here**.  One type means one gate —— masking, the viewer filter and the pre-release
    scan all finish on a single condition.  With two conditions, one of them eventually gets missed.

First measured run: **1,031 chunks · 3 hours 3 minutes · 1 failure.**

**The verdict —— the criteria were set before the re-extraction.**  Criteria invented afterwards
make any result a "success".  So three were pinned down before the numbers were seen, with a
rollback if they were not met.

```
                          before        after       criterion        verdict
   concept share           45.6%        15.8%       under 30%          ✅
   median degree spread   2 values·1   3 values·2   3+ values·2+       ✅
   deg<=1 overall          44.0%        44.5%       within 5pp worse   ✅
```

② passed **exactly on the threshold** —— not a landslide.  What is clear is that the labels have
started predicting structure: `project` is median 3 · deg≤1 28%, `metric` is median 1 · 63%.
Within one graph, the type alone now separates "hub side" from "leaf side".

But as ③ says, **fragmentation did not fall at all.**  44.5% still have degree 1 or less.  It was
never a problem the type system could solve —— it is a relation-extraction problem.  Saying "the
graph is dense now" on the strength of this rework would be wrong.

⚠ Criterion ② was first written as "2 or more distinct values", and **the baseline already met
it** (`person` 1, everything else 2).  A criterion that cannot discriminate is not a criterion.
It was changed to "3 or more values + a spread of 2 or more" **before** the results were seen,
and the baseline was checked against the new one.  Not moving the goalposts —— fixing a broken ruler.

Final measurement: **8,179 entities · 12,355 relations · 10 types** (252 summaries · 0 failures · 32 minutes).

> **Lower `LR_WORKERS` on a long run.**  Measured 2026-08-21 —— **immediately after** 905 chunks
> ran for 99 minutes at 14 workers, the summary step that followed hit a 91% failure rate (218
> transport · 109 shape, all "empty response").  Calling `claude -p` by hand at the same moment
> worked, so it is not the model but **an accumulated concurrency limit**.  Rerunning with
> `LR_WORKERS=4` passed with 0 failures.  The gate stopped the graph being polluted, but two
> hours are gone.

### Cache ②  `~/.kal/lr_cache.jsonl`

One line = the LLM response for one chunk.  Appended and flushed the moment each chunk finishes.

```json
{"doc":"raw_articles_AI 2027","idx":5,"h":"a3f1c8e02b91",
 "entities":[…18 of them],"relationships":[…18 of them]}
```

**Why it is needed.**  A three-hour run interrupted halfway starts over.  And reruns are
frequent — every deletion or addition of a document goes through this step, and without the
cache that is three hours every time.

**The effect, measured.**  Deleting the quad project's 67 documents and rebuilding:

```
   chunks in the current corpus   877
     cache hits                   876  → no LLM call
     new (1 chunk whose content changed)  1  → 1 LLM call
   in the cache but unused        154  → belonging to deleted documents.  Left alone
   ────────────────────────────────────────────
   3 hours 3 minutes  →  81 seconds
```

**The key includes a content hash.**  At first it was only `(document path, chunk number)`, and
that **reuses the old extraction whenever the path matches, even if the content changed** — a
silent staleness.  `h` = the first 12 chars of the chunk body's sha1 went into the key, so a
content change re-extracts automatically.

```
   before  ("raw_articles_AI 2027", 5)
   after   ("raw_articles_AI 2027", 5, "a3f1c8e02b91")
```

If an existing cache has no `h`, migrate it with `python migrate_lr_cache.py` (it back-computes
the hash from the current documents.  Documents known to have changed are excluded and re-extracted).

**A failed chunk is not cached.**  A transient error must not freeze permanently into an empty result.

### 4-b  fixing the nodes (`group_nodes`) + the profile summary

> The function was called `merge` and was **renamed to `group_nodes`** (2026-08-20).
> It does not only join —— it **splits** too, and `merge` told only half the story.

Groups same-named things together.  This is **pure code, under a second**.

```
   description  the description fragments from each chunk
   frags        (document, description) **pairs**   ← the basis of the time axis
   hist         fragments of earlier versions       ← content from before a document was edited
   doc_ids      the union of the documents it appeared in
   degree       the number of relations on that entity (undirected, after duplicate merging)
   first_seen   the date of the earliest source document   ← computed outside the LLM
   last_seen    the date of the latest source document
   events       fragments **promoted to events**    ← below
```

**Nodes are decided in two directions.**

```
join    aliases.yml    several names → one node
split   homonyms.yml   one name → several nodes     ← added 2026-08-20
```

The split happens on **the very line** where `ents[k] = {...}` runs:

```python
d = (e.get("description") or "").strip()
n = split_sense(n, d, r["doc"])        # ← reads homonyms.yml and changes the name
k = canon.get(n.lower(), n.lower())    # ← so the key differs
if k not in ents:
    ents[k] = {...}                    # ★ a new node
```

**Only at this moment** are all three in hand at once: the original name, this fragment's
description, and this fragment's source.  One line later the fragments are fused and cannot be
recovered.  Neither the summary (4c) nor the index (Step 5) can do it.

`split_sense` is **string comparison alone** (`cue in fragment + path`).  It does not use an LLM
—— there are over 21,000 fragments, and asking about each would add another 4,913-second
extraction, with a 39% variance on re-extraction assigning them differently every time.

**A relation's two endpoints are split along with it.**  Split the entities alone and relations
point at a name that no longer exists, becoming ghost references (the 217 Step 5 discards every time).

The implementation is [`src/entity_resolve.py`](../src/entity_resolve.py) (`load_homonyms`,
`split_sense`); the rules and the full reasoning are in [`docs/ENTITY_RESOLVE.md`](./ENTITY_RESOLVE.md) §12.

Measured: the single `vault` node (23 documents · degree 76) split into `Obsidian vault`
(20 documents), `1Password vault` (3) and `vault` (the remaining 6 with no cue).

**`events` —— the fragments are not thrown away.**  They used to be deleted from the output with
`v.pop("frags")`, leaving the summary alone.  That folds 37 fragments into 900 characters and
**the detail disappears.**  They are now carried as dated events —— measured, 24,106 events at
**5.9MB**, **4.8×** the summary (1.2MB).  Not free.  The reason to keep them anyway is not their
value but **their irreversibility** —— once a summary is folded the fragments cannot be
recovered, and re-summarising with a fixed prompt needs them.  It is also what lets the
consuming LLM distrust our summariser's verdict and read for itself.

**Editing a document adds one more event.**  `lr_cache.jsonl` is append-only, so the older line
stays —— measured, **813** were preserved that way and used to be discarded as "duplicates",
affecting 49% of entities.  They now go to `history` and become `rev="superseded"` events.
They **do not enter** the `description` summary (content already known to be wrong must not mix into the present narration).

> ⚠ **Records before 2026-08-20 have estimated dates** (`at_exact: false`).  The extraction time
> (`at`) was added to the cache record that day, and earlier lines have no time and fall back to
> the document date —— which makes them **the same date** as the current version.  Deriving it
> from `doc_updated` was tried: of the 813, only **5** could be separated (only 2 of 353
> documents have `updated ≠ created`), so it was not done.
> **What changed is knowable; when is not.**

**Why `frags` is kept separately.**  Before 2026-08-20, `descriptions` (a list) and `docs` (a
set) were collected **separately**.  So there was never any way to recover "which document this
description came from", and the summary flattened the fragments with no time attached.  Keeping
the pairing is what makes ordering them by date possible ([`docs/TEMPORAL_DESIGN.md`](./TEMPORAL_DESIGN.md) §0).

**The summary also emits polysemy candidates** (`senses`).  The summary is **the only point**
where fragments from several documents are seen side by side, so polysemy is visible only there
—— extraction sees one chunk, and the index handles only strings.

```jsonc
{"profile": "…", "changes": [...],
 "senses": [{"label": "Obsidian",  "cue": ["obsidian", "notes", "wiki"]},
            {"label": "1Password", "cue": ["1password", "card", "member"]}]}
```

That judgement used to survive **in the `description` prose alone** —— measured, 17 entities say
"refers to two distinct systems", but as one sentence inside a 900-character string no machine
could use it.  It is now structured in the `senses` column and read by
[`homonym_suggest.py`](../src/homonym_suggest.py).

```bash
python src/homonym_suggest.py          # candidates, in homonyms.yml format
python src/homonym_suggest.py --weak   # including document-cluster signals (many false positives)
```

**Symmetric** with `alias_suggest.py` → `aliases.yml`.  A machine proposes and a person
confirms —— an automatic split quietly poisons the graph, while a wrong candidate in a list is
simply skipped.

`first_seen`/`last_seen` are computed deterministically from the dates **outside the LLM path**
(`_stamp()`).  A failed summary call still has to leave an `as_of` lower bound.

**The date is `effective_date(created, updated)`** —— the later of the two.  Extraction runs on
**the file's current content**, so stamping an edited document with `created` would **backdate**
the revised claim to the original moment (`wiki/entities/qmd.md` is created 04-24 · updated
06-23, a two-month backdating).  The asymmetry is why the later date wins —— stamping a typo fix
late is still true, while stamping a change of meaning early is false.

But simply concatenating the fragments is unreadable.  `Obsidian` has 15 fragments and one
paragraph grew to 6,571 characters.  So **an LLM rewrites them into a single profile, the same
way LightRAG does** (`operate.py:369` `_handle_entity_relation_summary` in
[LightRAG](https://github.com/HKUDS/LightRAG) —— MIT, retrieved 2026-08-17).

```
   1 fragment           kept as-is        (no LLM)
   2–7 fragments        " ".join          (no LLM)
   8+ fragments         the LLM rewrites  ← FORCE_LLM_SUMMARY_ON_MERGE = 8
   over 14,000 chars    summarise in pieces → summarise the summaries (map-reduce)
```

**When the LLM is called it is given the fragments in date order and returns two things** (`summarize_timed`):

```
   [2026-04-09] Note-taking software serving as primary platform …
   [2026-06-25] a markdown-file-based note-taking tool that …
   [2026-08-19] the tool used for knowledge management and documentation
        │
        ├─▶ profile   one paragraph on the present state   → description
        └─▶ changes   **only what actually changed**       → timeline (JSON)
```

The point is **"only what changed"**.  Most fragments repeat the same thing, so including them
all lets noise bury the signal.  The discrimination is confirmed by measurement:

```
   Claude Code  6   05-14 raised to the main runtime → 06-23 qmd memory → 07-19 folded into Orca
   Obsidian     0   ← only the wording differed; nothing actually changed
   nDCG@10      0
```

Fragments with no date (2 documents) cannot be ordered and drop out of the time axis, though
the description itself is still used.  Fewer than 2 distinct fragment dates skips the time-axis path.

The prompt puts the name first, narrates objectively in the third person, and makes the model
**first decide whether a conflict is a different entity with the same name**.  If different,
each separately; if the same, reconcile them or present both with the uncertainty stated.

The measured share of targets:

```
   entities 7,710     8+ fragments 259 (3.4%)
   relations 12,515   8+ fragments  46
   → 305 LLM calls · **4,913 seconds** (measured 2026-08-20)
```

**There is no need to rewrite everything.**  Most have a single fragment, with nothing to synthesise.

The time-axis output, measured:

```
   entities with a timeline   65 / 7,620  (0.85%)  · 112 changes
   relations with a timeline  11 / 12,246 (0.09%) · the relation's **state transitions**
```

**That low 0.85% is the success metric.**  The point is to pick out "only what changed", so a
high rate would mean description scatter had been mistaken for change.

> ⚠ **A silent failure happened once (2026-08-20).**  `KAL_CLAUDE_RELAY` still pointed at
> `host.docker.internal` (for the container) while running on the host, so DNS did not resolve
> and **every** LLM call failed.  But `call_text` swallowed the exception and `summarize_one`
> fell back to concatenating fragments, so the log showed only **"305/305 done · 0 failures ·
> 530s"**.  The output was rubbish and still looked like a summary to a human reader.
>
> → `CALL_STATS` counts failures now and **dies at a failure rate of 10% or more.**  Better than
> surviving and serving rubbish.  A healthy run is 4,913 seconds —— **530 was itself the signal.**
> When running on the host, set `KAL_CLAUDE_RELAY` to `127.0.0.1` or leave it empty.

### Cache ③  `~/.kal/lr_summary_cache.jsonl`

The key = `sha1(PROMPT_VERSION + the name + the sorted fragments + the fragment dates)`.
The value = `{profile, changes}`.

If the fragment set is unchanged the summary is too, so a rerun makes no LLM call.  Only
entities whose fragments changed through an added or deleted document are rewritten.

**Why `PROMPT_VERSION` is in the key.**  The old key was (name, fragments) alone, so editing the
prompt still hit the cache for everything.  Adding the time axis changed the output shape to
`{profile, changes}`, and with the same key **`changes` would never appear.**  Always raise this
value when the prompt changes.  The dates are in the key too —— the same fragments in a
different order give different `changes`.

The old format (a string value) is still read.  Only the new format fills `timeline`.

---

## Step 5 — indexing

```bash
python schema_v3.py
```

Cuts the vault into 500-character chunks (overlap 50), embeds them with
`intfloat/multilingual-e5-small` (384d), and builds 8 tables.

```
   meta          the DB knows its own configuration — model · dimension · chunk size · k1/b · key rules
   documents     doc_id = crc32(path).  origin[BITMAP] · doc_type[BITMAP]
   chunks        text[FTS ngram(2,3)] · vector · origin[BITMAP]
   ix_terms      term · df · idf          ┐ the BM25 inverted index, also kept as plain tables.
   ix_postings   term_id · chunk_id · tf  │ Not used for search.  Only for interrogating
   ix_doclen     chunk_id · num_tokens    ┘ "why this score?" in SQL.
   lr_entities   name · type · description[FTS] · doc_ids[LABEL_LIST] · vector
   lr_relations  src/tgt · keywords · description[FTS] · vector
```

**It is a full rebuild.**  Why not a partial update is in §Undoing and re-running below.

The key design:
```
   doc_id    crc32(path) & 0x7FFFFFFF      path-based → existing ids survive new files
   chunk_id  doc_id * 10000 + seq          stable.  A ceiling of 10,000 chunks per document
```

---

## What leaves the machine

`lr_extract.py` sends chunks to `claude -p` —— **the only point in this pipeline where vault
content leaves the machine**.  Every other step (embedding, BM25, graph building) is local.

What is transmitted **starts from the same rule as what is indexed, but is managed separately.**
`schema_v3.SKIP` is a list deciding "index this locally?", which is not the same judgement as
"send this outside?".  Tie the two together and a new folder in the vault gets transmitted with nobody having decided so.

Two ways to turn it off:

```bash
# ① by path fragment (colon-separated).  It stays in the index and in search
KAL_NO_LLM="Private:Finance" python lr_extract.py
```

```markdown
<!-- ② one document only — write it in the frontmatter -->
---
no_llm: true
---
```

`lr_extract.py` **prints what it is sending, by top-level folder, on every run.**

```
   LLM transmission targets: 376 documents · 905 chunks
     raw 295 · wiki 79 · (root) 1 · Clippings 1
```

`--check-scope` **separates a deliberate exclusion from an accidental divergence.**  Confirmed by measurement:

```
   scope deliberately narrowed   → exit 1 · "in the index but not extracted (not intended)"
   normal                        → exit 0
   1 excluded by no_llm          → exit 0 · marked "deliberately excluded"
```

The basis: the 2026-08-18 adversarial review (security lens).  Widening the scope to the whole
vault made the transmission boundary ride along with the index boundary; what newly goes out
today is only `CLAUDE.md` and `Clippings/Obsidian Changelog.md`, harmless enough —— but **the
future was the problem**.

---

## Step 6 — measuring the parameters

```bash
python tune_alpha.py --split --check-auto     # the controlled condition (only the vault's 98 documents are scored)
python tune_alpha.py --full-corpus            # candidates from 376 documents; scoring still only what is judged
python eval_sessions.py --n 250 --json        # session known-item
python ablate_params.py --json                # 1-hop · depth · N
```

58 queries · 991 judged pairs · nDCG@10 · Wilcoxon+BH (q=0.05) · bootstrap CI.

> ⚠ **Do not read `--full-corpus` as "validation on the operating corpus".**  It puts 376
> documents in the candidate pool, but condensed scoring erases unjudged documents from the
> ranking —— the gold set covers only 94 documents, so **all 278 session documents drop out
> immediately before scoring.**  Measured: 43.8% of the default weights' top 10 are session
> documents, and condensed removes them for free.  Which is why the two modes come out at 0.806
> vs 0.804, nearly identical —— and that agreement is not evidence of robustness but **the
> result of measuring the same thing twice.**  Treating sessions as irrelevant gives 0.549 ——
> the operating nDCG@10 is somewhere in [0.549, 0.806].  (2026-08-18 adversarial review)

The results are in [`docs/HYBRID_METHODOLOGY.md`](./HYBRID_METHODOLOGY.md).  In summary:

```
   weights      BM25 .18 · chunk .05 · entity .18 · relation .59   (vs the baseline: controlled Δ+0.128 · full Δ+0.097)
   top N        20        saturates at Recall@20 0.939
   candidate depth   BM25/chunk 300 · entity/relation 30/40
   1-hop expansion   1.0   turning it off costs -0.039 (p<0.001)
   mode switching    none  the rule did harm instead (Δ-0.017 (p=0.0027))
```

---

## Step 7 — visualisation

```bash
# ⚠ export_webgl.py is **not called by the pipeline.**  See README §2 ——
#   it is a separate renderer with a different UI, and calling it overwrites
#   the artifacts in web/public/galaxy.  Kept only as a reference for reading the structure.
# python export_webgl.py --iters 300      # (retired — do not run directly)
python export_graph.py --min-degree 2 --max-nodes 700 --obsidian <vault>/kg
```

`export_webgl.py` reads LanceDB directly and draws with WebGL2.  The layout (edge springs +
KDTree local repulsion + gravity toward the origin) is precomputed in Python, so there is no
runtime physics engine.

`--obsidian` turns the entities into `[[wikilink]]` notes for Obsidian's graph view to read.
That folder is in `SKIP` and **excluded from indexing** — otherwise a KG → note → index → KG
feedback loop forms.

---

## Undoing and re-running — what changed, and where to start

### The core principle

Delete the original `.md` and **the search results still come out.**  The content has been
*copied* into 6 tables, and `kal_search.py` walks the `chunks` table, not the files.

There are two reasons a partial deletion does not work:

**① `df`/`idf` are global statistics.**  Drop 67 documents and every idf changes, including for
words unrelated to what was deleted.  Delete only the rows and the remaining documents' BM25 scores are all wrong.

**② Entities are merged across documents.**  If `Terraform` came from 5 deleted documents and 3
that remain, deleting rows cannot undo the sentences left in its description, and the degree is
wrong too.  An entity that existed only in the deleted documents is left an orphan with empty `doc_ids`.

`sync_v3.py` does perform a partial deletion, but **only as far as clearing dead references.**
For exactness, the merge has to be redone from scratch over the remaining documents.

### By situation

| What changed | What to re-run | Cost |
|---|---|---|
| **a few vault notes edited or added** | `sync_v3.py` | seconds.  The KG is only recorded in `stale_docs` |
| the above + bringing the knowledge graph up to date | `lr_extract.py` → `schema_v3.py` | the LLM only for changed chunks.  The rest is cached |
| **some session documents deleted** | delete the files → Steps 3–5 | minutes, thanks to the cache |
| **a whole project excluded** | add to `EXCLUDE_PROJECTS` → delete the files → Steps 3–5 | measured below |
| **new sessions added** (a new conversation) | Steps 1–5 | the LLM only for the new sessions |
| **a schema column added or changed** | `schema_v3.py` (the LR cache stays valid) | **64 seconds.**  `mode="overwrite"`, so a full rebuild —— check the consumers reading the old columns first (`plugin/`, `web/`, `api/`) |
| **the summary prompt changed** | raise `PROMPT_VERSION` → `lr_extract.py` | only the summaries are recalled (305 · about 82 minutes).  The chunk-extraction cache stays valid |
| **`homonyms.yml` / `aliases.yml` edited** | `lr_extract.py` → `schema_v3.py` | **minutes.**  Only the groups that split or joined are re-summarised.  The whole chunk-extraction cache stays valid |
| the embedding model replaced | `schema_v3.py` (the KG cache stays valid) | every embedding recomputed |
| the chunk size changed | `lr_extract.py --restart` → `schema_v3.py` | **the cache is void.  3 hours** |
| the extraction prompt changed | `lr_extract.py --restart` | **the cache is void.  3 hours** |

### Measured — removing the quad project's 67 documents

```
   ① add "quad" to EXCLUDE_PROJECTS        1 line of code
   ② delete those documents from ~/.kal/distilled    67 (by session_project)
   ③ promote_distilled.py                  2s    (empties the target folder and rebuilds)
   ④ lr_extract.py                        81s   ← 876 cache reuses · 1 LLM call
   ⑤ schema_v3.py                         83s
   ────────────────────────────────────────────────
                                     about 3 minutes
   + re-tuning and regenerating the graph    10–15 minutes
```

The result:

```
                    before      after
   documents        443    →    376     -67
   chunks         3,936    →  3,372     -564
   ix_postings  2,491,228  → 2,108,773  -382,455   ← idf recomputed globally
   lr_entities   10,101    →  8,132     -1,969
   lr_relations  15,287    →  12,771    -2,516
```

**What was actually rebuilt, from the entities' point of view:**

```
   2,016   appeared only in the deleted documents  → deleted outright (no LLM needed)
     120   spanning both sides                     → **the description is rewritten**  ← the real work
   8,151   unaffected                              → left alone
```

Those 120 rewrites happen in the 4-b merge.  Only the ones with 8 or more fragments need the
LLM, and even those are reused from `lr_summary_cache` when the fragment set is unchanged.

### Incremental sync (`sync_v3.py`)

```bash
python sync_v3.py --dry-run     # the verdict only — always first
python sync_v3.py
```

```
   documents   upsert on add/modify · delete on remove · rename = delete+insert
   chunks      only that doc_id is dropped and rebuilt (embeddings recomputed)
   ix_*        **rebuilt in full** — df/idf are global, so a partial update is inaccurate
   lr_*        only dead references cleared + a record in stale_docs
```

A rename is decided when the same `content_hash` is on both the deleted and the added side.

**A limit — the knowledge graph does not follow automatically.**  It needs LLM extraction.  The
changed documents are noted in `stale_docs` and brought back with `refresh_kg.py` (below).

---

## Concurrent runs — writing is exclusive

`schema_v3.py`, `sync_v3.py` and `manual_index.py` all replace tables with `mode="overwrite"`,
and `sync_v3` mixes delete and add.  Overlap them and one reads a table mid-write from the
other, leaving **a quietly inconsistent DB**.  Silent corruption rather than a crash, so it is
hard to notice.

[`kal_lock.py`](../src/kal_lock.py) puts an `flock` on `~/.kal/.write.lock`.  All three entry
points acquire it in `__main__`.

```
   $ python schema_v3.py          # while sync_v3 is running
   ❌ the DB is already in use — sync_v3 · pid 821 · 2026-08-18 16:15:43
      Editing the same DB concurrently corrupts it silently.
      Run again once that job finishes.
```

**It dies immediately rather than waiting.**  A write in this pipeline takes 90 seconds to 3
hours, so waiting in silence makes the user think it has hung.  Naming the holder and dying is better.

Read-only work (`kal_search.py` and friends) takes no lock — LanceDB does not block writes
during a read, and what needs blocking is writers colliding with each other.

> An item left as "concurrency — unverified" in the 2026-08-17 review.

---

## KG refresh — when staleness accumulates, and how to clear it

### What decides that something is "stale"

A `content_hash` comparison, and nothing else ([`schema_v3.py:882`](../src/schema_v3.py)).
**Not mtime** — opening and closing a file leaves no mark.

```
   added      a new doc_id                                        → stale
   modified   the same doc_id with a different content_hash        → stale
   renamed    the same content_hash on the deleted and added side  → stale (the path changed)
   deleted    a doc_id that vanished                               → not stale.  prune_kg only clears references
   unchanged  identical hash                                       → nothing happens
```

Why `deleted` is not treated as stale: there is nothing to re-extract.  Removing it from the
`doc_ids` of the entities and relations that pointed at it is all, and `prune_kg` does that at once.

### Where it accumulates

The `stale_docs` table — `doc_id · path · reason · marked_at`.
`mark_stale()` at [`sync_v3.py:114`](../src/sync_v3.py) **appends**.

Three things to know:

```
   ① it accumulates     a row is added on every sync run
   ② duplicates appear  editing the same document twice makes two rows
                        → count **distinct doc_id**, not rows
   ③ a rebuild empties it   schema_v3.py:1425 does drop_table("stale_docs")
                        because doc_ids and the KG are both remade, making old marks false alarms
```

### Clearing it — `refresh_kg.py`

```bash
python refresh_kg.py --check      # status only.  exit 1 past 20%   (WARN_RATIO = 0.20)
python refresh_kg.py              # extract → build the graph → export
python refresh_kg.py --no-export  # skip regenerating the graph artifacts
python refresh_kg.py --force      # run even with nothing stale
```

What runs — matched against the Step numbers above:

```
   Step 1  session extraction · masking  ✗ skipped   (the conversation logs are already in the vault)
   Step 2  LLM distillation              ✗ skipped
   Step 3  moving into the vault         ✗ skipped
   ────────────────────────────────────────────────────────────────
   Step 4  knowledge-graph extraction    ✔ lr_extract.py
                                    the cache keys on the chunk hash → only changed chunks call the LLM
                                    the profile summary happens here too (8+ description fragments only)
   Step 5  indexing                      ✔ schema_v3.py
                                    global merge → entity and relation vectors rebuilt → chunks and the inverted index
                                    drops stale_docs when it finishes
   Step 6  parameter measurement         ✗ skipped   (the weights are already set.
                                             to measure again, run tune_alpha.py separately)
   Step 7  visualisation                 ✔ export_graph.py      graphml · graph3d · kg/ notes
                                    export_kal_graph.py  kal-graph.json (+ communities)
                                    the plugin build       viewer/galaxy.html · the plugin
   ────────────────────────────────────────────────────────────────
   last    verify_docs.py                ✔ **only checks** that the documents' numbers match reality
                                    (--fix is deliberately not called — refresh_kg.py:157)
```

**So: from Step 4 onwards.**  Steps 1–3 (collecting, distilling and moving sessions) are needed
only when bringing in new conversations, and Step 6 (evaluation) only when resetting the weights.

Measured — the whole span with `--force` and nothing changed: **130 seconds**
(every extraction cache hit · graph build 74s · export and build the rest).

**Why a partial build is impossible** — entity merging is global.  Change one document and its
entities have to be merged again with the entities of *every other document*
(see [`ENTITY_RESOLVE.md`](./ENTITY_RESOLVE.md)).  So ② is always the full set.  At a measured
88 seconds there is nothing worth saving.

So `refresh_kg.py`'s benefit is not "it calls the LLM less" — the cache already does that ——
but that **it is one line, and the marks are reliably cleared when it finishes.**

### When to run it — the basis for 20% (now weaker)

A measurement that hides N% of documents from the KG component, imitating "the KG does not know that document".

```
   stale share  nDCG@10   vs baseline
   ────────────────────────────────
     0%         0.804      —          ← deterministic (nothing hidden)
    20%         0.775     -0.029      ← a single draw
    50%         0.714     -0.090      ← a single draw
   100%         0.711     -0.093      ← deterministic
```

⚠ **The middle two rows are single draws and move a lot with the seed.**
The 2026-08-18 review measured them again across 8 seeds:

```
   20% stale   mean 0.7673 · seed range 0.065
   50% stale   mean 0.7277 · seed range 0.055
```

The range rivals the size of the loss.  So **"-0.029 at 20%" cannot be stated to that precision.**

Which leaves 20% justified by only two things —— the first of the original three ("the loss just
clears the resolution limit") is withdrawn because of the variance above:

1. **It is cheap.**  A rebuild is about 2 minutes on cache hits, so running it often costs
   little.  If it were expensive, raising the threshold and accepting the loss would be right,
   but there is no reason to.
2. **The damage is not irreversible.**  Even stale, BM25 and the chunk vectors still find the
   document.  The ranking gets worse; the document does not disappear.

⚠ **The simulation's own validity is off in two directions.**  Hiding a document models a
*deletion*.  A `modified` document, which is what actually happens often, **still holds its old
extraction** —— which may be worse than nothing, or better.  The earlier note only recorded that
random selection underestimates the damage; this one overestimates it.  Both are unverified.

On 376 documents, run it once **about 75** have accumulated.

---

## Communities — grouping by 'topic' rather than type

`export_kal_graph.py` exports Louvain communities alongside the graph.

```
   type       'BM25'(concept) · 'LanceDB'(tool)      →  **what** it is
   community  both in the 'qmd · BM25 · Hermes' clump →  **what it is used with**
```

Measured (2026-08-17 · 7,756 nodes · 12,228 edges):

```
   Louvain communities   261 · modularity 0.862
   30 or more members    20  (covering 6,386 nodes = 82%)     ← MIN_COMM = 30 · TOP_COMM = 20
   the rest              folded into 'other' (1,370)
```

modularity 0.862 means **a very distinct structure** (anything above 0.3 is taken as meaningful).
The top communities read straight off:

```
   qmd · BM25 · Hermes                     705    the hybrid search engine
   Obsidian · Vault · LLM Wiki             692    personal knowledge management
   Agent-3 · OpenBrain · Agent-4           647    the AI 2027 scenario
   dokploy · Traefik · Cloudflare          487    deployment and infrastructure
   Hermes Agent · Pattern B · Implicit …   470    agent termination patterns
   ARPA · M4 · 삼쩜삼                        371    marketplace evaluation
   pm2 · yarn build · 환경변수                362    Next.js deployment troubleshooting
   BlackHole · Whisper · Multi-Output …    287    system audio capture
```

### The name — the LLM writes it, and the entity list stays as a subtitle

`export_kal_graph.label_communities()` shows the top 25 entities of each community and receives
a topic name and a one-sentence summary.  The cache (`~/.kal/comm_label_cache.jsonl`) keys on
**the hash of the top 12 member names** — Louvain's boundaries wobble between runs, so caching
by community id would call the LLM afresh every time.

```
   qmd · BM25 · Hermes                    →  하이브리드 검색 시스템
   Obsidian · Vault · LLM Wiki            →  LLM 기반 개인 지식베이스
   Agent-3 · OpenBrain · Agent-4          →  에이전트 능력 증강과 정렬
   dokploy · Traefik · Cloudflare         →  애플리케이션 배포 스택
   Hermes Agent · Pattern B · Implicit…   →  에이전트 루프 종료 제어
   ARPA · M4 · 삼쩜삼                       →  법률세무 에이전트 마켓플레이스
   pm2 · yarn build · 환경변수              →  PM2 기반 Next.js 배포
   BlackHole · Whisper · Multi-Output…    →  macOS 시스템 오디오 캡처
```

The labels come out in the vault's own language.  These are real output from a Korean vault
(`~/.kal/comm_label_cache.jsonl`), left untranslated so they stay what the tool actually
produced rather than what it would have been nice to show.

**The entity list is not thrown away.**  It stays as a subtitle under the name — with the LLM's
name alone there is no way to check "what is actually in it", and no way to notice when the
label is wrong.  A failed LLM call falls back to the list of names automatically.

The prompt says explicitly "do not simply list the entity names" and "no abstract flourishes".
Without that, you get useless titles like 'a journey through knowledge'.

### Spatial cohesion — putting a community together on screen too

Same colour but scattered positions does not make "viewing by community" true as a picture.
`forceCommunity` (galaxyForces.ts) finds each community's centre of mass every tick and pulls towards it.

Why fixed coordinates are not assigned in advance — many relations cross communities, and
forcing them apart turns those edges into a tangle across the screen.  The centre-of-mass
approach **tightens slightly** the layout the links have already made, so the two cooperate.

The strength was set by measurement (300 ticks to full settling · kNN purity K=10, a scale-free metric):

```
   strength 0     0.643    the link forces alone already clump somewhat
   strength 0.06  0.657    too weak to notice
   strength 0.15  0.686    ← adopted
   strength 0.30  0.714    tighter, but the inter-community edges become a tangle
```

⚠ The first metric was 'mean intra-community distance / overall radius', and it said cohesion
made things **worse**.  Cohesion contracts the whole layout together (radius 322→277), so the
denominator shrank with it.  Only switching to the scale-free kNN purity got the sign right.

⚠ **kNN purity underestimates this change too.**  A random layout would be 0.054, and without
cohesion it is already 0.643 — Louvain groups 'what is heavily connected' and the link forces
pull 'what is connected', so both read the same signal.  That is, the nearest 10 were mostly
in the same community even before cohesion, and what cohesion actually changed is **the
distance between clumps**.  Tangled communities separating into distinct clumps barely registers
in a local metric and is instantly visible to the eye.  That is what the gap between +0.043 and
the on-screen impression is —— the metric is not wrong, it is **measuring something else**.

**Turned off in type mode.**  Turned on, 1,293 `concept` nodes clump into one lump, which is meaningless.

### hover — light up that community alone and show its summary

Two places trigger it.  Both leave only that community with `renderer.setFocus()`, settle the
rest, and show the topic name, summary and representative entities in the box above FILTER.

```
   mouse on a legend row      ControlPanel  →  cb.onGroupHover(name)
   mouse on a graph node      pointermove   →  hoverGroupAt(px,py,w,h) → hoverGroup(g)
                                        lights up **the whole community** that node belongs to
```

The state (`hoveredGroup`) is used in one place only.  Kept separately, going from B in the
legend to A in the graph would look like "already A" and the update would be skipped.

Moving the mouse away **returns to the selected state** — so sweeping the legend with a node
selected does not clear the selection.  While something is selected, graph hover does not
intervene at all (selection highlighting wins).

### The summary box has a fixed height

`height: 78px` + the summary clamped to 2 lines.  Growing and shrinking with the content keeps
pushing the list below it, so sweeping the mouse makes the screen judder (user report: "stuttery").

When empty it shows **the overall summary**.

```
   20 communities
   6,386 entities · the remaining 1,370 sit in 'other'
   Hover a row to light up that community alone
```

Left empty, the 78px is dead space; defaulting to the first community's description makes it
**look selected**, turning a state that does not exist into one that does.  The overall summary
is true, fills the space, and misleads nobody.

### Colour — the data brings it

The community colours are decided by `COMM_COLORS` in `export_kal_graph.py` and the plugin uses
them as-is (`palette.setExplicitColors`).  Why the plugin's own colour wheel must not be trusted:

```
   assignFolderHues's HUES has only 9 slots.  HUES[i % 9]
   21 communities  →  communities 0, 9 and 18 share a colour  →  indistinguishable in the galaxy
```

The collision already met with 14 folders, recurring threefold with communities
(see the existing comment in [`palette.ts`](../plugin/src/render/palette.ts)).

The colours come from **a golden-angle (137.5°) walk + a 3-step alternation of lightness and
saturation**.  A hand-picked palette produced a measured ΔE 6.7 pair (`#5fd39b` vs `#4ad6a8` — effectively the same colour).

```
   20 hand-picked colours    minimum ΔE  6.7    ✗
   20 evenly around the wheel  minimum ΔE 11.5
   20 golden-angle colours     minimum ΔE 15.6 · 20.2 among the top 6 communities   ✓ adopted
```

Communities are ordered by descending size, so ids 0, 1, 2… dominate the screen.  The golden
angle spreads that front section as widely around the wheel as possible, so **the large
communities separate best** (ΔE 20 is the boundary of distinguishability).

**How to see it** — the `by type / by community` segment above FILTER in the graph view.  Turn
it on and the filter list and the node colours both switch to communities.  Leave one community and only that topic shows.

**Switching resets the filter** — the group name space is entirely different.  Go to community
mode with `concept` turned off and that name exists nowhere, leaving a ghost filter
([`GraphStore.setKalGroupBy`](../plugin/src/data/GraphStore.ts)).

⚠ **Communities can differ on every rebuild.**  Louvain is an approximation algorithm, pinned
with `seed=7`, but a changed graph (documents added, entities merged) moves the boundaries and
the numbers.  A community id must not be stored anywhere and reused — it is recomputed every time it is viewed.

---

## Search

### Through the skill

Triggered by natural language in Claude Code — "find it in my notes", "what did I write about
this before", "how did we handle the terraform state file".  The definition is
[`kal-search-skill.md`](kal-search-skill.md), installed at `~/.claude/skills/kal-search/`.

### Through the CLI

```bash
#  ⚠ Run it from the repository root —— `cd src` and there is no `.venv/bin/python`
.venv/bin/python src/kal_search.py "a query" --snippets --json
```

| Option | Meaning |
|---|---|
| (none) | the measured best weights · top 20 |
| `--origin vault` / `session` | curated notes only / session documents only |
| `--top N` | how many (default 20) |
| `--min-degree N` | leave thinly connected entities and relations out of the graph component |
| `--snippets` | include the supporting fragments |
| `--mode graph` | only right after the KG was re-extracted |

### What is mixed, and how

```
   BM25            chunks.text FTS(ngram 2,3)     an exact term, Korean included
   chunk vector    chunks.vector cosine           paraphrases and near wording
   entity vector   lr_entities.vector             names of concepts, tools and people
   relation vector lr_relations.vector            "how does A act on B"

   min-max normalised, then a weighted sum —— **the weights are in Step 6 above**
```

Normalisation is essential — BM25 runs 12–42 and cosine 0.83–0.86.  Added raw, BM25 is 49×
larger and crushes the vectors.

A relation-vector hit pulls in not only the documents attached to that relation but **the
documents its two endpoint entities appear in** (the 1-hop expansion).  Turning it off costs
nDCG the amount recorded in Step 6 — this is the path by which graph search actually works.

### Reading the results

```
   📓 vault    curated by the user directly.     high confidence
   💬 session  a Claude conversation distilled by an LLM.   decisions and reasons survive, nuance is lost
```

A session document is a distillation, so **the guesses and errors of that moment are mixed in.**
Cite it with the `captured` date stated, and when the exact source is needed, find
`~/.claude/projects/**/<id>.jsonl` through the frontmatter's `session_id`.

### Interrogating why a score is what it is

That is what the `ix_*` tables are for.  Not on the search path, but tf, df and idf can be read directly in SQL.

```python
import lancedb
db = lancedb.connect("~/.kal/db")
db.open_table("ix_terms").search().where("term = '역색인'").to_list()
db.open_table("ix_postings").search().where("term_id = 12345").limit(20).to_list()
```

---

## The three caches — at a glance

| Cache | Key | Invalidated by | Cost without it |
|---|---|---|---|
| `distilled/.done/` | `session_id` | `--restart` | 54 minutes |
| `lr_cache.jsonl` | `(document, chunk number, body hash)` | a change of chunk size or prompt | 3 hours |
| `lr_summary_cache.jsonl` | `sha1(name + the sorted fragments)` | a change in the fragment set | 16 minutes |

All three record **only what succeeded.**  Caching a failure freezes a transient error
permanently — 41 sessions were really lost that way once.

---

## Known limits

- Cache invalidation is **per chunk**.  Change the front of a document and the later chunks'
  boundaries shift, re-extracting everything after it.
- `lr_summary_cache` keys on the fragment list, so one changed fragment rewrites the whole profile.
- The knowledge graph is not refreshed automatically by `sync_v3.py`.  Only a `stale_docs` record is left.
- Session search quality is effectively unverified.  The gold set judges vault documents only,
  and the known-item evaluation failed through vocabulary leakage
  ([`HYBRID_METHODOLOGY.md`](HYBRID_METHODOLOGY.md) L3 and L4).

---

## See also

- The design in detail — [`KNOWLEDGE_DB_DESIGN.md`](KNOWLEDGE_DB_DESIGN.md)
- The evaluation methodology — [`HYBRID_METHODOLOGY.md`](HYBRID_METHODOLOGY.md)
- All the measured numbers — [`../README.md`](../README.md)
- LightRAG's summary implementation — `operate.py:369` · thresholds `constants.py:30-36` · the prompt `prompt.py:295` — [LightRAG](https://github.com/HKUDS/LightRAG) source, retrieved 2026-08-17
