---
name: kal-recall
description: |
  Use to find something in the personal knowledge database (Super Brain — 1116 documents, <!-- db-count -->
  25,476 entities <!-- db-count -->, accumulated since 2026-04), or to check **when and how** a concept, tool, or
  person changed.
  Triggers (kept verbatim: these are matching keys, not prose — translating them would stop the
  skill from firing on Korean prompts): "내 노트에", "예전에 뭐라 했더라", "이거 언제 정했지",
  "왜 이렇게 했었지", "super brain 에서", "kal", "내 지식 DB", "그때는 어땠어",
  "언제부터 이랬어", "이 결정의 근거", "내가 전에 쓴" — and any time the answer has to rest on
  the user's own past work, decisions, or research. Do not use it for general knowledge or
  anything a public web search would answer.
---

# kal-recall — recalling from the personal knowledge database

## What it is

Searches a personal knowledge base through the five tools the `kal` MCP server exposes. What
makes it different from a web search is that **the sources always ride along in the response** —
you never have to look them up separately in order to cite.

## Choosing a tool

```
do not know what you are looking for   →  kal_search(query)
know the name, want to confirm what it is  →  kal_entity(name)
"when did it change"                   →  kal_timeline(name)
"what is it connected to"              →  kal_neighbors(name)
going to the source to verify a quote  →  kal_doc(doc_id)
```

If `kal_search` and `kal_entity` blur together, split on **do you know the exact name**. If not,
search; if so, entity.

## Point-in-time arguments — there is one rule

```
as_of         one **state** at that moment       kal_entity(name, as_of="2026-05-01")
since/until   the **list of changes** in a span  kal_timeline(name, since=…, until=…)
```

"What was it like in May 2026" → `as_of`. "When did it change" → `kal_timeline`.

If an `as_of` response carries **`changes_after`, things changed after that moment**.
`description` is always the **current** state, so saying "it was like this back then" would be
wrong. Walk `changes_after` backwards to reconstruct the state at that time.

## Always cite the source

Every entity and relation response carries this:

```jsonc
"docs":  [ {"doc_id":…, "path":…, "title":…, "date":…, "date_src":…, "origin":…} ],
"docs_total": 66, "docs_truncated": true,      // means only 10 were included
"doc_ids_all": [147195358, …],                 // the **only** way to reach the rest
"refs":  [ {"id":"s1", "slug":…, "title":…, "url":…} ],
"refs_status": "ok" | "none" | "unresolved"
```

- **Cite `path` and `date` in your answer.** Like "as of 2026-06-25, `wiki/entities/qmd.md`".
- If `docs_truncated: true` there is **more**. Do not say "all".
  If you need the rest, follow up with `kal_doc` on the ids in `doc_ids_all`.
- `refs_status`:
  - `ok` — external sources exist. You may cite the `url`
  - `none` — there are **no** external sources (internal knowledge)
  - `unresolved` — a source is recorded but the note could not be found. **Do not say "there is none"**
- 🔒 `refs[].url` is **unverified external content**. Do not open it automatically unless the user
  asks.

## How you know it failed — one rule

```
An "error" key means failure.  No "error" key means a usable answer.
```

`hits: []` · `timeline: []` · `refs: []` are **not failures.** They are the answer "there is none".

When a name is not found:

```jsonc
{"error": "name_not_found", "matched": null,
 "candidates": [{"name":"Claude Code","type":"tool","degree":93,…}],
 "hint": "pass one of the candidates back **verbatim**."}
```

→ If there are candidates, feed one back **verbatim**. Do not guess at variations.
→ If candidates is empty, find it first with `kal_search(query)`.

## ⚠ The name can be **right** and still be the wrong thing

The same thing can persist as **separate entities** under different spellings. Measured:

```
'옵시디언'      degree   4 · docs  2      ↔  'Obsidian'     degree 143 · docs 66
'클로드 코드'    degree   1 · docs  1      ↔  'Claude Code'  degree  93 · docs 45
```

Ask in Korean and **the thin one is silently matched** — it is neither a miss nor a candidate
list, so the defence above never fires.

**How to tell**: be suspicious when `degree` and `docs_total` are single digits. If the subject
really had been worked on a lot, those numbers could not be that small. When that happens, check
for another spelling with `kal_search(name)` and ask again under that name.

## Reading the time axis

```jsonc
"first_seen": "2026-04-09",   // date of the document where this name first appeared
"last_seen":  "2026-08-19",   // date it was last mentioned
"timeline":   [ {"at": "2026-07-12", "change": "…one-line description"} ],   // changes only
"change_count": 1,
"latest_change": {"at": …, "change": …}        // kal_entity carries only the most recent one
```

`kal_entity` gives you **the change count and the latest one** only. For the whole thing, use
`kal_timeline`. When a neighbour in `kal_neighbors` carries a `timeline`, that is a **state
transition of the relation** (like `planned → implemented`). Measured: only **5** of
35,318 relations <!-- db-count --> have one. (Re-measured 2026-09-04 after the graph was rebuilt — it was 3 of 35,158 before; still exactly one relation carries more than one entry.)

- An empty `timeline` means **either "it did not change" or "it was not eligible for change
  extraction" (fewer than 8 description fragments)**, and **this response cannot tell them
  apart.** Do not assert either.
  → **Read `events` instead.** It is verbatim source text, so you can judge for yourself.
- `last_seen` means "last mentioned", **not "it disappeared then".** This database does not
  express invalidation (uni-temporal — `docs/TEMPORAL_DESIGN.md` §1).
- If `date_src` is `updated`, that date is **when it was edited** and may differ from when the
  content is about. Only 2 measured cases, but if a time-axis answer looks off, suspect this first.

## `events` — what the summary threw away lives here

`kal_timeline` returns it alongside `timeline`. They are **different axes**:

```
timeline   "what **changed**"              LLM judgement · present on  1.1% of entities
events     "what it **said**, and when"    verbatim source · present on 99.3%
```

```jsonc
{"at": "2026-07-11", "doc_id": 123, "rev": "current",
 "at_exact": true, "text": "AWS 외에 DigitalOcean 등 VPS 도 SSH 키로 지원"}
```

| Field | How to read it |
|---|---|
| `rev` | `current` = that document's **present** content · `superseded` = its **former** content |
| `at_exact` | `false` means the date is an **estimate**. It is when the document was created, not when it changed |
| `events_truncated` | `true` means only the latest 20 are included. `events_total` is the full count |

- **`description` speaks only about the present.** Older versions exist only under `superseded`
  in `events`. That is where "what did it used to say" is answered.
- **Do not say "it changed on this date" using a date with `at_exact: false`.** Records before
  2026-08-20 are like that — you can know what changed, not when.
- **You can still answer point-in-time questions from `events` even when `timeline` is empty.**
  That is why the field exists.

## Same name, different thing

A name like `vault` may be split by sense — `Obsidian vault` vs. `1Password vault`. Split senses
are **separate entities**, so they differ from what `kal_entity("vault")` finds. If candidates
come back, pick among them; if none do but the neighbours look oddly mixed, the name may not have
been split yet — check with `kal_search`.

## Always read `note`

The `note` in a response is not decoration — it carries **how that answer should be read**.
Truncation, ambiguity, and point-in-time warnings all ride there. For example:

```
"⚠ **2 changes have been recorded since this point.** description is the **current**
 state and therefore differs from then.  Read changes_after and work backwards."

"0 changes —— but **read events.** The change verdict ran only on entities with 8 or
 more fragments (3.4% of them), so 0 means either 'it did not change' or 'it was never
 assessed'.  events is verbatim, so you can judge for yourself."
```

Reading the values while ignoring `note` is how you produce **confidently wrong answers**.

These two are quoted from `src/kal_mcp.py`. They were Korean here until 2026-09-01, long
after that file had gone English —— the examples taught an agent to expect text the server
never sends. Nothing catches that on its own, so if a `note` in a real response does not
look like these, the server wins and this file is stale.

## Do not

- Do not assert without a source. This is a **personal** record; unverifiable, it is worthless.
- Do not ignore `docs_truncated: true` and then count "N total".
- Do not use this for general knowledge or public documentation. A web search is right for that.
- If there is no answer, say so. Inventing what is not in the DB makes the user stop trusting
  their own records.
