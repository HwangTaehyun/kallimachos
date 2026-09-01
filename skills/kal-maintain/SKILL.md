---
name: kal-maintain
description: |
  Use when you need to **check whether the personal knowledge database (Super Brain) is stale and
  refresh it**. This is pipeline operation, not search — is the index behind, what needs to run,
  how long will it take.
  Triggers (kept verbatim: these are matching keys, not prose — translating them would stop the
  skill from firing on Korean prompts): "지식 DB 갱신", "kal 상태", "색인 다시", "재색인",
  "노트가 검색에 안 나와", "방금 쓴 글이 안 잡혀", "KG 낡았어", "파이프라인 돌려",
  "second brain 갱신", "vault 반영해줘". Search and recall are not this skill — kal-recall does that.
---

# kal-maintain — refreshing the knowledge database

## What this skill is not

**If the job is to find something, it is `kal-recall`.** This one covers only "what do I run when
the database has drifted from reality". Confusing the two means running a 35-minute rebuild when
the user just wanted an answer.

There is one boundary:

```
"find it / what did I say about this"   →  kal-recall
"it is not showing up / refresh / status" →  here
```

## Look at status first — the tool tells you what to run

```bash
docker run --rm -v <KAL_DIR>:/data -v <VAULT>:/vault:ro \
  ghcr.io/hwangtaehyun/kal:0.1.0 src/status.py
```

> ⚠ That image is **not on the public registry yet.** Build it from the repository first:
> `just build-kal && docker tag kal:local ghcr.io/hwangtaehyun/kal:0.1.0`
> Otherwise you get `manifest unknown`.

If you cloned the repository, `just status` does the same thing.

The **"what to run next"** section of the output already ranks the options. 🔴 means now, ⚪ means
it can wait. **Follow that recommendation; do not pick a bigger stage on your own.**

## Stages — they are ordered, and they cost differently

```
1 distil sessions   ~/.claude conversation logs → distilled documents   20 min · needs LLM
2 promote to vault  move the distilled copy into the vault and commit    1 min
3 extract KG        entities and relations per chunk, via LLM           30 min · needs LLM
4 rebuild knowledge DB  chunk, embed, index                              3 min · overwrites the DB
5 export graph      viewer artifacts                                     2 min

partial  incremental sync  only changed docs: chunks, vectors, inverted index
                                                                         1 min · **cannot fix the KG**
```

| Symptom | What to run | Cost |
|---|---|---|
| A note I just wrote does not show up in search | `src/sync_v3.py` (incremental sync) | 1 min |
| Search works but entities/relations show old content | `src/refresh_kg.py` (refresh stale KG) | 35 min · LLM |
| I changed configuration (chunk size, etc.) | `src/schema_v3.py` (rebuild knowledge DB) | 3 min |
| The graph view shows old entities | `src/export_all.sh` | 2 min |

## Ask, then run

**Anything that overwrites the DB or takes more than five minutes needs the user's confirmation.**
The "Cost" column above is the basis for that judgement. `refresh_kg` in particular is 35 minutes
and costs LLM calls.

The LLM stages (1 and 3) need `claude` CLI authentication. Containers do not have it, so run them
on the host or start the relay — if that comes up, point at `just relay`.

## How you know it failed

```
"has no index yet — the index has not been run"  →  never indexed. Start at src/schema_v3.py.
"the DB is already in use — <holder>"            →  one at a time. Wait.  (CLI)
"something else is writing the DB — <holder>"    →  the same thing over HTTP 409.  (web)
"needs the claude CLI and it is unavailable"     →  HTTP 412. LLM stages cannot run.
                                                    Use the host, or the relay.
```

These are quoted from three other files — `src/kal_mcp.py`, `src/kal_lock.py` and
`api/main.go` — so they drift the moment one of them is reworded. They already did once:
until 2026-09-01 this table listed the Korean sentences those files used to print, and the
code had gone English, leaving an agent matching on text nothing emits. Nothing fails
loudly when that happens; the guidance just stops arriving. If a message here does not
match its source, the source wins.

## Do not

- Do not jump to a rebuild without looking at status. `status` will tell you when a one-minute
  job is enough.
- Do not default to "full rebuild" (60 min). It is almost always overkill.
- Do not edit vault files directly. This skill brings **the DB in line with the vault**, not the
  other way round.
