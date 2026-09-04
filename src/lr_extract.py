#!/usr/bin/env python3
"""LightRAG-style entity and relation extraction — the LLM writes them fresh.

The earlier regex approach (scraping [[link]] lines) is gone.  That recycled sentences
the user had already written; it is not what LightRAG does.

What happens here (the LightRAG pipeline as-is):
  1. chunk each document into 1200-token pieces (the LightRAG default)
  2. feed every chunk to the LLM and extract entities / relationships as JSON
     - the schema is the same as entity_extraction_json_examples in LightRAG's prompt.py
     - entity:       name / type / description
     - relationship: source / target / keywords / description
  3. entities sharing a name have their descriptions merged (LightRAG's merge stage)

Running it: there are no API credits, so calls go through the claude CLI (subscription).
      If ANTHROPIC_API_KEY is set the CLI prefers it, so `env -u` strips it.
"""
import vault_path
import unicodedata
import collections, hashlib, json, os, re, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from entity_resolve import build_canon, name_stats, split_sense
from schema_v3 import doc_meta, effective_date

# slug -> document date.  The only ground for putting fragments in time order (docs/TEMPORAL_DESIGN.md §0).
# Parsing reuses schema_v3.doc_meta —— parsing in two places drifts apart eventually.
DOC_DATE = {}
from claude_cli import run as claude_cli_run


# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
# Where the vault lives.  Mounted at /vault inside the container (see docker-compose).
VAULT = vault_path.vault()
OUT = os.path.join(KAL_HOME, "lr_kg.json")
#  How far the entity count may fall in one run before the write refuses.  Extraction is not
#  incremental —— a run either sees the vault or it does not —— so anything approaching a halving
#  means the input is wrong, not that the notes changed.  `schema_v3.DB_SHRINK_RATIO` guards the
#  database the same way and for the same reason.
KG_SHRINK_RATIO = 0.67
CACHE = os.path.join(KAL_HOME, "lr_cache.jsonl")   # for resuming — appends the raw response per chunk
SUM_CACHE = os.path.join(KAL_HOME, "lr_summary_cache.jsonl")   # entity profile summary cache

# the same thresholds as LightRAG's _handle_entity_relation_summary
# (raw/lightrag/lightrag/constants.py:30-36)
FORCE_LLM_SUMMARY_ON_MERGE = 8        # once this many description fragments pile up, the LLM rewrites them
SUMMARY_MAX_CHARS = 900               # upper bound on the final profile length
MAP_REDUCE_CHARS = 14_000             # longer than this: summarise the pieces, then summarise the summaries

# Bump this **whenever** the prompt changes.  _sum_key hangs off this value, so without a
# bump the old cache keeps hitting and the new output (changes) never appears at all.
# t4 —— entity type system redesigned (see the ENTITY_TYPES comment above).  Invalidates every cache entry.
# t5 —— ran t4 over 144 chunks, read the sample, and narrowed two definitions (2026-08-21):
#   · artifact reached 24.1% and became **the new dumping ground** ("wiki content", "warning
#     callout", "ASCII art").  It was holding parts of documents, not files.
#   · metric swallowed version strings ("Electron v39.8.3").  A number in the string does not
#     make something a measurement —— nobody measured anything to obtain a version.
# Without that mid-run sample this would have surfaced only after burning the full 90 minutes.
#
# Running t5 over 121 chunks (1,826 entities) gave —— **the material for the next round**:
#   Improved    concept 45.6% → 14.1% · artifact 24.1% → 21.1% ·
#               metric's version-shaped hits 3/3 → 1/53 · failure's sample is nearly right
#               ("outline view flickering", "scroll position not updating")
#   Two boundary errors remain —— they break no stated criterion, so t5 left them alone:
#     · organisations leak into project.  "OpenAI", "RAND Forecasting Initiative" and
#       "Center for AI Policy" are actors.  Either narrow project towards **what we build**,
#       or reorder the list so actor is read first.
#     · metric still swallows **the subject of measurement**.  "research speed",
#       "algorithmic progress" and "AI-relevant compute" are things measured, not measured
#       values.  "no number or unit in the string means it is not a metric" is worth trying.
#   When fixing either, **bump PROMPT_VERSION** so the caches are invalidated.
PROMPT_VERSION = "t5"

SUM_TIME_SYS = """You are a knowledge-graph curator. You get dated fragments about ONE
entity (or relation), oldest first. Produce JSON with exactly two keys.

"profile": one coherent paragraph describing what the entity IS **now** (as of the
  latest fragment). Open with its full name. Integrate every distinct fact. Third
  person, objective. No "this document", "we", "it". If fragments describe DIFFERENT
  things sharing a name, say so and describe each.

"changes": a list of things that GENUINELY CHANGED over time. Each item is
  {"at": "YYYY-MM-DD", "change": "<what became true, one sentence>"}.
  - Use the date of the fragment where the new state first appears.
  - **Most entities have no changes. Return [] then.** Restating the same fact in
    different words is NOT a change. A later fragment being more detailed is NOT a
    change. Only include it when an earlier claim stopped being accurate, or a
    genuinely new role/value/state appeared.
  - Never invent a date that is not on a fragment.

"senses": present ONLY when the fragments describe **genuinely different things that
  happen to share this name** (e.g. "vault" = an Obsidian note store in some
  fragments, a 1Password card container in others). Each item:
    {"label": "<short qualifier>", "cue": ["<word>", "<word>", …]}
  - "label" qualifies the name so a reader can tell the senses apart —— e.g.
    "Obsidian", "1Password". Not a full sentence.
  - "cue" —— **words that actually appear in the fragments of that sense.**
    A reader will use them to decide which sense a new fragment belongs to,
    so pick words that are specific to one sense and absent from the others.
    3–6 per sense. Lowercase. Copy them from the fragments; do not invent.
  - Omit the key entirely (or return []) when the fragments are all about ONE
    thing —— **that is the normal case.** Different levels of detail, different
    languages, or a broader/narrower framing are NOT different senses.
  - Two or more senses only. Never one.

Output raw JSON only. No markdown fence, no commentary."""

SUM_SYS = """You are a knowledge-graph curator. Rewrite a list of fragmentary descriptions
of ONE entity (or relation) into a single coherent profile.

Rules:
1. Open with the full name of the entity/relation, then describe it.
2. Integrate every distinct fact. Drop repetition, not information.
3. Third person, objective. No "this document", "we", "it".
4. If the fragments clearly describe DIFFERENT things that share a name, say so and
   describe each separately. If they merely conflict, reconcile them or state both
   with the uncertainty noted.
5. Write in the language the fragments are mostly written in. Keep proper nouns and
   technical terms in their original form.
6. At most %d characters. Plain prose, no markdown, no preamble.

Output the profile text only.""" % SUMMARY_MAX_CHARS
CHUNK_CHARS = 2400          # ≈1200 tokens (Korean runs about 2 chars/token) = the LightRAG default
OVERLAP = 200
# Number of concurrent LLM calls.
#
# ⚠ **Lower this on long runs.**  Measured 2026-08-21: with 14 workers, immediately after a
#   99-minute extraction over 905 chunks, the summary stage that followed hit a 91% failure
#   rate (218 transport · 109 shape).  Every one was an "empty response", and calling
#   `claude -p` by hand at that same moment answered normally —— so this is not the model but
#   **a limit on accumulated concurrency**.  Re-running with `LR_WORKERS=4` passed with 0 failures.
#
#   Why the default is not 4: on short incremental runs 14 is simply faster, and a failure is
#   stopped by the gate anyway (which now also gives up early).  Only jobs running past an
#   hour, such as a full re-extraction, need it lowered.
WORKERS = int(os.environ.get("LR_WORKERS", "14"))


def effective_workers():
    """With a relay in front, never exceed its concurrency cap.  Past it, queue waits become timeouts.

    An **explicit** LR_WORKERS is honoured —— a person set that value on purpose.
    With no relay (a local run), or if it cannot be asked, WORKERS stands.
    """
    if os.environ.get("LR_WORKERS"):
        return WORKERS
    relay = os.environ.get("KAL_CLAUDE_RELAY", "").rstrip("/")
    if not relay:
        return WORKERS
    try:
        import json as _j
        import urllib.request as _u
        with _u.urlopen(relay + "/health", timeout=5) as r:
            cap = int(_j.load(r).get("inflight_max") or 0)
        return min(WORKERS, cap) if cap > 0 else WORKERS
    except Exception:
        return WORKERS
MODEL = "haiku"

# ⚠ Changing this list means changing **three places together**:
#     schema_v3.CANON_TYPES / TYPE_MAP   a type missing there folds into "other"
#     export_kal_graph.TYPE_COLOR        no colour means grey in the viewer
#   And PROMPT_VERSION must be bumped, or neither the extraction nor the summary cache invalidates
#   (extraction was wired up on 2026-08-21 —— before that only --restart did it, and that
#    wiped the cache, taking the document revision history with it).
#
# Why not the LightRAG defaults (measured 2026-08-21)
#   With the default 7, concept became the dumping ground at 45.6% (3,473/7,620).  Worse,
#   **the types distinguished nothing** —— median degree was 2 for every type alike, so
#   knowing a node's label predicted nothing about its role.
#
#   The cause is the corpus.  77.3% of this vault is Claude Code session logs (work records),
#   but the LightRAG default types are for general prose, where person/organization come first.
#   The stars of a work record —— **errors, files, numbers, decisions** —— had no slot, so they fell into concept.
#
# Why pattern was dropped
#   The LLM split method (technique) and pattern (recurring structure) almost evenly, 16.2% / 14.9%.
#   That is less a boundary than **a coin flip because the two could not be told apart**.  Keeping a
#   near-synonym pair works directly against the goal of making types predict structure.  TYPE_MAP
#   folds pattern in old data into method, so nothing breaks before a re-extraction.
#
# Why person + organization → actor
#   Together they are only 5.2%, yet **every personal-data exposure comes from here**.  One type
#   means one gate —— masking, the viewer filter and the pre-publication scan each become a single
#   condition.  Two conditions means one of them gets missed eventually.  There is no evidence in
#   this vault that splitting person/organization helps search (person median degree 1 —— half were isolated).
ENTITY_TYPES = [
    "artifact",   # files, paths, config keys, schema fields
    "tool",       # external software
    "project",    # a repository or product boundary
    "method",     # techniques and procedures (the old pattern folds in here)
    "failure",    # errors, bugs, failure modes
    "decision",   # a choice and its rationale
    "metric",     # measured values, benchmarks
    "event",      # something whose point is when it happened
    "actor",      # people, teams, organisations
    "concept",    # ★ last.  Only what fits none of the above
]

SYS = """You extract a knowledge graph from text. Output ONE valid JSON object only — no prose, no code fence.

Schema:
{
  "entities": [{"name": "...", "type": "...", "description": "..."}],
  "relationships": [{"source": "...", "target": "...", "keywords": "...", "description": "..."}]
}

Rules:
1. entity.type must be one of the types below. Read the list IN ORDER and take the
   FIRST type that fits. `concept` is the LAST RESORT — use it only when no other applies.

   artifact  a NAMED file, path, config key, schema field, table, or env var —
             something a program reads or writes, that you could point at on disk
             e.g. docker-compose.yml, KAL_PATH, lr_entities, .gitignore
             NOT the software that reads it — that is a tool.
             NOT a part of a document's prose or presentation. "wiki content",
             "warning callout", "ASCII art", "the introduction" are NOT artifacts;
             if it has no name you could type into a shell, it is not one.
   tool      software that is run or called
             e.g. Obsidian, PM2, ffmpeg, LanceDB
             NOT a technique — that is a method.
   project   a repository, product, or system with a boundary
             e.g. kallimachos, galaxy-view
             NOT one of its internal components.
   method    a technique, procedure, or recurring pattern
             e.g. mutation testing, HNSW, incremental indexing
             NOT one particular run of it — that is an event.
   failure   an error, bug, or failure mode
             e.g. ERR_CONNECTION_REFUSED, port collision, silent LLM failure
             The thing that went wrong — NOT the fix for it.
   decision  a choice that was made, where something else could have been chosen
             e.g. chose uni-temporal over bi-temporal
             NOT an option still under consideration — that is a concept.
   metric    a measured value or benchmark — the OUTCOME of measuring something
             e.g. 24.4s to 5.0s, recall@10, 99.3%% coverage, 501s to 25s
             The measurement itself — NOT the thing being measured.
             A VERSION NUMBER IS NOT A METRIC. "Electron v39.8.3",
             "Obsidian 1.12.7", "Python 3.13" are the software, at a version →
             type them as `tool` (or `artifact` if it is a file), never `metric`.
             Nothing was measured to obtain a version number.
   event     something that happened at a specific time
             e.g. the 2026-03-31 sourcemap leak
   actor     a person, team, or organization
   concept   an abstract idea that fits none of the above
             e.g. idempotency, provenance
   (types: %s)
2. entity.description: concise but comprehensive, based SOLELY on the input text.
3. Extract only direct, clearly stated, meaningful relationships between extracted entities.
4. If one statement relates more than two entities, decompose into multiple binary relationships.
5. relationship.keywords: high-level keywords summarizing the relationship, comma-separated.
6. relationship.description: a concise explanation of the NATURE of the relationship — why these
   two are connected, what the connection does. This is the most important field.
7. Only output relationships whose source and target are both in the entities list.
8. Write everything in the SAME LANGUAGE as the input text (Korean input -> Korean output).
   Keep proper nouns and technical terms in their original form.
9. Third person. No pronouns like "this document", "we", "it".
10. At most 20 entities and 25 relationships per response. Prioritize the most significant.""" % ", ".join(ENTITY_TYPES)


def chunks_of(text, path):
    out, i, idx = [], 0, 0
    while i < len(text):
        piece = text[i:i + CHUNK_CHARS]
        out.append({"doc": path, "idx": idx, "text": piece,
                    "h": hashlib.sha1(piece.encode()).hexdigest()[:12]})
        idx += 1
        i += CHUNK_CHARS - OVERLAP
    return out


# Filled by collect().  It records **which point in time of the vault** the extraction saw.
#
# Why —— refresh_kg launches lr_extract then schema_v3, minutes apart.
# Edit a note in that gap and schema_v3 **indexes the new body while carrying the old KG.**
# Nobody marks anything, so refresh_kg --check reports "clean".
# schema_v3 compares its own scan against these hashes and marks mismatched docs stale.
# (adversarial review, 2026-08-18)
DOC_HASHES: dict[str, str] = {}
NO_LLM_SKIPPED: list[str] = []   # documents excluded from transmission in this collect()

# ── The outbound boundary ──────────────────────────────────────────────
# schema_v3.SKIP is the list deciding "do we index this locally".  This file sends the
# surviving chunks **off the machine** via `claude -p` —— the two judgements are not the same.
# A doc can be fine to index and not fine to send; reusing SKIP means nobody makes that call.
#
# Today only two things newly leave here that SKIP does not cover —— CLAUDE.md and
# Clippings/Obsidian Changelog.md —— and neither is sensitive.  The problem is **later**.  Add
# a folder like Private/ to the vault and it is sent silently along with indexing.  So the
# outbound boundary lives here, separately.  (adversarial review 2026-08-18, security lens)
#
# Put a path fragment here and it drops out of extraction (indexing and search keep it).
#  ⚠ Priority matches the other settings: environment > ~/.kal/config.json > default.
#     Back when only `os.environ` was read, the host CLI does not read `.env`, so this value
#     was **ignored entirely** (it took effect only inside the container).
try:
    import kal_config as _kcfg
    _NO_LLM_RAW = str(_kcfg.values().get("no_llm_paths", "") or "")
except Exception:
    _NO_LLM_RAW = os.environ.get("KAL_NO_LLM", "")
from schema_v3 import _clean_pathspec   # ← above the use: NO_LLM is built at import time
#  Same cleaner as `schema_v3.SKIP_EXTRA` —— the two settings are the same shape and used to
#  share the same defect: `./Private`, `../Private`, `Private/*` and a pasted absolute path were
#  all accepted, displayed back, and matched nothing.  For a **transmission** gate that is the
#  worst kind of failure: the screen says the folder is excluded and it is not.
NO_LLM = _clean_pathspec(_NO_LLM_RAW)

# To turn it off for one document, put `no_llm: true` in its frontmatter.
from schema_v3 import NO_LLM_RE as NO_LLM_MARK   # one place —— see schema_v3


def blocked_path(rel):
    """Is `rel` (a vault-root-relative path) blocked by `NO_LLM`?

    Matched **as path components anchored at the root, not as a substring**.  With `x in f`,
    `private` also matches `my-private-notes/`, and prefixing a `/` matches nothing at all
    —— `schema_v3.is_skipped` was fixed for exactly this reason.

    This was inline inside `collect()` and was pulled out here: when a test copies the
    predicate, deleting the body still passes (a failure this repository actually suffered).
    The table in README §"What stays on your machine" holds the same values as this self-check.
    """
    #  ⚠ **Both sides are normalised, because the filesystem is.**  APFS (and NTFS) are
    #     case-**insensitive** and normalisation-**preserving**: `Private/` and `private/` are the
    #     same directory, and a name typed as NFC and one stored as NFD render identically in `ls`.
    #     A plain `==` therefore let the gate miss the very folder the user meant:
    #       KAL_NO_LLM=private  ·  vault/Private/med.md   →  not blocked  (reproduced 2026-09-04)
    #       KAL_NO_LLM=`비공개`(NFC) · vault/`비공개`(NFD)/a.md →  not blocked  (macOS stores NFD)
    #     The second matters here specifically —— this vault's folder names are Korean.
    #     Normalising is the safe direction for a transmission gate: it can only block more, and
    #     the component anchoring above still stops `private` from matching `my-private-notes`.
    def _norm(x):
        return unicodedata.normalize("NFC", x).casefold()
    parts = [_norm(c) for c in rel.replace(os.sep, "/").split("/")]
    return any(parts[:len(x)] == x for x in
               ([_norm(c) for c in b.split("/")] for b in NO_LLM))


def collect():
    import glob
    cs = []
    DOC_HASHES.clear()
    # ⚠ This used to walk only wiki/ and raw/.  But schema_v3.scan_vault() indexes the
    #   **whole** vault —— documents outside those two get indexed and show up in search
    #   while never entering the knowledge graph.  Worse, sync_v3 marks them stale and
    #   schema_v3 drops that mark, so they are **reported "clean" forever.**  Two measured
    #   cases: CLAUDE.md · Clippings/Obsidian Changelog.md.  (adversarial review 2026-08-18)
    #
    #   Same rule as indexing —— change one side only and they drift apart again.
    #   Deciding what to discard uses the same function as indexing too.  It used to measure
    #   120 chars of raw text while schema_v3 measures 60 chars of body with the frontmatter
    #   stripped —— so two documents that are almost entirely frontmatter
    #   (raw/conversations/sessions/untitled*.md) burned LLM calls while never being indexed.
    #
    #   ⚠ clean() decides **discard or not**, nothing else.  The body that goes into a chunk
    #   is the raw text —— the cache key is a hash of the chunk content, so changing the text
    #   fed in turns all 835 chunks into cache misses and calls the LLM again.  Nothing has
    #   measured frontmatter-stripping as better for extraction, so there is no reason to pay that.
    from schema_v3 import is_skipped as _index_skipped, clean as index_clean
    skipped_llm = NO_LLM_SKIPPED
    skipped_llm.clear()
    for f in sorted(glob.glob(f"{VAULT}/**/*.md", recursive=True)):
        if _index_skipped(f):
            continue
        t = open(f, encoding="utf-8", errors="ignore").read()
        body, _ = index_clean(t)
        if len(body) < 60:
            continue
        # The judgement happens in doc_meta and nowhere else.  Two separate regexes drift
        # apart eventually, and these actually had — on BOM, leading blank lines and CRLF.
        #  ⚠ Matched **as path components, not as a substring**.  With `x in f`,
        #     `KAL_NO_LLM=private` also matches `.../my-private-notes/`, and prefixing
        #     a `/` matches nothing at all —— that exact defect ——
        #     `schema_v3.is_skipped` was already fixed for the same reason.
        if blocked_path(os.path.relpath(f, VAULT)) or doc_meta(t)[2]:
            skipped_llm.append(os.path.relpath(f, VAULT))
            continue
        rel = os.path.relpath(f, VAULT).replace("/", "_")[:-3]
        _m = doc_meta(t)
        # Not created but the **effective date**.  What we read is the current content, so an
        # edited document counts as a claim made at edit time (schema_v3.effective_date).
        DOC_DATE[rel] = effective_date(_m[0], _m[3])     # "" drops it off the time axis
        # Must be the **same expression** as schema_v3.scan_vault() or the comparison is void
        DOC_HASHES[os.path.relpath(f, VAULT)] = hashlib.sha256(t.encode()).hexdigest()[:16]
        cs += chunks_of(t, rel)
    # Show what leaves the machine every time —— stops a new folder slipping in silently
    tops = collections.Counter(p.split("/")[0] if "/" in p else "(root)" for p in DOC_HASHES)
    print(f"  sending to the LLM: {len(DOC_HASHES)} docs · {len(cs)} chunks"
          + (f" · {len(skipped_llm)} excluded from transmission" if skipped_llm else ""))
    print("    " + " · ".join(f"{k} {v}" for k, v in tops.most_common()))
    for x in skipped_llm[:5]:
        print(f"    excluded: {x}")
    return cs


def call(chunk):
    prompt = (f"{SYS}\n\n---Input Text---\n{chunk['text']}\n\n"
              f"---Output---\nJSON only:")
    for attempt in range(3):
        try:
            # claude_cli.run —— the prompt goes on stdin (argv would expose it in the process
            # list), env is allowlisted, tools are blocked.  Rationale in claude_cli.py.
            s = claude_cli_run(MODEL, prompt, timeout=180)
            # ⚠ Never retry an empty response **without waiting**.
            #
            # claude_cli.run reports failure as an empty string —— rate limits and transient
            # errors all arrive here.  This used to `continue` straight into the next attempt,
            # burning all 3 in a few seconds and dropping the chunk as "failed".  Retrying a
            # rate limit immediately is exactly the wrong prescription.
            #
            # Measured (2026-08-19): 224 of 299 chunks failed this **fast** —— had they all
            # been timeouts it would have taken over 4 hours; it finished in 1131 seconds.
            if not s:
                if attempt < 2:
                    time.sleep(15 * (attempt + 1))    # 15s → 30s
                continue
            m = re.search(r"\{.*\}", s, re.S)
            if not m:
                continue
            d = json.loads(m.group(0))
            # ⚠ Check the schema.  This used to be `d.get("entities", [])` —— a response with
            #   **no entities key at all**, such as `{}` or a CLI error payload, was cached as
            #   "success with 0 entities", and since the cache is keyed on the chunk hash that
            #   document stayed entity-less forever.  It did not show in the failed-chunk count
            #   either.  An empty array is legitimate (some chunks have no entities) —— only a
            #   missing key counts as failure.  (adversarial review 2026-08-18, BLOCKER)
            if not isinstance(d.get("entities"), list) or not isinstance(d.get("relationships"), list):
                continue
            return {"doc": chunk["doc"], "idx": chunk["idx"], "h": chunk["h"],
                    # Which prompt produced this.  Part of the reuse key —— edit the prompt and
                    # the same chunk is re-extracted, while the old line stays on as history.
                    "pv": PROMPT_VERSION,
                    # Which model produced this.  Part of `cache_key` —— without it, changing
                    # the model still reuses the old result.  (2026-08-25)
                    "model": MODEL,
                    # ★ Extraction time.  The cache is append-only, so the same (doc, idx) piles
                    #   up over several lines (measured: 812 chunks, 771 of them with **changed**
                    #   content), and with no timestamp there was no way to write "when did it
                    #   change".  This one field opens the transaction-time axis.
                    "at": time.strftime("%Y-%m-%d"),
                    "entities": d["entities"],
                    "relationships": d["relationships"]}
        except Exception:
            time.sleep(2 * (attempt + 1))
    return {"doc": chunk["doc"], "idx": chunk["idx"], "h": chunk["h"],
            "pv": PROMPT_VERSION, "model": MODEL,
            "entities": [], "relationships": [], "failed": True}


def group_nodes(results, history=()):
    """Per-chunk extraction results → **the node set**.  (formerly named `merge`)

    Why the rename —— it now works in two directions **at once**:

        joins   folds spelling variants into a canon      aliases.yml
        splits  divides one name by sense                 homonyms.yml   ← newly added

    `merge` tells only half of it.  The job is "decide which node each fragment goes into",
    and joining is just the special case of that.

    ★ **This is the only place a node comes into being.**  The moment `ents[k] = {...}` runs
    the node set is fixed, and nothing after it (summary, indexing) can split them —— the
    fragments have fused into one blob and which fragment meant what is unrecoverable.

    The merge key is entity_resolve.merge_key — it absorbs spelling variants (case, hyphen, space).
    The exclusion rules need **group context** (they must know whether only one side is
    path-shaped), so names are collected into a canon first, then the real grouping runs.

    `history` —— chunks from **older revisions** no longer in the vault.  When a document is
    edited the old line stays in the cache, and that is the event "it used to read like this".
    It **does not** enter the `description` summary (content already known to be wrong must
    not mix into the current account) —— it is carried only in `events`.

    Pure code.  No LLM (measured under 1 second).  Criteria: docs/ENTITY_RESOLVE.md
    """
    raw_e = [e for r in results for e in r["entities"]]
    raw_r = [x for r in results for x in r["relationships"]]
    names = {(e.get("name") or "").strip().lower() for e in raw_e}
    names |= {(x.get(k) or "").strip().lower() for x in raw_r for k in ("source", "target")}
    names.discard("")
    stats = name_stats(raw_e, raw_r, docs_of=lambda e: ())
    canon, ngroups = build_canon(sorted(names), stats)
    if ngroups:
        print(f"name merge — {ngroups} groups (spelling variants absorbed)", flush=True)

    ents, rels = {}, {}
    for r in results:
        for e in r["entities"]:
            n = (e.get("name") or "").strip()
            if not n:
                continue
            d = (e.get("description") or "").strip()
            # ★ Homonyms are split **here**.  This line is where a fragment's node is chosen,
            #   and it is the **only** moment where the fragment's description (d) and its
            #   source (r["doc"]) are both still in hand.  Later the fragments have fused.
            #   Rules live in homonyms.yml (human-confirmed).  With no rule, n comes through as is.
            n = split_sense(n, d, r["doc"])
            k = canon.get(n.lower(), n.lower())
            if k not in ents:
                ents[k] = {"name": n, "type": e.get("type", "concept"),
                           "descriptions": [], "frags": [], "docs": set()}
            if d and d not in ents[k]["descriptions"]:
                ents[k]["descriptions"].append(d)
                # Keep the pair together.  Collecting descriptions and docs separately makes
                # "which document gave this description" unrecoverable —— the original defect.
                ents[k]["frags"].append((r["doc"], d))
            ents[k]["docs"].add(r["doc"])
        for x in r["relationships"]:
            s, t = (x.get("source") or "").strip(), (x.get("target") or "").strip()
            if not s or not t:
                continue
            # Split the relation endpoints too —— otherwise the entity splits while the relation
            # still points at the old name, a **dangling reference** (the 217 the index drops).
            xd = (x.get("description") or "").strip()
            s = split_sense(s, xd, r["doc"])
            t = split_sense(t, xd, r["doc"])
            ks = canon.get(s.lower(), s.lower())
            kt = canon.get(t.lower(), t.lower())
            if ks == kt:
                continue            # a relation the merge turned into a self-loop
            k = tuple(sorted([ks, kt]))
            if k not in rels:
                rels[k] = {"source": s, "target": t, "keywords": set(),
                           "descriptions": [], "frags": [], "docs": set()}
            d = (x.get("description") or "").strip()
            if d and d not in rels[k]["descriptions"]:
                rels[k]["descriptions"].append(d)
                rels[k]["frags"].append((r["doc"], d))
            for w in (x.get("keywords") or "").split(","):
                if w.strip():
                    rels[k]["keywords"].add(w.strip())
            rels[k]["docs"].add(r["doc"])
    # ── Attach older revisions **as events only** ──
    # Not into descriptions —— content already known wrong must not mix into the current account.
    # Not into frags either —— shifting the summarise-or-not test (8+ fragments) shifts the cost.
    for r in history:
        for e in (r.get("entities") or []):
            n = (e.get("name") or "").strip()
            d = (e.get("description") or "").strip()
            if not n or not d:
                continue
            n = split_sense(n, d, r["doc"])
            k = canon.get(n.lower(), n.lower())
            if k in ents:                       # only for entities that are still alive
                ents[k].setdefault("hist", []).append((r["doc"], d, r.get("at", "")))

    for v in ents.values():
        v["docs"] = sorted(v["docs"]); v["description"] = " ".join(v["descriptions"])[:1200]
        _stamp(v)
    for v in rels.values():
        v["docs"] = sorted(v["docs"]); v["keywords"] = sorted(v["keywords"])
        v["description"] = " ".join(v["descriptions"])[:1200]
        _stamp(v)
    return list(ents.values()), list(rels.values())


def _events(v):
    """Keep the fragments **as they are** —— a dated list of events.

    Why a summary alone is not enough
      A summary folds 37 fragments into 900 characters, and **information disappears** in
      the process —— individual numbers, moments and context blur into a "synthesis".
      timeline rescues the "what changed" part, but that exists for only 0.85% of entities
      as measured.  For the other 99.15% one summary paragraph is everything.

      Keeping the raw fragments lets the consuming LLM judge **for itself**.  It need not
      trust our summariser's verdict on "what counts as a change" —— the reproducibility of
      that verdict has never been measured.

    The cost is negligible: 14,979 fragments measured = 0.87MB, 1.1× the summaries (0.81MB).
    """
    out, seen = [], set()
    # Current revision —— rev 2 (what that chunk holds now)
    for doc, text in (v.get("frags") or []):
        d = DOC_DATE.get(doc, "")
        if d and (doc, text) not in seen:
            seen.add((doc, text))
            out.append({"at": d, "doc": doc, "text": text, "rev": "current"})
    # Older revisions —— how the same document used to read
    for doc, text, at in (v.get("hist") or []):
        if (doc, text) in seen:
            continue                      # identical content is a duplicate, not a revision
        seen.add((doc, text))
        # Date priority: the cache's extraction time > the document's creation date.
        # When the cache carries an extraction time it is exact.  Backfilled rows have no
        # `at` (caches from before 2026-08-20) and fall back to the document date, which
        # lands on the **same date** as the current revision, so only rev separates them.
        # There is no way to backfill it either —— of 772 measured rows, doc_updated can
        # separate just 5.  An honest limitation, so "this date is not the date it changed"
        # is carried in the response itself.
        out.append({"at": at or DOC_DATE.get(doc, ""), "doc": doc, "text": text,
                    "rev": "superseded",
                    "at_exact": bool(at)})
    out = [e for e in out if e["at"]]
    out.sort(key=lambda e: (e["at"], e["rev"] == "current"))
    return out


def _stamp(v):
    """Compute first_seen · last_seen from the dates, **deterministically**.

    Why this sits outside the LLM path —— the lower bound of as_of must survive a failed
    summary (docs/TEMPORAL_DESIGN.md §2.5).  Fragments from undated documents cannot be
    ordered, so they drop off the time axis while their descriptions are still used.
    """
    ds = sorted({DOC_DATE.get(doc, "") for doc, _ in v.get("frags", ())} - {""})
    v["first_seen"] = ds[0] if ds else ""
    v["last_seen"] = ds[-1] if ds else ""


# ────────────────────── Entity profile summary ──────────────────────
# Why it is needed: descriptions are written **per chunk**.  An entity appearing in 20
# chunks yields 20 fragmented viewpoints, and " ".join on those is unreadable prose.
# LightRAG calls the LLM once more here and rewrites them into a single profile.
# (raw/lightrag/lightrag/operate.py:369 _handle_entity_relation_summary)

def _sum_key(name, descs, dated=()):
    """The cache key.  **The prompt version goes into it.**

    The old key was only (name, fragments), so editing the prompt still hit the cache every
    time.  Adding the time axis changed the output shape, and with an identical key the new
    output never appears.  Dates go in too —— the same fragments in a different order change `changes`.
    """
    #  `MODEL` goes in as well —— same reason as `cache_key`.  Without it, switching to a
    #  stronger model still reuses the profile haiku wrote.  (deep review, 2026-08-25)
    parts = [PROMPT_VERSION, MODEL, name] + sorted(descs) + sorted(d for d, _ in dated)
    return hashlib.sha1("\u0000".join(parts).encode()).hexdigest()[:16]


def summarize_one(name, descs):
    """Fragment descriptions → one profile.  Too long, and it folds via map-reduce."""
    _fallback(False)                       # this function owns the lifetime of the flag
    cur = list(dict.fromkeys(descs))                 # drop exact duplicates
    while True:
        total = sum(len(d) for d in cur)
        if total <= MAP_REDUCE_CHARS or len(cur) <= 2:
            body = "\n".join(f"- {d}" for d in cur)
            out = call_text(f"{SUM_SYS}\n\nName: {name}\n\nFragments:\n{body}\n\nProfile:")
            if not out:
                _fallback(True)        # stitched fragments —— must not be cached
            return (out or " ".join(cur))[:SUMMARY_MAX_CHARS]
        # Too long: split into groups, summarise each, then try again (a loop, not recursion)
        groups, buf, n = [], [], 0
        for d in cur:
            if n + len(d) > MAP_REDUCE_CHARS and buf:
                groups.append(buf); buf, n = [], 0
            buf.append(d); n += len(d)
        if buf:
            groups.append(buf)
        if len(groups) >= len(cur):                  # cannot shrink further, so truncate
            return " ".join(cur)[:SUMMARY_MAX_CHARS]
        nxt = []
        for g in groups:
            body = "\n".join(f"- {d}" for d in g)
            o = call_text(f"{SUM_SYS}\n\nName: {name}\n\nFragments:\n{body}\n\nProfile:")
            nxt.append(o or " ".join(g))
        cur = nxt


# ok/fail count **transport** only —— did a response arrive.  `shape` counts whether that
# response was **usable in shape**.  Keep them together and "a response arrived but it is
# not JSON" tallies as ok, and the gate below passes.  That is exactly the shape of the
# 2026-08-20 incident that printed "305/305 · 0 failures" while emitting nothing but garbage.
# Was this summary a **fallback** —— flagged per thread.
#
# Why it is needed: `work()` wrote every result into the cache unconditionally, and the
# failure-rate gate runs **after** that.  So when run 1 failed at 91% and the gate blocked,
# 252 pieces of garbage were already in the cache.  Run 2 hits all of them and passes
# **without calling the LLM even once** (CALL_STATS all 0 → should_abort False) —— and the
# garbage goes straight into the graph.
#
# The same shape as the "305/305 · 0 failures" incident, one layer up.  The lower layer
# (counting) got fixed and the upper one (the cache) was missed.  (2026-08-21)
_FALLBACK = threading.local()


_ABORTED = threading.Event()


def abort_early():
    """On a fatal failure rate, **give up the remaining calls**.  The final gate still judges.

    Why —— the gate runs only after `ex.map` finishes entirely.  Measured (2026-08-21), with
    91% failing it stopped only **after making all 361 calls, retries included**.  Three
    hundred more confirmations of something twenty would have shown.

    Stopping early on a small sample kills healthy runs, so this fires only at **20 or more
    calls and 50% or worse** —— far more lenient than the final gate (10%).  This is not a
    quality verdict but **a guard against wasted time**.  The verdict is still the gate's.
    """
    if _ABORTED.is_set():
        return True
    n = CALL_STATS["ok"] + CALL_STATS["fail"]
    bad = CALL_STATS["fail"] + CALL_STATS["shape"]
    if n >= 20 and bad / n >= 0.50:
        if not _ABORTED.is_set():
            _ABORTED.set()
            print(f"  ⛔ failure rate {bad}/{n} ({bad/n*100:.0f}%) —— giving up the rest. "
                  f"The final verdict is below.", flush=True)
        return True
    return False


def cache_summary(fh, lock, k, prof, changes, senses):
    """Write a summary into the cache —— **not if it was a fallback.**  True when written.

    This judgement lived inside `work()` and was pulled out here.  Inside, there is no way
    to test it, and the first version tested only the flag, never this judgement itself.
    What it guards is exactly that incident —— cache a fallback and the next run hits every
    entry, never calls the LLM, and the gate passes because it has nothing to look at.
    """
    if _fallback():
        return False
    with lock:
        fh.write(json.dumps({"k": k, "s": {"profile": prof, "changes": changes,
                                           "senses": senses}},
                            ensure_ascii=False) + "\n")
        fh.flush()
    return True


def _fallback(v=None):
    """Given v, set the flag; given nothing, read the current value."""
    if v is None:
        return getattr(_FALLBACK, "hit", False)
    _FALLBACK.hit = v
    return v


def cache_key(chunk):
    """The reuse key for the extraction cache.

    ⚠ **The prompt version goes into it.**  Leave it out and editing the prompt reuses the
    old results, changing nothing —— and then the only remedy is `--restart`, which
    **deletes** the cache file, taking the superseded revisions with it.  That means the
    document revision history vanishes from events entirely (772 rows measured).

    It sits at module level so it can be tested —— as an inner lambda the self-check had no
    choice but to grep the source text, and that assertion **found itself** and therefore
    always passed (2026-08-21).
    """
    #  ⚠ `MODEL` is part of the key too.  Without it, switching to a stronger model reuses
    #     what haiku produced and **nothing changes** —— the KG carries 0.77 of the default
    #     ranking, so a model upgrade is nullified wholesale.  (deep review 2026-08-25, domain)
    return (chunk["doc"], chunk["idx"], chunk["h"], PROMPT_VERSION, MODEL)


def should_abort(stats):
    """Is the failure rate above 10% —— better to die than to survive and emit garbage.

    **Transport failures and shape failures are counted together.**  The causes differ but
    the outcome is the same: the summary degrades to stitched fragments.  Watch transport
    alone and a healthy relay with a babbling model passes —— the 2026-08-20 incident's shape.
    """
    n = stats["ok"] + stats["fail"]
    bad = stats["fail"] + stats["shape"]
    return bool(n) and bad / n >= 0.10


CALL_STATS = {"ok": 0, "fail": 0, "shape": 0}


def call_text(prompt, tries=3):
    """**Counts** failures.  The old version swallowed exceptions whole, so with the relay
    unreachable all 305 calls quietly degraded to stitched fragments while the log printed
    only "305/305 done".  The output was garbage and nobody knew.
    """
    last = ""
    for i in range(tries):
        try:
            out = claude_cli_run(MODEL, prompt, timeout=240)
            if out:
                CALL_STATS["ok"] += 1
                return re.sub(r"\s+", " ", out)
            last = "empty response"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(2 * (i + 1))
    CALL_STATS["fail"] += 1
    if CALL_STATS["fail"] in (1, 5, 20):        # only the first few —— do not bury the log
        print(f"  ⚠ LLM call failure #{CALL_STATS['fail']} — {last}", flush=True)
    return ""


def summarize_timed(name, dated, descs):
    """(profile, changes).  Dated fragments are handed over in time order.

    On failure changes is emptied and it drops to the existing path —— the profile must come
    out even when the time axis does not.  first_seen/last_seen do not depend on this (_stamp).
    """
    _fallback(False)                       # so the caller has nothing to remember
    body = "\n".join(f"[{d}] {t}" for d, t in dated)
    if len(body) > MAP_REDUCE_CHARS * 2:            # too long: take the existing folding path
        # Not a failure but **a designed path**.  summarize_one calls the LLM properly, so
        # no fallback flag is raised here (if it fails there, it raises the flag there).
        return summarize_one(name, descs), [], []
    out = call_text(f"{SUM_TIME_SYS}\n\nName: {name}\n\nFragments (oldest first):\n{body}\n\nJSON:")
    try:
        j = json.loads(re.sub(r"^```(?:json)?|```$", "", (out or "").strip(), flags=re.M).strip())
        prof = (j.get("profile") or "").strip()
        senses = [{"label": (x.get("label") or "").strip(),
                   "cue": [str(c).lower().strip() for c in (x.get("cue") or []) if str(c).strip()]}
                  for x in (j.get("senses") or [])
                  if (x.get("label") or "").strip() and (x.get("cue") or [])]
        # A single sense is not polysemy.  The prompt forbids it; the code blocks it too.
        senses = senses[:4] if len(senses) >= 2 else []
        seen = {d for d, _ in dated}
        ch = [{"at": c.get("at", ""), "change": (c.get("change") or "").strip()}
              for c in (j.get("changes") or [])
              # A date not present in the fragments is invented.  Drop it.
              if c.get("at") in seen and (c.get("change") or "").strip()]
        if prof:
            # The **most recent** 12.  The older end is dropped (same way as schema_v3._fold_tl).
            return prof[:SUMMARY_MAX_CHARS], ch[-12:], senses
        why = "profile came back empty"
    except Exception as e:
        why = f"{type(e).__name__}: {e}"
    # Reaching here means a response arrived in **an unusable shape**.  This used to be an
    # `except: pass` that degraded silently, and CALL_STATS then counted ok and the gate held.
    #
    # ⚠ An empty `out` means **no response arrived**, and `call_text` has already counted
    #   that as `fail`.  Bumping shape unconditionally counts the same call twice ——
    #   measured: summarising one entity with the relay down gives n=2 but bad=3, and the
    #   gate printed "150%".  It errs safe, since the gate fires **earlier**, but a threshold
    #   written as 10% then is not 10%.  A safeguard that does not behave as written deceives
    #   whoever tunes it next.  (raised by r2-verify, 2026-08-21)
    if out:
        CALL_STATS["shape"] += 1
        if CALL_STATS["shape"] in (1, 5, 20):
            print(f"  ⚠ LLM response shape failure #{CALL_STATS['shape']} — {why}", flush=True)
    prof = summarize_one(name, descs)      # the flag is reset inside this call
    _fallback(True)                        # which is why the flag is raised **after** it
    return prof, [], []


def summarize_all(ents, rels, workers):
    """Rewrite only entities with FORCE_LLM_SUMMARY_ON_MERGE or more description fragments.
    The rest (97% of them) have 1-7 fragments, with nothing to synthesise."""
    cache = {}
    if os.path.exists(SUM_CACHE):
        for line in open(SUM_CACHE, encoding="utf-8"):
            try:
                r = json.loads(line); cache[r["k"]] = r["s"]
            except Exception:
                pass

    def dated_of(v):
        """(date, description) in time order.  Undated fragments drop out —— unorderable."""
        out = [(DOC_DATE.get(doc, ""), t) for doc, t in v.get("frags", ())]
        return sorted([x for x in out if x[0]])

    jobs = []
    for v in ents:
        if len(v["descriptions"]) >= FORCE_LLM_SUMMARY_ON_MERGE:
            jobs.append((v, v["name"], v["descriptions"], dated_of(v)))
    for v in rels:
        if len(v["descriptions"]) >= FORCE_LLM_SUMMARY_ON_MERGE:
            jobs.append((v, f'{v["source"]} — {v["target"]}', v["descriptions"], dated_of(v)))
    if not jobs:
        return 0, 0

    hit = 0
    todo = []
    for v, name, descs, dated in jobs:
        k = _sum_key(name, descs, dated)
        c = cache.get(k)
        if c is not None:
            # Old cache entries are strings, new ones are {profile, changes}.  Both are read.
            if isinstance(c, dict):
                v["description"] = c.get("profile", ""); v["timeline"] = c.get("changes") or []
                v["senses"] = c.get("senses") or []
            else:
                v["description"] = c
            hit += 1
        else:
            todo.append((v, name, descs, dated, k))
    print(f"profile summary — {len(jobs)} targets (cached {hit} · new {len(todo)})", flush=True)

    if todo:
        lock = threading.Lock()
        fh = open(SUM_CACHE, "a", encoding="utf-8")
        done = [0]

        def work(item):
            v, name, descs, dated, k = item
            if abort_early():
                return                     # already gave up —— make no further calls
            if len(dated) >= 2:
                s, ch, sn = summarize_timed(name, dated, descs)
            else:
                s, ch, sn = summarize_one(name, descs), [], []
            v["description"] = s
            v["timeline"] = ch
            # A polysemy candidate.  **Never split automatically** —— homonym_suggest.py shows
            # it to a person, and the person confirms it into homonyms.yml.
            v["senses"] = sn
            cache_summary(fh, lock, k, s, ch, sn)
            with lock:
                done[0] += 1
                if done[0] % 20 == 0 or done[0] == len(todo):
                    print(f"  {done[0]}/{len(todo)}", flush=True)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(work, todo))
        fh.close()

        n = CALL_STATS["ok"] + CALL_STATS["fail"]
        bad = CALL_STATS["fail"] + CALL_STATS["shape"]
        if should_abort(CALL_STATS):
            # Better to die than to survive and emit garbage.  Fallback output is stitched
            # fragments, which still reads like a summary, so it is noticed far too late.
            raise SystemExit(
                f"❌ LLM failure rate {bad}/{n} "
                f"(transport {CALL_STATS['fail']} · shape {CALL_STATS['shape']})"
                f" ({bad/n*100:.0f}%) —— summaries degraded to stitched fragments.\n"
                f"   That garbage is in the cache ({SUM_CACHE}); delete it and run again.\n"
                f"   KAL_CLAUDE_RELAY is for the container.  Running on the host, point it "
                f"at 127.0.0.1 or leave it empty.")
        if bad:
            print(f"  ⚠ LLM failures {bad}/{n} "
                  f"(transport {CALL_STATS['fail']} · shape {CALL_STATS['shape']})"
                  f" —— that many are fallback output", flush=True)
    return len(jobs), len(todo)


def scope_gap(idx, ext, deliberate):
    """(indexed but not extracted, extracted but not indexed).

    **Why it is a separate function**: `check_scope` walks the whole vault and reads the
    cache, so tests can hardly call it.  Nobody was watching this arithmetic —— replace it
    with `missing = set()` and `rebuild_all.sh` still prints ✅ and carries on (r4-guard
    mutation).  That brings back "documents indexed but never entering the knowledge graph",
    documents whose stale mark is dropped too, so they are **reported clean forever**.
    Precisely the state this function exists to prevent.
    """
    return idx - ext - deliberate, ext - idx


def check_scope():
    """Check that the extraction scope == the indexing scope.  Exit code 1 on a mismatch.

    Why it exists —— the two used to use different globs.  That produced documents that were
    indexed but could never enter the knowledge graph (2 of them, including CLAUDE.md), and
    their stale mark was dropped too, so they were **reported "clean" forever**.  This is the
    kind of defect that returns quietly, so the check reads the filesystem only (no DB).
    """
    from schema_v3 import scan_vault
    idx = {d["path"].replace("/", "_")[:-3] for d in scan_vault().values()}
    ext = {c["doc"] for c in collect()}
    # What NO_LLM excluded **on purpose** is not a mismatch.  Counting that as an error
    # halts rebuild_all.sh the moment anyone excludes a single document from transmission.
    deliberate = {p.replace("/", "_")[:-3] for p in NO_LLM_SKIPPED}
    missing, extra = scope_gap(idx, ext, deliberate)
    if not missing and not extra:
        note = f" · {len(deliberate)} excluded from transmission" if deliberate else ""
        print(f"  ✅ extraction scope == indexing scope ({len(ext)} documents{note})")
        for x in sorted(deliberate)[:5]:
            print(f"     deliberately excluded: {x}")
        return 0
    print(f"  ❌ mismatch — indexed {len(idx)} · extracted {len(ext)} · deliberate {len(deliberate)}")
    for x in sorted(missing)[:10]:
        print(f"     indexed but not extracted (not deliberate): {x}")
    for x in sorted(extra)[:10]:
        print(f"     extracted but not indexed: {x}")
    return 1


def _main_extract():
    if "--check-scope" in sys.argv:
        raise SystemExit(check_scope())
    import threading
    restart = "--restart" in sys.argv
    args = [x for x in sys.argv[1:] if not x.startswith("--")]
    cs = collect()
    limit = int(args[0]) if args else len(cs)
    cs = cs[:limit]

    # Resume — a per-chunk cache.  Restarting a 2-hour run from scratch is not affordable.
    # Keyed on (document, chunk index).  When a document's content changes, --restart clears it.
    cached, results = {}, []
    if restart and os.path.exists(CACHE):
        os.remove(CACHE)
    if os.path.exists(CACHE):
        for line in open(CACHE, encoding="utf-8"):
            try:
                r = json.loads(line)
                # Old cache lines have no h → not trusted, re-extracted
                if "h" in r:
                    # Lines with a different prompt version are **not reused** (see key below).
                    # They still go into the dict —— the history calculation below reads them.
                    #  ⚠ **This must have the same shape as `cache_key()`.**  It stored a
                    #     4-tuple while lookups used a 5-tuple —— tuples of different length
                    #     are never equal, so **the hit rate was 0**.
                    #     Measured (2026-08-28): 0 hits out of 905 chunks; the cache's 2,773
                    #     lines and 17.4MB were dead weight.  An extraction meant to be
                    #     incremental called the LLM 905 times a run —— 89 minutes by the log.
                    #     I added MODEL to `cache_key` (929b109) and never fixed this side,
                    #     and the self-check passed because it only looked for `MODEL in _k`.
                    #     **There was no round-trip (write then read) test.**
                    #
                    #  ⚠ Old lines carry no `model`.  `MODEL` is the single hardcoded constant
                    #     `"haiku"` with no environment override, so those lines are certainly
                    #     haiku output too.  Hence they are filled with the current value ——
                    #     invalidating here would burn 89 minutes of the user's quota for no
                    #     information.  On the day models actually diverge the lines will carry
                    #     `model`, and invalidation will work normally.
                    cached[(r["doc"], r["idx"], r["h"],
                            r.get("pv", ""), r.get("model", MODEL))] = r
            except Exception:
                pass
    # ⚠ The prompt version is **part of the key.**  Editing the prompt must re-extract even
    #   an unchanged chunk, and the only route used to be `--restart` —— which **deletes** the
    #   cache file, taking the superseded revisions with it.  That means the document
    #   revision history vanishes from events entirely (813 rows measured).  The cache is
    #   append-only, so re-extracting while keeping the old lines is possible, and correct.
    key = cache_key
    todo = [c for c in cs if key(c) not in cached]
    results = [cached[key(c)] for c in cs if key(c) in cached]
    #  estimate.py learns "seconds per LLM call" from this count.
    try:
        import run_log; run_log.count(len(todo))
    except Exception:
        pass


    # ── Hand older revisions over **as history** ──
    # A cache line whose hash differs from the chunk now in the vault = that chunk used to read so.
    # These used to be skipped unread —— 771 of them were disappearing that way, measured.
    # Not the current revision, so they **stay out of** the description summary; only events.
    # ⚠ Here the key is **without the prompt version**, just (doc, idx, h).  A version bump
    #   means we edited the prompt, not that the document changed, so it is not revision history.
    #   Compare with pv in the key and every chunk masquerades as an "older revision" on each
    #   prompt bump, doubling the size of events.
    live = {(c["doc"], c["idx"], c["h"]) for c in cs}
    same_chunk = {(c["doc"], c["idx"]) for c in cs}
    seen_hist = set()
    history = []
    for r in cached.values():
        k3 = (r["doc"], r["idx"], r["h"])
        if k3 in live or (r["doc"], r["idx"]) not in same_chunk or k3 in seen_hist:
            continue
        seen_hist.add(k3)        # the same old revision across several versions counts once
        history.append(r)
    if history:
        print(f"  {len(history)} older revisions —— carried as events only (never in the summary)",
              flush=True)
    if cached:
        print(f"resume — reusing {len(results)} cached chunks, {len(todo)} left", flush=True)

    # Match the worker count **to the relay's concurrency cap.**
    #
    # Why —— the relay takes a slot with `with _sem:` **before** calling claude.  With 8 slots
    # and 14 workers, 6 wait in the queue, and that wait counts fully against the client's
    # HTTP timeout (180+30 seconds).  Past it an empty string comes back, and lr_extract
    # counts that as a "failed chunk".
    #
    # Measured (2026-08-19): 14 workers over 8 slots for 905 chunks gave **299 failures (38%)**
    # and entities fell 8,237 → 5,755 (−30%).  The exit code was 0 all the same.
    workers = effective_workers()
    if workers != WORKERS:
        print(f"  workers {WORKERS} → {workers} (matched to the relay cap — the excess times out in the queue)",
              flush=True)
    print(f"{len(cs)} chunks ({CHUNK_CHARS} chars · overlap {OVERLAP}) · {workers} workers · model {MODEL}", flush=True)
    t0 = time.time()
    done = 0
    lock = threading.Lock()
    # Running isolated with KAL_HOME pointed somewhere new (experiments, comparisons), the
    # folder does not exist.  Create it here —— otherwise it computes every chunk, then dies.
    os.makedirs(KAL_HOME, exist_ok=True)
    fh = open(CACHE, "a", encoding="utf-8")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(call, c): c for c in todo}
        for f in as_completed(futs):
            c = futs[f]
            r = f.result()
            results.append(r)
            if not r.get("failed"):          # failures are not cached — a re-run must retry them
                with lock:                   # a successful chunk is written out at once
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n"); fh.flush()
            done += 1
            if done % 25 == 0 or done == len(todo):
                el = time.time() - t0
                print(f"  {done}/{len(todo)}  {el/60:.1f} min  "
                      f"({el/done*(len(todo)-done)/60:.0f} min left)", flush=True)
    fh.close()
    ents, rels = group_nodes(results, history=history)
    if "--no-summary" not in sys.argv:
        n_all, n_new = summarize_all(ents, rels, workers)
    for v in ents + rels:            # the fragment list lives in the cache, not the output
        # Fragments are **promoted to events** and carried.  They used to be dropped here.
        v["events"] = _events(v)
        v.setdefault("senses", [])     # polysemy candidates; empty list when there are none
        v.pop("descriptions", None)
        v.pop("frags", None)
        v.pop("hist", None)
    fail = sum(1 for r in results if r.get("failed"))

    #  ⚠ **Do not replace a populated graph with a much smaller one.**  This single `json.dump`
    #     is the only writer of `lr_kg.json`, and it had no guard in any language —— so an empty
    #     or mis-set vault produced `0 entities · 0 relations · 0s` and overwrote the output of an
    #     eight-hour extraction.  That is not hypothetical: it happened on 2026-09-04, through the
    #     web UI, and recovery was only possible because `lr_cache.jsonl` survived.
    #
    #     The Go handler's empty-vault refusal does not cover this.  It gates `extract`, but the
    #     **combo** steps (`refresh_kg`, `apply_aliases`, `rebuild_all`) run their own command and
    #     never pass through it, and `just extract` on the host never touches Go at all.  Same
    #     lesson as `schema_v3`'s shrink guard: the check belongs where the write happens.
    #     (codex adversarial review 2026-09-04, finding #1)
    _prior = 0
    if os.path.exists(OUT):
        try:
            _prior = len(json.load(open(OUT, encoding="utf-8")).get("entities") or [])
        except Exception as e:
            #  Not knowing is not permission —— the same rule as the DB guard.
            raise SystemExit(f"❌ cannot read the existing {OUT} to compare, so refusing to "
                             f"overwrite it: {e}\n   Move it aside if it is corrupt.")
    if _prior and len(ents) < _prior * KG_SHRINK_RATIO and "--allow-shrink" not in sys.argv:
        raise SystemExit(
            f"❌ this run produced {len(ents):,} entities but {OUT} holds {_prior:,} —— "
            f"writing it would throw away {_prior - len(ents):,}.\n"
            f"   VAULT={VAULT}  ({len(cs):,} chunk(s) this run)\n"
            f"   If the vault really shrank, pass --allow-shrink.  If it looks empty, check "
            f"KAL_VAULT before anything else —— that is what this has always been.")

    json.dump({"entities": ents, "relationships": rels,
               "chunks": len(cs), "failed": fail,
               # The vault state this extraction saw.  schema_v3 checks it against its own scan.
               "doc_hashes": dict(DOC_HASHES),
               "extracted_at": int(time.time())},
              open(OUT, "w"), ensure_ascii=False, indent=1)
    print(f"\n{len(ents)} entities · {len(rels)} relations · {fail} failed chunks · {time.time()-t0:.0f}s")

    # ⚠ Failures are **reported through the exit code.**
    #
    #   This used to exit 0 even on failure.  So in the 2026-08-18 run, all 18 freshly called
    #   chunks **failed** because of `--permission-mode plan`, refresh_kg still finished as a
    #   success, and the KG looked fine thanks to 875 cached chunks.  Emptying out quietly is
    #   the worst outcome —— those documents show up in search with no entities at all.
    #
    #   Why the threshold is not "zero": LLM responses occasionally break format, and those
    #   retry naturally on the next run as cache misses.  But **every freshly called chunk
    #   failing** means the environment is broken (flags, auth, network).
    # A threshold of "everything failed" alone lets **partial failure pass quietly.**
    # Measured (2026-08-19): 299 of 783 chunks (38%) failed on timeout while the exit code was
    # 0, and lr_kg.json was overwritten from 8,237 entities down to 5,755 in the process.  The
    # UI printed "success".  A silently 30%-smaller graph flows into the index.
    FAIL_RATIO = 0.10        # LLM format errors live below this (retried automatically next run)
    if fail and todo and fail >= len(todo) * FAIL_RATIO:
        pct = fail * 100 // len(todo)
        raise SystemExit(
            f"\n❌ {fail} of the {len(todo)} freshly called chunks failed ({pct}%).\n"
            f"   The graph was saved that much smaller —— run this step again **before\n"
            f"   running the index**.  Failures are not cached, so it resumes.\n"
            f"   Likely causes:\n"
            f"     · relay concurrency cap < worker count → queue waits become timeouts\n"
            f"     · python claude_cli.py   (self-check: response · tool blocking)")
    if fail:
        print(f"  ⚠ {fail} will be retried on the next run (failures are not cached)")
    print(f"→ {OUT}")


def _selftest():
    """Are the guards against the LLM **surviving and emitting garbage** still alive?

    On 2026-08-20 a run printed "305/305 · 0 failures · 530s" while emitting nothing but
    fallback output.  Transport succeeded, so it counted as ok, and a response that was not
    JSON got swallowed by `except: pass`.  Those two guards are protected here.

    This module calls the LLM, so the self-check must run **without any call** —— call_text
    is swapped out for the test.
    """
    import collections as _c
    global call_text
    _orig = call_text

    def _run(reply):
        CALL_STATS.update(ok=0, fail=0, shape=0)
        globals()["call_text"] = lambda *a, **k: reply
        try:
            return summarize_timed("test", [("2026-01-01", "fragment")], ["fragment"])
        finally:
            globals()["call_text"] = _orig

    # ① a non-JSON response counts as a **shape failure** (this used to degrade silently)
    _run("Sorry, I cannot produce a summary.")
    assert CALL_STATS["shape"] == 1, f"a prose response is not counted: {dict(CALL_STATS)}"
    # ② the same when profile comes back empty
    _run('{"profile": "", "changes": [], "senses": []}')
    assert CALL_STATS["shape"] == 1, "an empty profile is not counted"
    # ③ a good response is not counted (false positives make the gate spin)
    prof, ch, sn = _run('{"profile": "a proper summary", "changes": [], "senses": []}')
    assert CALL_STATS["shape"] == 0 and prof == "a proper summary", "counts a good one as failed"

    #  The relay-is-down path —— where double counting actually happened.  **No response**
    #  has already been counted as fail by `call_text`, so shape must not be bumped again.
    #  ⚠ Mind the names —— there is already a **helper function** called `_run` above.
    #    Reusing that name at first overwrote the helper and the self-check blew up oddly.
    _sleep_orig, _cli_orig = time.sleep, globals()["claude_cli_run"]
    try:
        time.sleep = lambda *a: None
        globals()["claude_cli_run"] = lambda *a, **k: ""      # relay down
        CALL_STATS.update(ok=0, fail=0, shape=0)
        summarize_timed("X", [("2026-01-01", "fragment")], ["fragment"])
        assert CALL_STATS["shape"] == 0, \
            f"no response, yet counted as a shape failure too (double count): {dict(CALL_STATS)}"
        _n2 = CALL_STATS["ok"] + CALL_STATS["fail"]
        assert CALL_STATS["fail"] + CALL_STATS["shape"] <= _n2, "double count on the relay-down path"
    finally:
        time.sleep, globals()["claude_cli_run"] = _sleep_orig, _cli_orig
        CALL_STATS.update(ok=0, fail=0, shape=0)

    # ④ the gate looks at **both** transport and shape —— by calling the real function.
    #    (The old version recomputed the formula inside the test.  A test that never calls the
    #     code does not break when the code does —— it passed with the gate on transport alone.)
    assert not should_abort({"ok": 100, "fail": 0, "shape": 0}), "aborts on a healthy run"
    assert not should_abort({"ok": 95, "fail": 0, "shape": 5}), "aborts at 5%"
    assert should_abort({"ok": 90, "fail": 0, "shape": 10}), "misses 10% shape failures"
    assert should_abort({"ok": 90, "fail": 10, "shape": 0}), "misses 10% transport failures"
    assert not should_abort({"ok": 0, "fail": 0, "shape": 0}), "aborts on zero calls"

    # ⑤ the prompt version is **actually in** the cache key —— checked by calling the function.
    #    (The old version grepped the source for a string, and that string sat inside **the
    #     assertion itself**, so it passed no matter what was changed.)
    _c = {"doc": "a.md", "idx": 0, "h": "HH"}
    _k = cache_key(_c)
    assert PROMPT_VERSION in _k, \
        "PROMPT_VERSION is missing from the extraction cache key —— prompt edits never re-extract"
    #  The model is part of the key too —— without it a stronger model reuses the old results
    assert MODEL in _k, \
        "MODEL is missing from the extraction cache key —— model changes never re-extract"
    #  Same for the summary cache.  It is a hash, so check **that the key changes with the model**.
    _g = globals()
    _m0 = _g["MODEL"]
    try:
        _s1 = _sum_key("n", ["d"])
        _g["MODEL"] = _m0 + "-x"
        _s2 = _sum_key("n", ["d"])
    finally:
        _g["MODEL"] = _m0
    assert _s1 != _s2, "the summary cache key ignores MODEL —— a model change reuses old profiles"

    #  A cache **round trip**: can a line just written be found again by its key?
    #
    #  ⚠ The absence of this left a 17.4MB cache entirely dead.  Adding MODEL to `cache_key`
    #     (929b109) never fixed the loading side —— a 4-tuple against a 5-tuple, which can
    #     **never be equal** —— and measured (2026-08-28) that meant 0 hits out of 905
    #     chunks.  An extraction meant to be incremental called the LLM 905 times a run (89
    #     minutes by the log).  The self-check then only looked for `MODEL in _k`.  It saw
    #     the key's **shape**; nobody checked it could **find** anything.  Assert round trips.
    _c = {"doc": "d.md", "idx": 0, "h": "abc123"}
    _row = {"doc": "d.md", "idx": 0, "h": "abc123",
            "pv": PROMPT_VERSION, "model": MODEL, "entities": [], "relationships": []}
    _loaded = {(_row["doc"], _row["idx"], _row["h"],
                _row.get("pv", ""), _row.get("model", MODEL)): _row}
    assert cache_key(_c) in _loaded, (
        f"a written line cannot be found by its key —— the whole cache is dead.\n"
        f"  key={cache_key(_c)}\n  loaded={list(_loaded)[0]}")
    #  Old lines (no `model`) must be found too —— MODEL is a single hardcoded constant, so
    #  those lines certainly came from the same model.  Invalidating them here burns 89
    #  minutes of the user's quota for no information at all.
    _old = {k: v for k, v in _loaded.items()}
    _legacy = dict(_row); _legacy.pop("model")
    _old2 = {(_legacy["doc"], _legacy["idx"], _legacy["h"],
              _legacy.get("pv", ""), _legacy.get("model", MODEL)): _legacy}
    assert cache_key(_c) in _old2, "old lines without `model` are missed —— everything re-extracts"
    #  A **genuinely different** model must not be found (the reason the guard exists)
    _diff = dict(_row); _diff["model"] = MODEL + "-other"
    _od = {(_diff["doc"], _diff["idx"], _diff["h"],
            _diff["pv"], _diff["model"]): _diff}
    assert cache_key(_c) not in _od, "a different model still reuses —— the MODEL key is meaningless"
    print("  ✅ extraction cache round trip —— write then find · old-line compat · miss on model change")

    #  NO_LLM path matching —— these rows encode the rule README §"What stays on your machine"
    #  states in prose: "matched as path components from the vault root".  Let the two diverge
    #  and a user believes `wiki/Private/` is blocked when it is not.  No error is raised, so
    #  the docs are the only guidance, and in 929b109 only the code changed, leaving the README
    #  saying the exact opposite (deep review 2026-08-25).  status.py ⑪ pins the README side.
    _g = globals(); _n0 = _g["NO_LLM"]
    try:
        _g["NO_LLM"] = ("Private", "wiki/Finance")
        _table = [("Private/x.md", True), ("wiki/Private/x.md", False),
                  ("raw/my-Private-notes.md", False), ("Privateer/x.md", False),
                  ("wiki/Finance/2026.md", True), ("Finance/2026.md", False)]
        _bad = [r for r, want in _table if blocked_path(r) != want]
        assert not _bad, f"NO_LLM matching disagrees with the README table: {_bad}"

        #  ⚠ **The filesystem is case-insensitive and normalisation-preserving; the gate must match
        #     it.**  `KAL_NO_LLM=private` against `Private/` returned False while `ls private/`
        #     opened that very directory, and an NFC setting missed an NFD path —— which matters
        #     here because this vault's folder names are Korean (reproduced 2026-09-04).
        _nfc = unicodedata.normalize("NFC", "비공개")
        _nfd = unicodedata.normalize("NFD", "비공개")
        _g["NO_LLM"] = ("private", _nfc)
        _fold = [("Private/med.md", True), ("PRIVATE/med.md", True), ("private/med.md", True),
                 (_nfd + "/a.md", True), (_nfc + "/a.md", True),
                 #  and still not over-matching —— a prefix is not a component
                 ("my-private-notes/a.md", False), ("Private2/a.md", False)]
        _bad2 = [r for r, want in _fold if blocked_path(r) != want]
        assert not _bad2, f"case/unicode folding is wrong for: {_bad2}"
    finally:
        _g["NO_LLM"] = _n0
    # ── The graph write must refuse to shrink drastically ───────────────────────────────
    #  This is the write that was destroyed on 2026-09-04: an empty vault produced
    #  `0 entities · 0 relations · 0s` and replaced an eight-hour extraction.  Driven as a
    #  **subprocess against the real entry point**, because the failure was that no caller in any
    #  language checked —— testing the comparison in isolation would agree with itself.
    import subprocess as _sp, tempfile as _tf3, json as _j3
    with _tf3.TemporaryDirectory() as _d3:
        _home, _empty = os.path.join(_d3, "home"), os.path.join(_d3, "empty")
        os.makedirs(_home); os.makedirs(_empty)
        _out = os.path.join(_home, "lr_kg.json")
        _j3.dump({"entities": [{"name": f"e{i}"} for i in range(100)],
                  "relationships": [], "chunks": 0, "failed": 0,
                  "doc_hashes": {}, "extracted_at": 0}, open(_out, "w"))
        _env = {**os.environ, "KAL_VAULT": _empty, "KAL_HOME": _home}
        _here = os.path.dirname(os.path.abspath(__file__))
        _r = _sp.run([sys.executable, os.path.join(_here, "lr_extract.py")],
                     env=_env, capture_output=True, text=True)
        assert _r.returncode != 0, "an empty vault overwrote a populated graph"
        assert "throw away" in _r.stdout + _r.stderr, \
            f"the refusal does not say what would be lost: {(_r.stdout + _r.stderr)[-300:]}"
        assert len(_j3.load(open(_out))["entities"]) == 100, \
            "the guard fired but the file was written anyway"
        #  …and the escape hatch works, or the guard becomes something people delete.
        _r2 = _sp.run([sys.executable, os.path.join(_here, "lr_extract.py"), "--allow-shrink"],
                      env=_env, capture_output=True, text=True)
        assert len(_j3.load(open(_out))["entities"]) == 0, \
            f"--allow-shrink did not get past the guard: {(_r2.stdout + _r2.stderr)[-200:]}"
    print("  ✅ the graph write refuses to replace a populated lr_kg.json (and --allow-shrink passes)")

    print("  ✅ NO_LLM path matching —— 6 README rows · case-insensitive · NFC/NFD (both ways)")
    assert _k[:3] == ("a.md", 0, "HH"), f"the head of the key changed: {_k}"
    assert cache_key(_c) != (_c["doc"], _c["idx"], _c["h"]), "the key does not distinguish versions"

    # ⑥ **a fallback must never be cached.**
    #    Cache one and the next run hits every entry, never calls the LLM, CALL_STATS stays
    #    all zeros and the gate has nothing to look at —— stitched fragments go straight into
    #    the graph.  It does not pass the gate; it **goes around it.**
    _fallback(False)
    _run("Sorry, this is not JSON.")               # shape failure → fallback
    assert _fallback() is True, "a fallback is not flagged —— garbage enters the cache"

    _fallback(False)
    _run('{"profile": "a proper summary", "changes": [], "senses": []}')
    assert _fallback() is False, "a good summary is flagged as fallback —— the cache never fills"

    # An empty response (transport failure) is a fallback too
    _fallback(False)
    globals()["call_text"] = lambda *a, **k: ""
    try:
        summarize_one("test", ["fragment one", "fragment two"])
    finally:
        globals()["call_text"] = _orig
    assert _fallback() is True, "an empty response is not flagged as a fallback"

    # ⑦ **test the decision, not the flag** —— does it actually reach the cache?
    #    The first version only looked at _fallback(), so reverting to "cache fallbacks too"
    #    (the original bug) still passed.  A test that never calls a judgement cannot guard it.
    import threading as _th
    class _FH:
        def __init__(s): s.lines = []
        def write(s, x): s.lines.append(x)
        def flush(s): pass
    _lk = _th.Lock()

    _fh = _FH(); _fallback(True)
    assert cache_summary(_fh, _lk, "k1", "garbage", [], []) is False, "a fallback gets cached"
    assert _fh.lines == [], f"a fallback wrote {len(_fh.lines)} lines"

    _fh = _FH(); _fallback(False)
    assert cache_summary(_fh, _lk, "k2", "proper", [], []) is True, "a good summary is not cached"
    assert len(_fh.lines) == 1 and '"k2"' in _fh.lines[0], "the cache line looks wrong"

    # ⑧ is it reset per item —— otherwise one fallback blocks **everything after it**
    _fallback(True)                        # pretend the previous item was a fallback
    _run('{"profile": "the next item is fine", "changes": [], "senses": []}')
    assert _fallback() is False, \
        "the previous item's fallback flag persists —— good summaries never get cached"

    # ⑨ early abort —— the first question is **does it kill healthy runs**.
    #    The gate runs only after ex.map finishes, so at 91% failure it stopped only after
    #    making all 361 calls, retries included.  It gives up only at 20+ calls and 50%+ ——
    #    far more lenient than the final gate (10%).  Not a verdict, just saved time.
    def _try(ok, fail, shape):
        _ABORTED.clear()
        CALL_STATS.update(ok=ok, fail=fail, shape=shape)
        return abort_early()
    assert not _try(0, 0, 0), "gives up on zero calls"
    assert not _try(5, 5, 0), "gives up on a sample of 10 —— healthy runs die"
    assert not _try(19, 0, 19), "19 calls must not give up yet (the threshold is 20)"
    assert not _try(80, 20, 0), "gives up at 20% —— that is the final gate's call"
    assert _try(10, 10, 10), "does not stop at 20 calls · 50%"
    assert _try(5, 25, 5), "does not stop on a fatal rate"
    # **Shape failures alone** must stop it too —— a healthy relay with a babbling model.
    #   (fail=0, so counting transport alone gives 0% and it never stops.  That is the split.)
    assert _try(20, 0, 20), "100% shape failures do not stop it —— only transport is counted"
    # Once tripped it stays tripped **even when the stats turn healthy**.  If each thread
    # re-decided, an earlier failure would wash out and the remaining calls would keep going.
    CALL_STATS.update(ok=1000, fail=0, shape=0)          # pretend it suddenly turned healthy
    assert abort_early() is True, "the abort state does not persist —— the rest keeps going"
    _ABORTED.clear()
    assert not abort_early(), "the abort state survives a clear"
    CALL_STATS.update(ok=0, fail=0, shape=0)    # ── scope check —— rebuild_all.sh:45 calls it every run and nobody tested it ──
    #    Replace it with `missing = set()` and it still prints ✅ and carries on.  That brings
    #    back "documents indexed but never entering the knowledge graph", whose stale mark is
    #    dropped too, so they are **reported clean forever**.  (r4-guard, 2026-08-21)
    _idx, _ext, _del = {"a", "b", "c"}, {"a"}, {"b"}
    _m, _e = scope_gap(_idx, _ext, _del)
    assert _m == {"c"}, f"an indexed-only document is missed: {_m}"   # b is a deliberate exclusion
    assert _e == set(), f"invents an excess that is not there: {_e}"
    _m2, _e2 = scope_gap({"a"}, {"a", "z"}, set())
    assert _e2 == {"z"}, f"an extracted-only document is missed: {_e2}"
    assert scope_gap({"a"}, {"a"}, set()) == (set(), set()), "reports a mismatch on a match"
    #    ★ **does the caller go through it?**  The above only checks the arithmetic ——
    #    replace it with `missing, extra = set(), set()` and it still passes.  Then
    #    rebuild_all.sh prints ✅ and carries on, and the "reported clean forever" state this
    #    was meant to stop comes right back.  (r4-guard round 5, V5).  Small fakes are slotted
    #    in for scan_vault and collect so **the real check_scope runs.**
    import schema_v3 as _s3
    _osv, _oc = _s3.scan_vault, globals()["collect"]
    try:
        _s3.scan_vault = lambda: {1: {"path": "a/b.md"}, 2: {"path": "a/c.md"}}
        globals()["collect"] = lambda: [{"doc": "a_b"}, {"doc": "a_c"}]
        assert check_scope() == 0, "reports a mismatch on a match"
        globals()["collect"] = lambda: [{"doc": "a_b"}]          # a_c has dropped out
        assert check_scope() == 1, \
            "a document indexed but not extracted still passes as ✅ —— the caller " \
            "does not go through scope_gap"
    finally:
        _s3.scan_vault, globals()["collect"] = _osv, _oc
    #    A deliberate exclusion must shrink **missing only** —— shrinking extra hides real errors
    assert scope_gap({"a"}, {"a", "z"}, {"z"}) == (set(), {"z"}), \
        "a deliberate exclusion also wipes extra"



    print(f"  ✅ lr_extract self-check —— shape-failure counting · gate formula · "
          f"cache key ({PROMPT_VERSION}) · fallbacks not cached · early abort")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest(); sys.exit(0)
    # Record the run under ~/.kal/runs/ —— the web screen's "last run" only knew about runs
    # started from the web UI, so a CLI success still showed yesterday's failure as the last.
    from run_log import record
    with record("extract"):
        _main_extract()
