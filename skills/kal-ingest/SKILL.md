---
name: kal-ingest
description: |
  Use when **putting outside writing into the personal knowledge database (Super Brain)** —
  Slack, Notion, Google Drive, web documents. Explains what format is needed, and what belongs in
  the front matter for the time axis and the egress block to work.
  Triggers (kept verbatim: these are matching keys, not prose — translating them would stop the
  skill from firing on Korean prompts): "이거 vault 에 넣어줘", "노트로 저장", "지식 DB 에 추가",
  "slack 대화 넣어", "notion 페이지 가져와", "이 문서 색인해줘", "kal 에 넣어",
  "second brain 에 담아". Finding what is already in there is kal-recall.
---

# kal-ingest — outside writing into the knowledge database

## The format is free — but four keys buy you something

**Anything under `vault/**/*.md` gets indexed.** Neither the openwiki format nor front matter is
required (measured: a Slack thread with zero front-matter lines came back first in semantic
search).

Leaving them out only costs you things:

| Front-matter key | Without it |
|---|---|
| `title` | the **filename** becomes the title |
| `created` (or `captured` · `generated_at` · `updated`) | it **drops out of the time axis** — `as_of` and timeline will not see it |
| `tags` | that word will not find it |
| `no_llm: true` | **there is no way to turn it off** — the extraction stage sends that text to the LLM |

So the minimum form is this:

```yaml
---
title: Retro on tuning search weights
created: 2026-08-01          # ★ ISO required
tags: [search, retrieval]
no_llm: true                 # ★ mandatory for private channels and pages
---
(the original text as-is — no conversion needed)
```

## The date format is strict

```
2026-08-01              ✓
"2026-08-01"            ✓
2026-08-01T14:02:00Z    ✓   ← passes exactly as Notion and Drive give it
July 2, 2026            ✗   ← Notion's UI display format
2026/08/01              ✗
1721030400 (epoch)      ✗   ← Slack. Must be converted
```

It must **start** with `YYYY-MM-DD`. Slack's epoch `ts` has to be converted.

## One document = one point on the time axis

For a `timeline` (what changed when) to appear, **the same subject has to appear in several
documents with different dates** (measured: entities that have a timeline carry a median of 4
distinct dates, minimum 2).

So **what you treat as one document is your time resolution**:

```
Slack    split by channel × day       → the time axis comes for free
Notion   pages get overwritten        → you have to accumulate snapshots:
Drive                                   imported/<title>@2026-08-01.md
                                        imported/<title>@2026-08-23.md
                                        a different path is a different document = a different date
```

Snapshots cost duplicated content, but vectors are reused when the content hash matches, so the
embedding cost barely moves — only disk grows.

## Procedure — **writing the file alone does nothing**

Put a file in the vault and the DB does not know it exists. Indexing is **two stages**, and doing
only the first leaves it half-findable.

```
1  write the file                                → DB does not know
        │
2  incremental sync   sync_v3.py     1 min        → chunks · vectors · inverted index
        │              chunks · ix_* · stale_docs    **no entities or relations**
        │
3  refresh stale KG   refresh_kg.py  35 min · LLM → entities and relations too
```

**Stopping at stage 2 uses 23% of the retrieval signal.** The default weights are
`BM25 0.18 · chunk 0.05 · entity 0.18 · relation 0.59` (`src/kal_search.py:108`), so with no
entities or relations **0.77 of it is missing.**

    search with the exact word         →  it comes back (BM25 answers)
    "what was there about this idea"   →  **it does not** (that query is answered by relation vectors)

So the procedure is:

1. Write `.md` under `<VAULT>/imported/<source>/`. The folder structure is up to you.
2. Add the four front-matter keys above. **If it is private, do not omit `no_llm: true`.**
3. Run the **incremental sync** (1 min) — `kal-maintain` knows how.
4. **Tell the user you stopped at stage 2.** Ask whether to run the 35-minute KG refresh now.
   Do not run it without asking — it costs LLM calls.
5. Verify — search for what you just added with `kal-recall`. If it is not there, stage 3 was
   never run.

If you are adding several items, run 3 and 4 **once, after writing them all**. Per-item runs
multiply the 35 minutes by the number of items.

## What is excluded automatically

If any of these appear in the path, it is neither indexed nor sent (**always**, cannot be turned
off):

```
anywhere         /.git/  /.claude/  /.obsidian/  /node_modules/  /.omc/  /.ouroboros/
vault root       kg/                      ← output of export_graph --obsidian
auto-detected    a copy of this repository ← found by a marker, not by name
```

**To exclude an arbitrary folder, use `KAL_SKIP`.** Vault-relative paths joined by colons:

```bash
KAL_SKIP=imported/slack-dm:private
```

It is the same value as **excluded folders for indexing and egress** in the web UI's
"Chunks · Search" page, and it is stored in `~/.kal/config.json`. Folders listed there are neither
indexed nor sent through `claude -p`.

To block **a single document** rather than a folder, put `no_llm: true` in its front matter —
then it is indexed (and searchable) but never sent to the LLM.

## Do not

- Do not **convert** the original into the openwiki format. It is unnecessary, and conversion
  mangles the source.
- Do not **invent dates.** Leave it blank if you do not know — a wrong time axis is worse than
  none.
- Do not ingest a private conversation without `no_llm`. The extraction stage will send it
  through `claude -p`.
