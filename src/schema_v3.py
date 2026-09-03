#!/usr/bin/env python3
"""Unified schema v3 — every one of the 8 adversarial-review findings applied.

Changes against v2:
  ① doc_id becomes a stable crc32(path) ID.  Adding or removing documents leaves existing ids alone
  ② (removed) chunk_ids —— 2026-08-19.  It was written and **never read**: 0 uses in search,
     0 in the graph export, 0 in the plugin.  All that remained was sync_v3 cleaning up what
     it had made itself.  There was room to use it for evidence snippets or precise expansion,
     but reviving it when such a consumer appears is the better trade.
  ③ incremental sync implemented — add, edit, delete and rename detection + dangling-reference cleanup
  ④ 29 entity types → normalised to the 7 declared plus other.  Names normalised too
  ⑤ relations stated as undirected — a directed flag + documented canonical ordering
  ⑥ a new meta table — the DB knows its own model name, dimensions and parameters
  ⑦ positions no longer truncated + a flag for whether they were
  ⑧ flat column removed — path is the sole identifier
"""
import vault_path
import os, re, glob, json, math, zlib, time, hashlib, collections, sys, datetime, functools

# The model weights are already on disk.  Without this, every process round-trips to
# huggingface.co **before** reading the local file —— measured 10.8s → 5.7s, **5.1s per process**.
# (The container had it at Dockerfile:41 and MCP at kal_mcp.py:27.  Only the host CLI was
#  missing it, while a comment in kal_search.py **asserted** the invariant ——
#  written down and not true.  Measured by r4-perf, 2026-08-21)
#
# It is a `setdefault` —— to genuinely fetch a new model, override with `HF_HUB_OFFLINE=0`.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
sys.path.insert(0, os.path.join(KAL_HOME, "bench"))
import lancedb
from lancedb.index import FTS, BTree, Bitmap, LabelList
from kal_lock import db_lock
from entity_resolve import build_canon, name_stats, display_name
import pyarrow as pa

import kal_config as _kal_config
from frontmatter import FM_RE, FM_OPEN_RE
# Where the vault lives.  Mounted at /vault inside the container (see docker-compose).
#  Priority is the same as every other setting: environment > ~/.kal/config.json > default.
#  This one place used to skip config.json, so `export KAL_VAULT=…` lived **only in that
#  shell** —— new terminals, the MCP server and cron all fell back to the default, silently.
#  The config file's override comes right after the environment.  With neither, vault_path
#  **fails loudly** —— it does not quietly land on the author's own path.
VAULT = (os.environ.get("KAL_VAULT")
         or _kal_config.path_override("vault")
         or vault_path.vault())
DB = os.environ.get("KAL_PATH", os.path.join(KAL_HOME, "db"))
#  ⚠ The literals that lived here moved to `kal_config`.  The values are unchanged ——
#     indexing results must not shift at the defaults, so they were compared by md5.
#     Priority: environment > ~/.kal/config.json > default.  The web UI writes the middle one.
_CFG = _kal_config.values()
MODEL = _CFG["embedding_model"]
CHUNK, OVERLAP = _CFG["chunk_chars"], _CFG["chunk_overlap"]
NGRAM = (_CFG["ngram_min"], _CFG["ngram_max"])
K1, B = _CFG["bm25_k1"], _CFG["bm25_b"]

#  Broken settings die **before** indexing.  Running anyway produces 0 chunks, or
#  (overlap > size) kills range() —— both blow up minutes later, somewhere unrelated.
_BAD = _kal_config.validate(_CFG)
if _BAD:
    raise SystemExit("❌ the configuration is not valid:\n" + "\n".join(
        f"   {k}: {why}" for k, why in _BAD.items()) +
        f"\n   fix it in: {_kal_config.CONFIG_PATH} or the environment")
FM_KEEP = ("title", "aliases", "tags", "why_captured", "summary")
# ⚠ This list decides two things at once —— **what is indexed locally** and **what lr_extract
#   sends off the machine** through `claude -p` (docs/PIPELINE.md §What leaves the machine).
#   So judge by "may this be sent", not by "may this be indexed".
#
# ── Split into two kinds.  It used to be one, and that was wrong **in both directions**. ──
#
#   ANY   excluded wherever it sits.  A tool-created directory, so the name is the meaning.
#   ROOT  excluded **only at the vault root**.  These names describe this vault's own layout,
#         so matching them anywhere below would swallow the user's notes.
#
# What happened (measured 2026-08-23, both directions reproduced):
#   ① silently dropped —— an experiment folder named `kg/` matched the `/kg/` substring and
#      was excluded whole.  No error, no warning, just `chunks 0 rows`.
#      The same happens the moment a user creates `projects/kg/` or `meetings/kallimachos/`.
#   ② silently **sent** —— clone this repository as `kal/` and `/kallimachos/` does not match,
#      so it is **indexed and sent through `claude -p`.**  An outbound boundary hung on one name.
#      (deep review, security lens: "transmission control quietly stops on someone else's disk layout")
SKIP_ANY = ("/.git/", "/.claude/", "/.obsidian/", "/node_modules/",
            # The two below are tool-created working state.  The vault's own .gitignore already
            # excludes them, so the user has classified them as "not for export".  They were
            # missing from the index/transmit list —— the moment OMC writes notepad.md or
            # plans/*.md they are sent silently.  (adversarial review 2026-08-18, security lens)
            "/.omc/", "/.ouroboros/")

#  ⚠ `references/` holds **vendored third-party documents** —— an openwiki bundle keeps the
#     upstream OKF specification there so its citations verify locally (1,006 lines, Apache-2.0).
#     Indexing it puts Google's spec vocabulary into a *personal* knowledge graph and pays LLM
#     calls to do it.  Caught mid-run 2026-09-02: "sending to the LLM: … references 2".
SKIP_ROOT = ("kg", "references")   # kg: export_graph --obsidian output, and indexing it feeds KG→note→KG back

#  The user can exclude more folders.  Colon-separated, relative to the vault.
#     KAL_SKIP="imported/slack-dm:private"
#  There used to be no such mechanism at all: excluding one folder meant editing the code.
#  Priority is the same as every other setting: environment > ~/.kal/config.json > default.
#  `kal_config` owns that decision —— reading os.environ directly here would ignore what the
#  settings screen changed, and the user would hit "I turned it off and it still goes out".
SKIP_EXTRA = tuple(x.strip().strip("/") for x in
                   str(_CFG.get("skip_extra", "")).split(":") if x.strip())


#  Markers that identify this repository.  A copy holding both of these files is ours.
#  Looking at one alone (say src/schema_v3.py) could wrongly claim someone else's folder.
_SELF_MARKS = (os.path.join("src", "schema_v3.py"), "justfile")


def _self_dir():
    """The directory name if this repository sits **inside the vault path**, else None.

    Hardcoding the name (`kallimachos`) silently fails for anyone who cloned it differently.
    Measuring our own location through `__file__` is right regardless of the name.

    ⚠ This alone is **not enough.**  In the container the code is at `/app` and the vault at
      `/vault`, so relpath yields `../app` and this returns None.  Yet a copy of the
      repository sits **in plain sight** inside the vault at `/vault/kallimachos`.  Measured
      (2026-08-24): 376 documents on the host, 407 in the container —— the same vault, 31
      apart, and those 31 went out through `claude -p`.  So `_self_copies()` re-checks by marker.
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # …/kallimachos
    try:
        rel = os.path.relpath(here, os.path.abspath(VAULT))
    except ValueError:                    # a different drive —— outside the vault
        return None
    return rel.split(os.sep)[0] if not rel.startswith("..") else None


@functools.lru_cache(maxsize=1)
def _self_copies():
    """Top-level directory names of **copies of this repository** inside the vault.

    It uses **both** the path comparison (`_self_dir`) and the marker check.  Either one
    alone is wrong on the host or in the container —— and the wrong side shows up not as an
    error but only as '31 more documents', which nobody notices.
    """
    out = set()
    d = _self_dir()
    if d:
        out.add(d)
    try:
        root = os.path.abspath(VAULT)
        for name in os.listdir(root):
            cand = os.path.join(root, name)
            if os.path.isdir(cand) and all(
                    os.path.exists(os.path.join(cand, m)) for m in _SELF_MARKS):
                out.add(name)
    except OSError:
        pass                              # unreadable vault: give up the judgement (do not block)
    return frozenset(out)


def is_skipped(path):
    """Is this a path to exclude from indexing and transmission?  `path` is absolute."""
    if any(x in path for x in SKIP_ANY):
        return True
    try:
        rel = os.path.relpath(path, os.path.abspath(VAULT))
    except ValueError:
        return False
    if rel.startswith(".."):
        return False                      # outside the vault was never in scope
    parts = rel.split(os.sep)
    #  ⚠ A generated `index.md` is navigation, not knowledge —— it is a list of links to the
    #     documents beside it, so indexing it puts a page whose entire body is other pages' titles
    #     into the same ranking as those pages.  Measured 2026-09-02: searching the bundle for
    #     "지식 그래프" returned `personal/sessions/claude/index.md` **first**, ahead of the three
    #     documents actually about it.  OKF §8 makes these files a required navigational surface,
    #     which is exactly why they must not compete as content.
    if parts[-1] == "index.md":
        return True
    top = parts[0]
    if top in SKIP_ROOT:
        return True
    #  ⚠ `SKIP_ROOT` only looks at the first segment.  The 699-page kg export was migrated to
    #     `personal/kg/` on 2026-09-02, which moved it out from under that check while leaving
    #     the reason for the check —— indexing a KG export feeds KG→note→KG back —— completely
    #     intact.  A guard whose reach depends on a directory name being at depth 1 reopens the
    #     moment someone nests it.  Matched at any depth.
    if any(seg in SKIP_ROOT for seg in parts[:-1]):
        return True
    if top in _self_copies():             # this project itself, placed inside the vault
        return True
    for extra in SKIP_EXTRA:              # user-specified —— matched as a path prefix
        e = extra.split("/")
        if parts[:len(e)] == e:
            return True
    return False


#  ⚠ The old name.  Callers used it as `x in path`, so it survives **for display and docs only**.
#     Judgement must always go through `is_skipped()` —— substring matching caused the defect above.
SKIP = SKIP_ANY + tuple(f"/{x}/" for x in SKIP_ROOT)

SESSION_DIR = "raw/conversations/sessions/"   # distill_sessions.py output → origin='session'
#  The openwiki bundle records the same provenance as a URI in `sources[]` instead of a path.
#  Matching it means the classification follows the document, not the directory it happens to sit in.
#  ⚠ The `-` prefix matters.  `sources:` entries are equally valid as
#     `  - resource: …` (the item starting on the same line) or as
#     `  - id: …` / `    resource: …`.  This tool emits the second, so a regex
#     anchored on `^\s*resource:` matched our own pages and would have missed any
#     bundle written the other way —— a self-check written for this guard caught it
#     before it mattered (2026-09-02).
SESSION_URI = re.compile(r"^[ \t]*(?:-[ \t]+)?resource:[ \t]*\"?(?:claude|codex)-session://", re.M)

# ④ The types declared in the prompt.  Anything else the LLM invents folds in here.
#    **Must match `lr_extract.ENTITY_TYPES`** —— a type missing here becomes "other".
CANON_TYPES = {"artifact", "tool", "project", "method", "failure",
               "decision", "metric", "event", "actor", "concept"}
TYPE_MAP = {
    # ── Old scheme → new scheme (so data from before a re-extraction still works) ──
    #    pattern folds into method.  The LLM split the two 16.2%/14.9%, not because a
    #    boundary existed but because it could not tell them apart (lr_extract.ENTITY_TYPES).
    #    person and organization become one actor —— one gate for personal data.
    "pattern": "method", "person": "actor", "organization": "actor",
    # ── Variants the LLM invents ──
    "system": "tool", "application": "tool", "platform": "tool", "framework": "tool",
    "infrastructure": "tool", "hardware": "tool", "library": "tool", "service": "tool",
    "file": "artifact", "path": "artifact", "config": "artifact", "schema": "artifact",
    "format": "artifact", "field": "artifact",
    "repository": "project", "product": "project", "codebase": "project",
    "process": "method", "practice": "method", "technique": "method",
    "plan": "method", "algorithm": "method", "workflow": "method",
    "error": "failure", "bug": "failure", "issue": "failure", "incident": "failure",
    "choice": "decision", "tradeoff": "decision", "rationale": "decision",
    "measurement": "metric", "benchmark": "metric", "statistic": "metric",
    "activity": "event", "milestone": "event",
    "group": "actor", "team": "actor", "company": "actor", "role": "actor",
    "goal": "concept", "feature": "concept", "capability": "concept",
    "domain": "concept", "context": "concept", "topic": "concept",
    "principle": "concept", "reference": "concept",
}


def stable_doc_id(path: str) -> int:
    """① A stable path-based ID.  Adding files does not shift existing ids."""
    return zlib.crc32(path.encode("utf-8")) & 0x7FFFFFFF


# Colour per type.  **Kept right beside the vocabulary** —— each viewer used to carry its own
# palette, and when the types moved to 10, two of the three went stale and six canonical types
# (actor · artifact · decision · failure · metric · project) lost their colour.  One of them,
# `artifact`, is **the largest type** (2,001 · 24.5%), so a quarter of the screen collapsed
# into a single grey mass —— the whole point of colouring by type was gone.  All three viewers
# now share **one object**, and `_selftest` enforces it by identity (`is`): copies drift. (2026-08-21)
#
# For dark-background viewers only (graph3d #0E1418 · galaxy #1e1e1e).
TYPE_COLOR = {
    "artifact": "#7FB3FF", "tool": "#FFB454", "project": "#B98CFF",
    "method": "#FF6E8A", "failure": "#FF5C5C", "decision": "#3FD6C0",
    "metric": "#FFD84D", "event": "#F79E4A", "actor": "#9BE564",
    "concept": "#5AA9FF", "other": "#8797A8",
}


# Types that carry identity.  The old scheme's `person` + `organization` merged into `actor`
# —— "merge them and the gate is one; two conditions means one gets missed eventually" was
# the reason for that redesign (docs/PIPELINE.md, entity types section).
#
# ⚠ If a name written here is absent from `CANON_TYPES`, the warning below is **permanently 0**.
#   That is worse than merely going quiet: this text is baked **into the HTML** that people
#   hand to others.  An alarm that cannot sound travels with the file as a false all-clear
#   reading "0 real names".  It actually happened —— after the move to 10 types these two
#   lines kept counting `person`/`organization`, so `graph3d.html` was stamped with
#   "person 0 · organization 0" while holding 296 real names.  (2026-08-21)
#   `--selftest` enforces membership in the canonical set.
IDENTITY_TYPES = ("actor",)


def count_identity(entities):
    return sum(1 for e in entities if (e.get("type") or "") in IDENTITY_TYPES)


# The **order** in which tables are written.  The point is that `meta` comes last.
#
# Why meta is last —— two things hang on it.
#
#  ① `meta` is the "the build finished" marker.  `kal_mcp._db_stamp()` reads it to decide
#     whether to drop its cache.  The sensor used to watch `documents` (2nd), while
#     `lr_entities` (7th) kept being written for 3 more seconds.  A cache filled inside that
#     window already holds the final stamp and is **never invalidated again** —— the MCP
#     server serves stale entities for as long as it lives.  (measured by r2-verify:
#     documents 1787252740.612 vs lr_entities 1787252743.582-.957, a 2.97-3.35s window)
#
#  ② The model guard in `prev_vectors()` reads `meta.embedding_model`.  Write meta **first**
#     and a crash in the middle leaves "meta naming the new model + vectors from the old
#     one", which the next build reuses —— embedding spaces mix silently.  Written last it
#     leaves the opposite, "old meta + new vectors", and the guard **refuses** to reuse them.
#     It fails towards the safe side of the two.
#
# `kal_mcp._selftest` enforces `TABLE_ORDER[-1] == "meta"` (it lives there because the sensor
# assumes it).  This repository's recurring failure shape is "claiming a guard exists without
# saying where", so the file name is written out exactly.  (r4-silent, round 5)
TABLE_ORDER = ("documents", "chunks", "ix_terms", "ix_postings", "ix_doclen",
               "lr_entities", "lr_relations", "meta")


def norm_type(t: str) -> str:
    t = (t or "concept").strip().lower()
    t = TYPE_MAP.get(t, t)
    return t if t in CANON_TYPES else "other"


def norm_name(n: str) -> str:
    """④ Name normalisation — fold whitespace, drop parenthetical notes, trim both ends."""
    n = re.sub(r"\s+", " ", (n or "").strip())
    n = re.sub(r"\s*\([^)]{0,40}\)\s*$", "", n)      # "qmd (Tobi Lütke)" → "qmd"
    return n.strip(" .,·-")


# ────────────────────────── Schema ──────────────────────────
# stale_docs is written by sync_v3 and cleared by schema_v3.  Two files touch the same table,
# so the schema definition lives **in this one place** (it used to be only in sync_v3, and
# the schema_v3 side quietly did nothing, swallowing a KeyError).
S_STALE = pa.schema([
    pa.field("doc_id", pa.int32()),
    pa.field("path", pa.string()),
    pa.field("reason", pa.string()),      # added | modified | renamed
    pa.field("marked_at", pa.int64()),
])


def schemas(dim):
    return {
        # ⑥ meta — the DB knows its own configuration
        "meta": pa.schema([pa.field("key", pa.string()), pa.field("value", pa.string())]),
        # ⑧ flat removed.  path is the sole identifier
        "documents": pa.schema([
            pa.field("doc_id", pa.int32()),          # ① crc32(path).  Stable
            pa.field("path", pa.string()),           # the natural key
            pa.field("abs_path", pa.string()),
            pa.field("title", pa.string()),
            pa.field("folder", pa.string()),
            pa.field("size", pa.int64()),
            pa.field("mtime", pa.int64()),
            pa.field("content_hash", pa.string()),   # ③ for rename detection
            pa.field("indexed_at", pa.int64()),
            # Curated notes vs documents distilled from sessions.  Searched together, trusted apart.
            pa.field("origin", pa.string()),         # vault | session
            pa.field("doc_type", pa.string()),       # brain-ingest frontmatter ("" when absent)
            # ── Time axis (docs/TEMPORAL_DESIGN.md §2.1) ──
            # Why not mtime: it **differs from the frontmatter date in 355/374 cases (94.9%)**
            # (median 6 days, max 132).  It is when the file was touched, not when the content is from.
            pa.field("doc_date", pa.string()),       # "2026-06-25" · "" when absent
            pa.field("date_src", pa.string()),       # generated_at|captured|created|updated|none
            # A separate axis.  doc_date is "when is this content from" (valid time);
            # doc_updated is "when was it last touched".  Filling one from the other mixes them.
            # Measured: 77 have it, and only 2 of those differ from doc_date —— a thin signal
            # today, but as the vault ages this gap becomes the "needs review" list.
            pa.field("doc_updated", pa.string()),    # "" means no edit history
            # The transmission gate.  MCP is the **second** outbound boundary and must consult
            # this per query; re-parsing frontmatter each time is impossible, so it is frozen at index time.
            pa.field("no_llm", pa.bool_()),
        ]),
        "chunks": pa.schema([
            pa.field("chunk_id", pa.int64()),        # doc_id*10000 + seq.  Stable
            pa.field("doc_id", pa.int32()),
            pa.field("seq", pa.int32()),
            pa.field("char_start", pa.int32()),
            pa.field("text", pa.string()),
            # Denormalised from documents.  chunks alone must be able to tell vault from session,
            # or the --origin filter ends up listing 700 doc_ids in an IN clause.
            pa.field("origin", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), dim)),
        ]),
        "ix_terms": pa.schema([
            pa.field("term_id", pa.int32()), pa.field("term", pa.string()),
            pa.field("df", pa.int32()), pa.field("idf", pa.float32()),
        ]),
        "ix_postings": pa.schema([
            pa.field("term_id", pa.int32()), pa.field("chunk_id", pa.int64()),
            pa.field("tf", pa.int32()),
            pa.field("positions", pa.list_(pa.int32())),   # ⑦ stored in full
            pa.field("pos_truncated", pa.bool_()),         # ⑦ True when truncated
        ]),
        "ix_doclen": pa.schema([
            pa.field("chunk_id", pa.int64()), pa.field("num_tokens", pa.int32()),
        ]),
        "lr_entities": pa.schema([
            pa.field("entity_id", pa.int32()),
            pa.field("name", pa.string()),
            pa.field("name_norm", pa.string()),      # ④ the merge key
            pa.field("type", pa.string()),           # ④ normalised to 8 kinds
            pa.field("type_raw", pa.string()),       # ④ the LLM's original type, preserved
            pa.field("description", pa.string()),
            pa.field("doc_ids", pa.list_(pa.int32())),
            pa.field("degree", pa.int32()),
            # ── Time axis (docs/TEMPORAL_DESIGN.md §2.4) ──
            # first/last_seen are computed from dates outside the LLM —— the lower bound of
            # as_of must survive a failed summary.  timeline is **only what the LLM saw change**.
            pa.field("first_seen", pa.string()),
            pa.field("last_seen", pa.string()),
            pa.field("timeline", pa.string()),      # JSON [{at, change}] · usually "[]"
            # The raw fragments, **unblurred**.  If timeline is "what changed", this is
            # "when did it say what".  The detail a summary throws away survives here.
            # Measured: 14,979 of them · 0.87MB —— 1.1× the summaries, so effectively free.
            pa.field("events", pa.string()),        # JSON [{at, doc_id, text}]
            # Polysemy candidates —— what the summarising LLM judged to be "two things".
            # **Never split automatically.**  homonym_suggest.py shows it to a person, and
            # once they confirm it in homonyms.yml, group_nodes does the splitting.
            pa.field("senses", pa.string()),        # JSON [{label, cue[]}] · usually "[]"
            pa.field("vector", pa.list_(pa.float32(), dim)),
        ]),
        "lr_relations": pa.schema([
            pa.field("rel_id", pa.int32()),
            pa.field("src_id", pa.int32()), pa.field("tgt_id", pa.int32()),
            pa.field("src_name", pa.string()), pa.field("tgt_name", pa.string()),
            pa.field("directed", pa.bool_()),        # ⑤ always False = undirected
            pa.field("keywords", pa.list_(pa.string())),
            pa.field("description", pa.string()),
            pa.field("weight", pa.float32()),
            pa.field("doc_ids", pa.list_(pa.int32())),
            # ── Time axis ──
            # Holds what an entity timeline cannot: **a relation's state transition**.
            #   "Obsidian—graphify: planned future feature → implemented plugin"
            #   "Phase 2—seCall:   required → on hold"
            # The graphify entity can say "it was implemented", but that the **relation** moved
            # from planned to implemented has nowhere else to live.
            pa.field("first_seen", pa.string()),
            pa.field("last_seen", pa.string()),
            pa.field("timeline", pa.string()),      # JSON · measured: only 11 of 12,251 non-empty
            pa.field("events", pa.string()),        # JSON [{at, doc_id, text}]
            # Polysemy candidates —— what the summarising LLM judged to be "two things".
            # **Never split automatically.**  homonym_suggest.py shows it to a person, and
            # once they confirm it in homonyms.yml, group_nodes does the splitting.
            pa.field("senses", pa.string()),        # JSON [{label, cue[]}] · usually "[]"
            pa.field("vector", pa.list_(pa.float32(), dim)),
        ]),
        # Written by sync_v3 and cleared by schema_v3.  Defined in this one place.
        "stale_docs": S_STALE,
    }


INDEXES = {
    "documents":    [("path", "BTREE"), ("folder", "BITMAP"), ("doc_id", "BTREE"),
                     ("origin", "BITMAP"), ("doc_type", "BITMAP")],
    "chunks":       [("doc_id", "BTREE"), ("chunk_id", "BTREE"), ("origin", "BITMAP")],
    "ix_terms":     [("term", "BTREE")],
    "ix_postings":  [("term_id", "BTREE"), ("chunk_id", "BTREE")],
    "ix_doclen":    [("chunk_id", "BTREE")],
    "lr_entities":  [("type", "BITMAP"), ("name_norm", "BTREE"),
                     ("doc_ids", "LABEL_LIST")],
    "lr_relations": [("doc_ids", "LABEL_LIST"), ("keywords", "LABEL_LIST"),
                     ("src_id", "BTREE"), ("tgt_id", "BTREE")],
}
FTS_COL = {"chunks": "text", "lr_entities": "description", "lr_relations": "description"}
# ⚠ Dropping any of these silently breaks Korean search.  lancedb's FTS() defaults are
#   stem=True · remove_stop_words=True · ascii_folding=True · ngram(3,3), which is
#   **the exact opposite of this configuration**.  Miss one and the index changes with no error.
#   (use_tantivy was dropped: the new config API has no counterpart —— it is always non-tantivy)
SCALAR_CFG = {"BTREE": BTree, "BITMAP": Bitmap, "LABEL_LIST": LabelList}
FTS_KW = dict(base_tokenizer="ngram", ngram_min_length=NGRAM[0], ngram_max_length=NGRAM[1],
              lower_case=True, stem=False, remove_stop_words=False,
              ascii_folding=False)


# ────────────────────────── Build ──────────────────────────
def clean(t):
    m = FM_RE.match(t)
    if not m:
        return t.strip(), ""
    kept, title = [], ""
    for line in m.group(1).split("\n"):
        k = line.split(":", 1)[0].strip().lower()
        if k in FM_KEEP and ":" in line:
            v = re.sub(r"[\[\]\"']", " ", line.split(":", 1)[1]).strip()
            if v:
                kept.append(v)
            if k == "title":
                title = v
    body = t[m.end():]
    return (("\n".join(kept) + "\n\n" + body).strip() if kept else body.strip()), title


# Where the date comes from.  The first one present, in this order.
#   generated_at  when a machine produced it (distill_sessions)
#   captured      the day the conversation was captured
#   created       the day a person created the document
#   updated       the day a person **edited** it —— not a creation time, so it is the last resort
# Which key was used is recorded in date_src.  Mixed together they cannot be told apart later.
DATE_KEYS = ("generated_at", "captured", "created", "updated")


def classify_origin(rel, raw):
    """`session` or `vault`.

    ⚠ **Provenance, not path.**  This was `rel.startswith(SESSION_DIR)`, which only recognises the
       vault's own `raw/conversations/sessions/`.  An openwiki bundle keeps the same documents at
       `personal/sessions/{claude,codex}/`, so all 299 classified as `vault` and
       `kal_search(origin="session")` —— whose docstring says "distilled from a conversation" ——
       returned nothing.  Measured 2026-09-02 with KAL_VAULT on the bundle: 306 documents,
       origin=session for 0.

       Pulled out of the indexing loop so a mutation can be caught: inlined, removing the
       provenance arm left every self-check green.

    ⚠ **The frontmatter, not the first 2,000 characters.**  `raw[:2000]` reaches into the body,
       so a note *documenting* the session URI —— at a line start, in a fenced code block, or as a
       list item —— was classified `session` and dropped from `kal_search --origin vault`.
       Reproduced 2026-09-04 on all three spellings.  This corpus is largely write-ups about this
       pipeline, so it fires in practice.  Same body-vs-frontmatter class as `owned()` in
       openwiki_emit and promote_distilled, which were both fixed for it.
    """
    if rel.startswith(SESSION_DIR):
        return "session"                       # the vault layout, kept for anything predating the bundle
    m = FM_RE.match(raw)
    return "session" if (m and SESSION_URI.search(m.group(1))) else "vault"


#  **The transmission gate, in one place.**  This exact regex was copied into six modules
#  (kal_mcp ×2, lr_extract, okf_convert, openwiki_emit and here).  Six copies of a privacy rule
#  drift, and the drift is silent by construction —— a document the user meant to keep local is
#  simply sent.  This repository has already paid for a list kept in two places.
#
#  ⚠ **A trailing comment used to open the gate.**  `no_llm: true # private` is valid YAML and
#     the obvious thing to write, and `\s*$` refused it —— the document was indexed with
#     no_llm=False and went to the LLM with nothing said.  `yes`/`on` are YAML 1.1 true and are
#     accepted for the same reason: the failure mode is silent, so being strict is not safe.
#     (codex review 2026-09-02, blocker #8 —— reproduced)
#  ⚠ **Quoting the key, or a space before the colon, used to open the gate.**  Both are ordinary
#     YAML and both parse to `{"no_llm": True}`:
#         "no_llm": true          no_llm : true          no_llm: "true"
#     The old pattern required a bare key, no space, and an unquoted value, so all three were read
#     as **no gate at all** and the document went to the LLM with nothing said.  Reproduced by an
#     adversarial review 2026-09-03; the hole was in every consumer, because they share this one
#     constant.  Leading whitespace is still refused —— an indented `no_llm` is a *nested* key, not
#     the document's own, and treating it as a gate would block pages that merely mention it.
#  How far into a **malformed** frontmatter the gate still looks.  Long enough for a real
#  frontmatter (the largest in this corpus is under 3 KB), short enough that prose mentioning
#  the key in a body does not trip it.
FM_SCAN_CHARS = 4000

NO_LLM_RE = re.compile(
    r"""^["']?no_llm["']?[ \t]*:[ \t]*["']?(?:true|yes|on)["']?[ \t]*(?:\#.*)?$""",
    re.M | re.I | re.X)


def doc_meta(raw):
    """(doc_date, date_src, no_llm, doc_updated).  Reads the frontmatter only.

    Why it is separate from clean() —— clean has many callers and its (body, title) contract
    is settled.  Widening the return value here would mean fixing every one of them.
    """
    # Matching the fence too narrowly **quietly opens the transmission gate.**  Measured, it
    # diverged from lr_extract's judgement in three cases: BOM, a leading blank line, and
    # CRLF —— the document is indexed with no_llm=False and MCP cannot catch it.
    m = FM_RE.match(raw)
    if not m:
        #  ⚠ **A frontmatter that fails to parse must not mean "send it".**  The fence above is
        #     strict, and five ordinary mistakes made it miss —— no closing fence, `----`, `--- x`,
        #     an indented fence, no trailing newline —— each of which returned no_llm=False for a
        #     document whose author had written `no_llm: true`.  The gate then read "not blocked"
        #     and the note went to the LLM (reproduced 2026-09-04).
        #     A parse failure is not evidence of consent.  So when the text *looks like* it was
        #     trying to carry frontmatter, the head is scanned and the gate fails **closed**.
        #     Only the head: a `no_llm` far down the body is prose about the key, not a mark.
        if FM_OPEN_RE.match(raw) and NO_LLM_RE.search(raw[:FM_SCAN_CHARS]):
            return "", "none", True, ""
        return "", "none", False, ""
    fm = m.group(1)
    no_llm = bool(NO_LLM_RE.search(fm))

    def _iso(v):
        d = (v or "").strip().strip("\"'")[:10]
        return d if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) else ""

    def pick(key):
        v = re.search(r"^%s:\s*(\S+)" % key, fm, re.M)
        return _iso(v.group(1)) if v else ""

    #  ⚠ **OKF keeps the date nested, and this function only ever looked at flat keys.**
    #     An openwiki bundle writes `generated: { by: …, at: "2026-06-18T00:00:00Z" }` (or the
    #     block form), never `generated_at:`.  Measured 2026-09-02: every page of the bundle came
    #     back `("", "none", …)` —— which does not error, it just removes the whole corpus from
    #     the time axis, so `kal_timeline`, `as_of` and `since/until` return nothing and look
    #     like an empty history rather than a broken reader.
    #
    #     Anchored on the `generated:` key so it cannot pick up the `at:` of a `verified` entry
    #     or the `last_modified:` of a source —— those are different assertions with different
    #     meanings, and silently substituting one for another is the same class of bug again.
    def okf_generated_at():
        m2 = re.search(r"^generated:[ \t]*\{[^}\n]*?\bat:\s*([^,}\s]+)", fm, re.M)
        if m2:
            return _iso(m2.group(1))
        m2 = re.search(r"^generated:[ \t]*\r?\n((?:[ \t]+\S.*\r?\n?)+)", fm, re.M)
        if m2:
            inner = re.search(r"^[ \t]+at:\s*(\S+)", m2.group(1), re.M)
            if inner:
                return _iso(inner.group(1))
        return ""

    upd = pick("updated")
    #  Tried before the flat keys: a document carrying both is an openwiki page, and its
    #  `generated.at` is the authoritative one —— the flat key would be a leftover.
    okf_at = okf_generated_at()
    if okf_at:
        return okf_at, "generated.at", no_llm, upd
    for k in DATE_KEYS:
        d = pick(k)
        if d:
            # Falling back to updated means this is not a creation time.  Dropping it entirely
            # would erase the document from the time axis, so it is used and date_src says why.
            return d, k, no_llm, upd
    return "", "none", no_llm, upd


def effective_date(doc_date, doc_updated):
    """The date the time axis **should actually use**.

    What we extract is **the current file content**.  If a document was edited later, the
    text we read is that revision, not the original.  Dating it by `created` therefore
    **backdates the revision's claims to the original moment** ——
    `wiki/entities/qmd.md` has created 2026-04-24 and updated 2026-06-23, and so was
    backdated by two months.

    The error is asymmetric, so the later date wins:
      a typo fix stamped as updated  →  merely a little late.  "the text then reads so" is true
      a meaning change stamped as created  →  **plainly false**

    `doc_date` itself is left alone —— "when was this first written about" is a different
    question, and it stays in `documents` as it is.
    """
    return max(x for x in (doc_date, doc_updated) if x) if (doc_date or doc_updated) else ""


def tokenize(text, lo=NGRAM[0], hi=NGRAM[1]):
    t = text.lower()
    return [(t[i:i + n], i) for n in range(lo, hi + 1)
            for i in range(len(t) - n + 1) if t[i:i + n].strip()]


#  The body a document must have before the indexer will read it.  Below this it is a stub and
#  contributes nothing but a row.  `clean()` measures **characters**, after it has folded the kept
#  frontmatter values back in —— so this is not a file size and cannot be approximated by one.
BODY_FLOOR = 60

#  How far the document count may fall in one run before the rebuild refuses.  An edit session
#  changes a handful of notes; losing more than a third of them means the vault is not what the
#  DB was built from.  `promote_distilled.SHRINK_RATIO` guards the vault the same way.
DB_SHRINK_RATIO = 0.67


#  Paths `glob` produced but `open` refused, for the most recent walk.  **Not discarded.**
#  A dangling symlink —— which an Obsidian rename or a moved attachment leaves routinely —— is
#  yielded by `glob`, passes `is_skipped`, and then raises in `open`.  Swallowing that made it
#  leave `seen`, which on the incremental path is not "unreadable" but **`deleted`**, and `sync`
#  removes it from the index.  A file that is present is neither content nor a deletion, and
#  CLAUDE.md names this failure directly: 조용한 실패를 만들지 않는다.  Same idiom as
#  `lr_extract.NO_LLM_SKIPPED` —— count them, surface them, let the caller decide.
UNREADABLE: list[str] = []


def indexable(path):
    """Would the indexer read this file?  → (raw, body, title), or None.

    **The single place that answers this.**  It had two —— `scan_vault` below, and a size floor
    re-implemented in `api/main.go`'s pre-run guard —— and they disagreed exactly where it hurt:
    an OKF page carries ~80 bytes of frontmatter, so a file with a three-character body clears a
    60-**byte** floor while failing this 60-**character** one.  Measured 2026-09-04: three such
    files on disk counted as 3 in Go and 0 here, the guard let `index` run, and every table was
    overwritten empty.  Go now calls `indexable_count` rather than keeping its own copy.
    """
    if is_skipped(path):
        return None
    try:
        raw = open(path, encoding="utf-8", errors="ignore").read()
    except OSError:
        #  ⚠ Recorded, not swallowed.  It is still not indexable —— there are no bytes to index ——
        #     but the caller has to be able to tell "the vault does not have this any more" from
        #     "the vault has it and I could not read it".  Those get opposite treatment.
        UNREADABLE.append(path)
        return None
    body, title = clean(raw)
    if len(body) < BODY_FLOOR:
        return None
    return raw, body, title


def _begin_walk():
    """Start a walk —— the unreadable list belongs to one walk, not to the process."""
    UNREADABLE.clear()


def indexable_count(root, limit=0):
    """How many files under `root` the indexer would read.  `limit` > 0 stops early.

    `limit` bounds the **file reads**, which is the expensive half; the directory walk is paid in
    full either way.  It used to say "returns after the first hit instead of reading the whole
    vault", which read as if the walk were bounded too —— it is not, and that is the sentence
    someone would trust on a vault ten times this size.  `iglob` at least keeps the walk lazy
    rather than materialising and sorting every path first (measured 2026-09-04: glob+sort alone
    is 12ms of the guard's cost on 1,134 files; the subprocess's lancedb import is ~1.1s of it).

    Order is not part of the answer here —— a count does not depend on it —— so unlike `scan_vault`
    this does not sort.
    """
    #  ⚠ `is_skipped` resolves paths against the module-level VAULT, not against its argument ——
    #     so asking about a different root without rebinding it silently evaluates the skip rules
    #     against the wrong tree.  Caught by the existing index.md fixture: a vault of nothing but
    #     generated indexes counted 1 instead of 0 (2026-09-04).
    global VAULT
    was, VAULT = VAULT, os.path.abspath(root)
    _begin_walk()
    try:
        n = 0
        for f in glob.iglob(os.path.join(VAULT, "**", "*.md"), recursive=True):
            if indexable(f) is None:
                continue
            n += 1
            if limit and n >= limit:
                break
        return n
    finally:
        VAULT = was


def scan_vault():
    out = {}
    _begin_walk()
    for f in sorted(glob.glob(f"{VAULT}/**/*.md", recursive=True)):
        got = indexable(f)
        if got is None:
            continue
        raw, body, title = got
        rel = os.path.relpath(f, VAULT)
        st = os.stat(f)
        did = stable_doc_id(rel)
        if did in out:                      # ① a hash collision — fail immediately
            raise SystemExit(f"doc_id collision: {rel} vs {out[did]['path']}")
        # brain-ingest documents from distill_sessions.py live in raw/conversations/sessions/.
        # They are ordinary vault files, but their provenance differs, so origin separates them
        # (a curated note = written by the user; session = distilled by an LLM from a conversation).
        #  ⚠ **Provenance, not path.**  This used to be `rel.startswith(SESSION_DIR)`, which only
        #     recognises the vault's own `raw/conversations/sessions/`.  An openwiki bundle keeps
        #     the same documents at `personal/sessions/{claude,codex}/`, so all 299 of them
        #     classified as `vault` and `kal_search(origin="session")` —— whose docstring says
        #     "distilled from a conversation" —— returned nothing.  Measured 2026-09-02 with
        #     KAL_VAULT pointed at the bundle: 306 documents, origin=session for 0 of them.
        #     A session URI in `sources[]` is what actually makes a document a session record, and
        #     it survives any directory layout.  The path test stays as the fallback for a vault
        #     that predates the bundle.
        origin = classify_origin(rel, raw)
        m = re.search(r"^doc_type:\s*(\S+)", raw[:1200], re.M)
        ddate, dsrc, no_llm, dupd = doc_meta(raw)
        out[did] = {"doc_id": did, "path": rel, "abs_path": f,
                    "title": title or os.path.basename(rel)[:-3],
                    "folder": os.path.dirname(rel) or ".",
                    "size": st.st_size, "mtime": int(st.st_mtime),
                    "content_hash": hashlib.sha256(raw.encode()).hexdigest()[:16],
                    "indexed_at": int(time.time()),
                    "origin": origin, "doc_type": m.group(1) if m else "",
                    "doc_date": ddate, "date_src": dsrc, "no_llm": no_llm,
                    "doc_updated": dupd,
                    "_body": body}
    return out


def diff_vault(new, db):
    """③ Incremental sync — decide add / edit / delete / rename.

    Session documents are now real files in the vault (raw/conversations/sessions/) too, so
    scan_vault picks them up as well.  Everything is compared without filtering by origin ——
    filtering would make the session side look like an 'add' on every run.
    """
    try:
        old = {r["doc_id"]: r for r in db.open_table("documents").search().limit(99999).to_list()}
    except Exception:
        return {"added": set(new), "modified": set(), "deleted": set(),
                "renamed": {}, "unchanged": set(), "first_build": True}
    added = set(new) - set(old)
    deleted = set(old) - set(new)
    modified = {d for d in set(new) & set(old)
                if new[d]["content_hash"] != old[d]["content_hash"]}
    unchanged = (set(new) & set(old)) - modified
    # rename = the same content_hash appears on both the deleted and the added side
    oh = {old[d]["content_hash"]: d for d in deleted}
    renamed = {}
    for d in list(added):
        h = new[d]["content_hash"]
        if h in oh:
            renamed[oh[h]] = d
            added.discard(d)
            deleted.discard(oh[h])
    return {"added": added, "modified": modified, "deleted": deleted,
            "renamed": renamed, "unchanged": unchanged, "first_build": False}


def llm_gate(doc_ids, blocked):
    """Decide whether derived text may be exported.  The `no_llm` **policy lives here alone**.

    Why here —— this module defines `no_llm` (`doc_meta` reads it from the frontmatter and the
    documents schema stores it).  `kal_mcp` already imports from here.  The other way round,
    an export importing `kal_mcp` would drag in the MCP SDK and the whole search stack.

    Filtering the document list alone is not enough —— description and timeline drawn from a
    no_llm document still go out with docs[] empty: **sensitive sentences with no source**.  Worse.

    Returns: "pass" | "redact" | "block"
    """
    if not blocked:          # with nothing blocked even unknown provenance passes —— the
        return "pass"        # subset rule below only means something once something is blocked
    ids = set(doc_ids or ())
    if ids <= blocked:       # everything is blocked, or **the provenance is unknown**
        return "block"       #   the empty set is a subset of anything → 0 sources is blocked too
    if ids & blocked:        # only some are blocked → redact the body
        return "redact"
    return "pass"


REDACTED = "[some sources are excluded from transmission, so the description is withheld]"


def prev_vectors(db, table, text_of):
    """Pull **vectors built from the same string** out of the previous index.  `{string: vector}`.

    Why it is needed
      85% of build time is embedding (85.8s of a measured 100.1s).  Yet every recorded run
      reads `added 0 · edited 0 · unchanged 376` —— nothing changed, and still 3,370 chunks,
      7,620 entities and 12,246 relations were re-encoded every time.  Measured, the rows
      whose content had changed were 0/7620 and 0/12246.

    Why no new store was built
      The string fed to the embedder is **a deterministic function of stored columns** (`text`
      for chunks, `name`+`description` for entities, both names + `description` for
      relations).  So reading the previous table alone reconstructs the keys exactly.  No
      schema change, no separate cache file.

    ⚠ **Reuse only when the model is the same.**  Mixing vectors from a different model
      silently breaks search —— no error, just worse results, which takes a long time to spot.
      The same risk that motivates baking weights into the image and setting `HF_HUB_OFFLINE=1`.
      If meta cannot be read, or the model differs, an **empty dict** forces a full re-encode.
    """
    try:
        meta = {r["key"]: r["value"] for r in
                db.open_table("meta").search().limit(99).to_list()}
    except Exception:
        return {}
    # Only the model name is checked.  The model fixes the dimension, so a matching name brings
    # it along (and `DIM` is only known after actually loading the model, so it is unusable here).
    if meta.get("embedding_model") != MODEL:
        print(f"   not reusing vectors —— the model differs "
              f"({meta.get('embedding_model')} → {MODEL})")
        return {}
    try:
        rows = db.open_table(table).search().limit(999999).to_list()
    except Exception:
        return {}
    return {text_of(r): r["vector"] for r in rows if r.get("vector") is not None}


def encode_reusing(m, texts, prev, label):
    """Skip strings already in `prev` and encode only the rest."""
    out, todo = [None] * len(texts), []
    for i, t in enumerate(texts):
        v = prev.get(t)
        if v is None:
            todo.append(i)
        else:
            out[i] = v
    if todo:
        #  Show a progress bar **only on a terminal**.  On a large vault this single line
        #  printed nothing for minutes and looked hung.  The web UI streams this output as a
        #  log, and a \r progress bar wrecks a log —— hence isatty as the condition.
        got = m.encode([texts[i] for i in todo], normalize_embeddings=True,
                       batch_size=64,
                       show_progress_bar=(len(todo) >= 2000 and sys.stderr.isatty()))
        for j, i in enumerate(todo):
            out[i] = got[j].tolist()
    print(f"   {label}: reused {len(texts) - len(todo)} · new {len(todo)}")
    #  How many were actually encoded —— the unit estimate.py learns its speed from.
    #
    #  ⚠ **Chunks only.**  This function is called three times per run (chunks, entities,
    #     relations), and counting all three diverges from estimate's unit (= chunks to
    #     embed), skewing the learned speed.  It overwrites, so left alone **the last call
    #     (0 relations) wins and units=0**, killing the learning —— measured (2026-08-24).
    if label == "chunks":
        try:
            import run_log; run_log.count(len(todo))
        except Exception:
            pass
    return out


def build_chunks(docs, m, db=None):
    rows = []
    for d in sorted(docs.values(), key=lambda x: x["path"]):
        t = d["_body"]
        for seq, i in enumerate(range(0, len(t), CHUNK - OVERLAP)):
            piece = t[i:i + CHUNK]
            if piece.strip():
                rows.append({"chunk_id": d["doc_id"] * 10000 + seq,   # ① a stable id
                             "doc_id": d["doc_id"], "seq": seq,
                             "char_start": i, "text": piece,
                             "origin": d.get("origin", "vault")})
    texts = ["passage: " + r["text"] for r in rows]
    prev = prev_vectors(db, "chunks", lambda x: "passage: " + x["text"]) if db else {}
    for r, v in zip(rows, encode_reusing(m, texts, prev, "chunks")):
        r["vector"] = v
    return rows


def build_inverted(chunks):
    tf = collections.defaultdict(lambda: collections.defaultdict(list))
    doclen = {}
    for c in chunks:
        toks = tokenize(c["text"])
        doclen[c["chunk_id"]] = len(toks)
        for tok, pos in toks:
            tf[tok][c["chunk_id"]].append(pos)
    N = len(chunks)
    terms = sorted(tf)
    tid = {t: i for i, t in enumerate(terms)}
    t_rows = [{"term_id": tid[t], "term": t, "df": len(tf[t]),
               "idf": math.log((N - len(tf[t]) + 0.5) / (len(tf[t]) + 0.5) + 1.0)}
              for t in terms]
    p_rows = [{"term_id": tid[t], "chunk_id": cid, "tf": len(ps),
               "positions": ps, "pos_truncated": False}      # ⑦ in full
              for t in terms for cid, ps in tf[t].items()]
    d_rows = [{"chunk_id": c, "num_tokens": n} for c, n in doclen.items()]
    return t_rows, p_rows, d_rows


def _fold_ev(items, flat2id, cap=60):
    """Fold events —— by date · deduplicated · slugs resolved to doc_id · the **latest** cap.

    Why the cap: one entity reaches 124 fragments, and carrying all of them into an MCP
    response eats context.  It truncates the same way as `_fold_tl` —— **keep the newest.**

    When `doc` cannot be resolved, `doc_id` is left None but the row is **not dropped** ——
    the event itself is valid; only its source cannot be pointed at.
    """
    seen, out = set(), []
    for e in items:
        at, txt = e.get("at", ""), (e.get("text") or "").strip()
        if not at or not txt or (at, txt) in seen:
            continue
        seen.add((at, txt))
        out.append({"at": at, "doc_id": flat2id.get(e.get("doc", "")),
                    "rev": e.get("rev", "current"),
                    # False = this date is **an estimate**.  An old revision with no known
                    # extraction time falls back to the creation date, matching the current one.
                    "at_exact": e.get("at_exact", True), "text": txt})
    out.sort(key=lambda e: e["at"])
    return out[-cap:]


def _fold_tl(items):
    """Fold the timeline —— by date · identical (at, change) deduplicated · the **latest** 12.

    Why a cap: this rides whole into MCP responses.  Dozens of changes means the "only what
    changed" selection failed, so truncating protects the response.

    ⚠ The **direction** of the truncation matters.  The old version used `[:12]`, keeping the
    **oldest** 12 and throwing away the newest —— the exact opposite of the goal, which is
    "what is its value now".  It is `[-12:]` today.
    """
    seen, out = set(), []
    for c in items:
        key = (c.get("at", ""), (c.get("change") or "").strip())
        if key[0] and key[1] and key not in seen:
            seen.add(key)
            out.append({"at": key[0], "change": key[1]})
    return sorted(out, key=lambda c: c["at"])[-12:]


def build_graph(kg, docs, chunks, m, db=None):
    """④ Re-merge after normalising names and types."""
    path2id = {d["path"]: d["doc_id"] for d in docs.values()}
    flat2id = {d["path"][:-3].replace("/", "_"): d["doc_id"] for d in docs.values()}
    by_doc = collections.defaultdict(list)
    for c in chunks:
        by_doc[c["doc_id"]].append(c)

    def resolve(names):
        return sorted({flat2id[x] for x in names if x in flat2id})


    # ④ re-merge after normalisation
    #
    # The merge key is entity_resolve.merge_key (docs/ENTITY_RESOLVE.md).  The exclusion rules
    # need group context, so names are collected into a canon first.  Entity keys, relation
    # pair keys and endpoint resolution **all ride that one canon** — let the three diverge
    # and relations point at entities that do not exist (the HIGH-1 family).
    _names = {norm_name(e["name"]).lower() for e in kg["entities"] if norm_name(e["name"])}
    for r in kg["relationships"]:
        for side in ("source", "target"):
            nm = norm_name(r[side])
            if nm:
                _names.add(nm.lower())
    _stats = name_stats(kg["entities"], kg["relationships"],
                        name_of=lambda e: norm_name(e.get("name", "")),
                        docs_of=lambda e: e.get("docs", ()),
                        ends_of=lambda r: (norm_name(r.get("source", "")),
                                           norm_name(r.get("target", ""))))
    canon, _ngroups = build_canon(sorted(_names), _stats)
    if _ngroups:
        print(f"④ name merge — {_ngroups} groups (spelling variants absorbed)")

    merged = {}
    for e in kg["entities"]:
        nm = norm_name(e["name"])
        if not nm:
            continue
        k = canon.get(nm.lower(), nm.lower())
        if k not in merged:
            # name takes the representative key's original spelling.  Corrected below to the real one.
            merged[k] = {"name": nm, "name_norm": k, "types": [], "descs": [],
                         "docs": set(), "forms": {}, "seen": [], "tl": [], "ev": [], "sn": []}
        # Which of an entity's spellings to show — the one that appears most often
        merged[k]["forms"][nm] = merged[k]["forms"].get(nm, 0) + 1
        merged[k]["types"].append(e["type"])
        if e["description"]:
            merged[k]["descs"].append(e["description"])
        merged[k]["docs"] |= set(e["docs"])
        # Merging happens **twice** (lr_extract.group_nodes → here), so the time axis must fold
        # along with it —— otherwise the timeline vanishes when spelling variants combine.
        merged[k]["seen"] += [x for x in (e.get("first_seen"), e.get("last_seen")) if x]
        merged[k]["tl"] += e.get("timeline") or []
        merged[k]["ev"] += e.get("events") or []
        # The first judgement wins —— if a later one overwrote it as spelling variants combine,
        # the result would differ from the candidate a person actually reviewed.
        merged[k]["sn"] = merged[k]["sn"] or (e.get("senses") or [])

    ent_rows, name2id = [], {}
    for k, v in merged.items():
        raw_t = collections.Counter(v["types"]).most_common(1)[0][0]
        dids = resolve(v["docs"])
        name2id[k] = len(ent_rows)
        # The display name —— the rule and its rationale live in entity_resolve.display_name.
        # It is not rewritten here: the version that used to live here failed to normalise the
        # alias lookup key, so the representative a user wrote was **always** ignored.
        v["name"] = display_name(k, v["forms"])
        ent_rows.append({"entity_id": len(ent_rows), "name": v["name"], "name_norm": k,
                         "type": norm_type(raw_t), "type_raw": raw_t,
                         "description": " ".join(dict.fromkeys(v["descs"]))[:2000],
                         "doc_ids": dids,
                         "degree": 0,
                         "first_seen": min(v["seen"]) if v["seen"] else "",
                         "last_seen": max(v["seen"]) if v["seen"] else "",
                         "timeline": json.dumps(_fold_tl(v["tl"]), ensure_ascii=False),
                         "events": json.dumps(_fold_ev(v["ev"], flat2id),
                                              ensure_ascii=False),
                         "senses": json.dumps(v["sn"], ensure_ascii=False)})

    # ⑤ undirected — canonical ordering merges duplicates, directed=False stated explicitly
    rmerged = {}
    for r in kg["relationships"]:
        s, t = norm_name(r["source"]), norm_name(r["target"])
        if not s or not t:
            continue
        ks = canon.get(s.lower(), s.lower())
        kt = canon.get(t.lower(), t.lower())
        if ks == kt:
            continue          # a self-loop from the start, or one the merge created
        key = tuple(sorted([ks, kt]))
        if key not in rmerged:
            rmerged[key] = {"s": ks, "t": kt, "kw": set(), "descs": [], "docs": set(),
                            "seen": [], "tl": [], "ev": [], "sn": []}
        rmerged[key]["kw"] |= set(r["keywords"])
        if r["description"]:
            rmerged[key]["descs"].append(r["description"])
        rmerged[key]["docs"] |= set(r["docs"])
        rmerged[key]["seen"] += [x for x in (r.get("first_seen"), r.get("last_seen")) if x]
        rmerged[key]["tl"] += r.get("timeline") or []
        rmerged[key]["ev"] += r.get("events") or []
        # The first judgement wins —— if a later one overwrote it as spelling variants combine,
        # the result would differ from the candidate a person actually reviewed.
        rmerged[key]["sn"] = rmerged[key]["sn"] or (r.get("senses") or [])

    rel_rows = []
    dropped_rel = 0
    for key, v in rmerged.items():
        # ⚠ key is an alphabetically sorted pair.  Taking ids in key order while taking names
        # in the original order (v["s"], v["t"]) makes the two disagree — 46% of relations did.
        # Ids and names are both taken **from the same original name** so they stay paired.
        # v["s"]/v["t"] are already canon keys, in the same key space as the entity merge.
        si = name2id.get(v["s"], -1)
        ti = name2id.get(v["t"], -1)
        if si < 0 or ti < 0:
            # The LLM used a name in a relation that is not in the entity list (prompt rule 7).
            # Leaving -1 makes it vanish silently in the graph export and halves 1-hop expansion.
            # The row is unusable, so it is dropped and the count is reported.
            dropped_rel += 1
            continue
        dids = resolve(v["docs"])
        # The name is taken **from the entity the id points at**.  v["s"] is the first spelling
        # seen, so case can diverge, as in 'Large Language Models' vs 'Large language models'.
        # The id is right but the display is not, so both come from one place and cannot diverge.
        rel_rows.append({"rel_id": len(rel_rows), "src_id": si, "tgt_id": ti,
                         "src_name": ent_rows[si]["name"], "tgt_name": ent_rows[ti]["name"],
                         "directed": False,
                         "keywords": sorted(v["kw"]),
                         "description": " ".join(dict.fromkeys(v["descs"]))[:2000],
                         "weight": 1.0, "doc_ids": dids,
                         "first_seen": min(v["seen"]) if v["seen"] else "",
                         "last_seen": max(v["seen"]) if v["seen"] else "",
                         "timeline": json.dumps(_fold_tl(v["tl"]), ensure_ascii=False),
                         "events": json.dumps(_fold_ev(v["ev"], flat2id),
                                              ensure_ascii=False),
                         "senses": json.dumps(v["sn"], ensure_ascii=False)})
        for i in (si, ti):
            if i >= 0:
                ent_rows[i]["degree"] += 1

    if dropped_rel:
        print(f"⑤ dropped {dropped_rel} relations with unresolved endpoints "
              f"(the LLM used a name in a relation that is not in the entity list)")

    # The rule for building the embedding string lives in **one place only** —— one character
    # of difference between reuse key and fresh encode and the cache never matches again.
    e_text = lambda e: "passage: " + f"{e['name']}: {e['description']}"
    r_text = lambda r: "passage: " + f"{r['src_name']} - {r['tgt_name']}: {r['description']}"
    ev = encode_reusing(m, [e_text(e) for e in ent_rows],
                        prev_vectors(db, "lr_entities", e_text) if db else {}, "entities")
    rv = encode_reusing(m, [r_text(r) for r in rel_rows],
                        prev_vectors(db, "lr_relations", r_text) if db else {}, "relations")
    for e, v in zip(ent_rows, ev):
        e["vector"] = v
    for r, v in zip(rel_rows, rv):
        r["vector"] = v
    return ent_rows, rel_rows


def prune_ghosts(ents, rels, live_doc_ids):
    """③ Clean up dangling references — remove vanished document ids from the arrays.

    chunk_ids was removed on 2026-08-19 (nothing read it), so chunks are not inspected.
    """
    n = 0
    for rows in (ents, rels):
        for r in rows:
            a = len(r["doc_ids"])
            r["doc_ids"] = [x for x in r["doc_ids"] if x in live_doc_ids]
            n += a - len(r["doc_ids"])
    return n


def _main_index():
    # Two writers on the same DB diverge silently (see the comments in kal_lock.py)
    with db_lock("schema_v3"):
        t_all = time.time()
        # Imported here rather than at the top —— sentence_transformers drags in torch, which
        # alone costs 5.8 seconds (measured).  status.py uses only SKIP, clean and VAULT from
        # this module, and a top-level import loaded all of torch just to fetch those three.
        # The reason the web UI's settings screen froze for 8 seconds every time.  (2026-08-19)
        from sentence_transformers import SentenceTransformer
        m = SentenceTransformer(MODEL)
        DIM = m.get_embedding_dimension()
        #  ⚠ The extraction output **may not exist.**  On a new vault this is the normal path ——
        #     setting KAL_VAULT and running the index first is the most natural opening move,
        #     and this used to die on FileNotFoundError.  To the user it looks like "did I
        #     configure something wrong" (nothing was wrong at all).
        #     Documents, chunks and search all build without a KG —— only entities/relations empty.
        _kgp = os.path.join(KAL_HOME, "lr_kg.json")
        if os.path.exists(_kgp):
            kg = json.load(open(_kgp))
        else:
            #  ⚠ The key names must be **identical** to what lr_extract actually writes.
            #     Writing `relations` here once killed build_graph with a KeyError ——
            #     an empty skeleton must be copied from the real output.  (measured 2026-08-24)
            kg = {"entities": [], "relationships": [], "chunks": 0,
                  "failed": 0, "doc_hashes": {}}
            print("   no knowledge graph — building documents, chunks and search only "
                  "(run `python src/lr_extract.py` first to add entities and relations)")
        db = lancedb.connect(DB)
        S = schemas(DIM)

        docs = scan_vault()
        # ⚠ This is **a report**, not a decision.  schema_v3 always rebuilds everything (that is
        #   this script's job —— sync_v3 does the incremental work).  The old label read
        #   "incremental sync decision", which made the numbers below read as "0 of them, so it
        #   will do nothing".  In fact it rebuilds all of it right afterwards.
        #
        #   A full rebuild is cheap now —— vectors are reused whenever the content is unchanged
        #   (prev_vectors): measured 501s → 25s.  So there is nothing to gain from branching
        #   here.  Instead of a branch, the label was made to match the facts.
        changes = diff_vault(docs, db)
        print("③ vault change summary (for reference —— everything below is rebuilt)")
        if changes["first_build"]:
            print(f"   first build — {len(docs)} documents")
        else:
            print(f"   added {len(changes['added'])} · modified {len(changes['modified'])} "
                  f"· deleted {len(changes['deleted'])} · renamed {len(changes['renamed'])} "
                  f"· unchanged {len(changes['unchanged'])}")
        print()

        # Note the stale marks present at the start.  **Only these** are cleared later —— marks
        # sync_v3 adds while the rebuild is running point at documents not yet extracted, so
        # they must survive.  (refresh_kg holds no lock across the lr_extract stage, so that
        # window really does open.  Rather than reworking the locking, the damage is contained
        # exactly.  Adversarial review, 2026-08-18)
        try:
            _stale_at_start = {r["doc_id"] for r in
                               db.open_table("stale_docs").search().limit(999999).to_list()}
        except Exception:
            _stale_at_start = set()

        chunks = build_chunks(docs, m, db)
        ixt, ixp, ixd = build_inverted(chunks)
        ents, rels = build_graph(kg, docs, chunks, m, db)
        ghosts = prune_ghosts(ents, rels, set(docs))

        meta = [{"key": k, "value": str(v)} for k, v in [
            ("embedding_model", MODEL), ("embedding_dim", DIM),
            ("chunk_chars", CHUNK), ("chunk_overlap", OVERLAP),
            ("fts_tokenizer", f"ngram({NGRAM[0]},{NGRAM[1]})"),
            ("bm25_k1", K1), ("bm25_b", B),
            ("doc_id_scheme", "crc32(path) & 0x7FFFFFFF"),
            ("chunk_id_scheme", "doc_id*10000 + seq"),
            ("relations_directed", "false (canonical sorted src/tgt)"),
            ("entity_types_canonical", "|".join(sorted(CANON_TYPES)) + "|other"),
            ("vault_path", VAULT), ("schema_version", "3"),
            ("built_at", int(time.time())), ("n_documents", len(docs)),
        ]]

        rowset = {"meta": meta,
                  "documents": [{k: v for k, v in x.items() if k != "_body"}
                                for x in docs.values()],
                  "chunks": chunks, "ix_terms": ixt, "ix_postings": ixp,
                  "ix_doclen": ixd, "lr_entities": ents, "lr_relations": rels}
        #  ⚠ **Refuse to overwrite the DB with nothing.**  Every table below is written with
        #     `mode="overwrite"`, unconditionally —— so `scan_vault()` returning {} silently
        #     replaced a populated database with empty rowsets, and `n_documents: 0` went into
        #     `meta` without complaint.  The only thing standing in the way was a guard in the Go
        #     API, in another process and another language: delete it and no Python check failed,
        #     and `just index` / `just sync` / `rebuild_all.sh` had **no guard at all**.  This is
        #     the same shape as `promote_distilled.would_shrink`, and for the same reason —— it
        #     belongs where the overwrite happens.  (adversarial review 2026-09-04, Q2)
        _prior = 0
        try:
            if "documents" in db.table_names():
                _prior = db.open_table("documents").count_rows()
        except Exception as e:
            #  Not knowing is not permission.  A broken read here must not read as "nothing to
            #  lose" —— that is how a silent failure turns into a deletion.
            raise SystemExit(f"❌ cannot tell what the DB holds, so refusing to overwrite it: {e}")
        if _prior and len(docs) < _prior * DB_SHRINK_RATIO and "--allow-shrink" not in sys.argv:
            raise SystemExit(
                f"❌ the vault yields {len(docs)} documents but the DB holds {_prior} —— "
                f"this rewrites every table, so it would throw away {_prior - len(docs)} of them.\n"
                f"   VAULT={VAULT}\n"
                f"   If the vault really did shrink, pass --allow-shrink.")

        TABLES = [(n, rowset[n]) for n in TABLE_ORDER]
        for name, rows in TABLES:
            tbl = db.create_table(name, mode="overwrite", schema=S[name], data=rows)
            made = []
            for col, kind in INDEXES.get(name, []):
                try:
                    tbl.create_index(col, replace=True, config=SCALAR_CFG[kind]()); made.append(col)
                except Exception as e:
                    made.append(f"{col}✗")
            if name in FTS_COL:
                try:
                    tbl.create_index(FTS_COL[name], replace=True, config=FTS(**FTS_KW)); made.append("FTS")
                except Exception:
                    made.append("FTS✗")
            print(f"  {name:14}{tbl.count_rows():>9,} rows   {' '.join(made)}")
        print(f"④ type normalisation: {len(set(e['type'] for e in ents))} kinds "
              f"(from {len(set(e['type_raw'] for e in ents))} raw)")
        print(f"④ name merge: entities {len(kg['entities'])} → {len(ents)} · "
              f"relations {len(kg['relationships'])} → {len(rels)}")
        print(f"③ dangling references removed: {ghosts}")

        # Clear only the marks this rebuild actually resolved.
        #
        # ⚠ Two things must hold at once.
        #   ① Clear **after every table is written**.  This used to happen before build_chunks,
        #      so a later build_graph failure left the KG stale with the marks already gone, and
        #      refresh_kg --check reported **"clean" forever**.
        #   ② Clear **only the starting snapshot**.  This used to drop_table the lot, wiping
        #      marks sync_v3 added mid-rebuild.  Those documents were not in this extraction,
        #      so their marks have to survive.
        # Is the vault the extraction saw the same one we just indexed?
        #
        # refresh_kg launches lr_extract then schema_v3, minutes apart.  A note edited in that
        # gap produces a DB holding **the new body with the old KG**, and since nobody marks
        # anything, refresh_kg --check reports "clean".  It is caught and marked here.
        _drift = []
        _snap = kg.get("doc_hashes") or {}
        if _snap:
            now_ts = int(time.time())
            for d in docs.values():
                was = _snap.get(d["path"])
                if was is not None and was != d["content_hash"]:
                    _drift.append({"doc_id": d["doc_id"], "path": d["path"],
                                   "reason": "modified", "marked_at": now_ts})
            missing = [p_ for p_ in _snap if p_ not in {d["path"] for d in docs.values()}]
            if _drift or missing:
                print(f"⚠ {len(_drift)} documents changed since extraction · {len(missing)} gone "
                      f"— marking them stale (the next refresh_kg re-extracts them)")
        else:
            #  Saying only "an old lr_kg.json" makes a new-vault user who has **not yet run**
            #  the extraction read it as "are my files stale".  The two cases are said apart.
            if not os.path.exists(os.path.join(KAL_HOME, "lr_kg.json")):
                print("   no timestamp comparison — the extraction has not been run yet (normal)")
            else:
                print("⚠ the extraction output has no doc_hashes (an old lr_kg.json) — skipping the timestamp comparison")

        if _stale_at_start or _drift:
            try:
                try:
                    rows = db.open_table("stale_docs").search().limit(999999).to_list()
                except Exception:
                    rows = []
                keep = [r for r in rows if r["doc_id"] not in _stale_at_start]
                # Documents whose timestamps disagree are marked anew (duplicates fold by doc_id)
                have = {r["doc_id"] for r in keep}
                keep += [r for r in _drift if r["doc_id"] not in have]
                if keep:
                    db.create_table("stale_docs", mode="overwrite",
                                    schema=S["stale_docs"], data=keep)
                    print(f"③ stale marks: {len(_stale_at_start)} of {len(rows)} resolved · "
                          f"{len(keep)} kept, having appeared during the rebuild")
                else:
                    db.drop_table("stale_docs")
            except Exception as e:
                print(f"  ⚠ could not tidy the stale marks (retried next run): {e}")
        # Clean up old versions.  Even overwritten with mode="overwrite", LanceDB keeps the
        # previous version's data files —— they pile up on every rebuild and grow without bound.
        # Measured 2026-08-18: after dozens of rebuilds, **1,466MB of 1,569MB (93%) was dead
        # versions**.  This DB is a derivative regenerated in full from the vault, so old
        # versions have no value.  24 hours are kept.  Reads take no lock (MVCC), so a handle
        # opened before the cleanup dies looking for data files that are gone —— an open REPL
        # or a long kal_search does exactly that.  LanceDB's own default is 7 days, and 1 hour
        # was excessively short next to it.  (adversarial review 2026-08-18, security lens)
        sz0 = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(DB) for f in fs)
        for name in db.list_tables().tables:
            try:
                db.open_table(name).optimize(cleanup_older_than=datetime.timedelta(hours=24))
            except Exception as e:
                print(f"  ⚠ could not optimise {name} (skipped): {e}")
        sz = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(DB) for f in fs)
        # "0MB reclaimed" usually does not mean **there is nothing to reclaim**; it means
        # **24 hours have not passed yet**.  Printed plainly it reads as if things were tidied.
        # Vector reuse took this stage from 501s to 25s, so it now runs several times a day and
        # each run stacks another version (measured ≈ +110MB per run).  So say why it did not shrink.
        freed = (sz0 - sz) / 1e6
        note = (f"reclaimed {freed:,.0f}MB of old versions" if freed >= 1 else
                "reclaimed 0 —— every remaining version is under 24 hours old (the retention window)")
        print(f"\ndisk {sz/1e6:.1f} MB ({note}) · total {time.time()-t_all:.0f}s")


def _selftest():
    """Vector reuse and the transmission gate —— both are the kind that **fail without an error**.

    Attach the wrong vectors and search merely gets quietly worse; let the gate leak and
    sensitive sentences go out with no sign at all.  So they are guarded here.

    Neither the model nor the DB is used —— a self-check must be fast and dependency-free.
    """
    # ── ① llm_gate policy ────────────────────────────────────────────
    B = {1, 2}
    assert llm_gate(None, set()) == "pass", "must pass when nothing is blocked"
    assert llm_gate([3], B) == "pass", "an unrelated document was blocked"
    assert llm_gate([1, 3], B) == "redact", "a partial block is not redacted"
    assert llm_gate([1], B) == "block", "a full block passes through"
    # **Unknown** provenance is blocked too.  The empty set is a subset of anything ——
    # the old `if ids and ...` guard is precisely what let this through.
    assert llm_gate([], B) == "block", "unknown provenance passes (the old bug)"
    assert llm_gate(None, B) == "block", "doc_ids of None passes through"

    # ── ② encode_reusing —— reuse what exists, encode only what does not ──
    class _V(list):                    # encode_reusing calls .tolist()
        def tolist(s): return list(s)
    class _M:
        def __init__(s): s.seen = []
        def encode(s, texts, **k):
            s.seen += list(texts)
            return [_V([float(len(t))]) for t in texts]  # deterministic fake vectors
    # Reused and freshly made values are kept **distinguishable**.  Vectors are built from
    # length, so the strings are given different lengths.
    m = _M()
    prev = {"aa": [99.0], "bbbb": [98.0]}          # stored values unrelated to length
    out = encode_reusing(m, ["aa", "ccc", "bbbb"], prev, "test")
    assert out == [[99.0], [3.0], [98.0]], f"order or values disagree: {out}"
    assert m.seen == ["ccc"], f"should encode only what is missing, but sent {m.seen}"

    m2 = _M()
    out2 = encode_reusing(m2, ["aa", "bbbb"], {}, "test")
    assert m2.seen == ["aa", "bbbb"], "an empty prev must encode everything"
    assert out2 == [[2.0], [4.0]], f"values disagree with an empty prev: {out2}"

    m3 = _M()
    out3 = encode_reusing(m3, ["aa", "bbbb"], prev, "test")
    assert m3.seen == [], "encoded even though everything was present"
    assert out3 == [[99.0], [98.0]], "reused, yet not the stored values"

    # ── ③ prev_vectors —— a different model **refuses reuse** ─────────
    #    Mixing vectors from another model makes search quietly worse with no error.
    class _T:
        def __init__(s, rows): s.rows = rows
        def search(s): return s
        def limit(s, n): return s
        def to_list(s): return s.rows
    class _DB:
        def __init__(s, model): s.model = model
        def open_table(s, n):
            if n == "meta":
                return _T([{"key": "embedding_model", "value": s.model}])
            return _T([{"text": "hi", "vector": [0.5]}])
    same = prev_vectors(_DB(MODEL), "chunks", lambda r: r["text"])
    assert same == {"hi": [0.5]}, f"same model, yet no reuse: {same}"
    other = prev_vectors(_DB("other/model"), "chunks", lambda r: r["text"])
    assert other == {}, "a different model still reuses vectors —— search breaks silently"

    class _Broken:
        def open_table(s, n): raise RuntimeError("no meta")
    assert prev_vectors(_Broken(), "chunks", lambda r: r["text"]) == {}, \
        "unreadable meta must not be reused"

    # ── ④ is the type vocabulary **the same in three places** ─────────
    #    lr_extract declares it in the prompt, this file normalises it, export gives it colour.
    #    Miss one and the type quietly becomes "other" or turns grey in the viewer ——
    #    with no error.  The move to 10 types (2026-08-21) really did need all three edited.
    import lr_extract as _L
    decl, canon = set(_L.ENTITY_TYPES), set(CANON_TYPES)
    colors = set(TYPE_COLOR) - {"other"}
    assert decl == canon, f"the prompt and the normaliser disagree: {decl ^ canon}"
    assert canon == colors, f"the normaliser and the viewer colours disagree: {canon ^ colors}"

    #    Whether the viewers use **the same object** is checked by identity.  Comparing values
    #    is not enough —— a copy is identical when made and drifts later.  It did drift: both
    #    export_webgl and export_graph were stuck on the old 7, leaving six canonical types
    #    (actor · artifact · decision · failure · metric · project) with no colour.
    #    artifact among them is the largest type (24.5%), so a quarter of the screen was grey.
    #    ⚠ Run as `python src/schema_v3.py`, this running file is `__main__` while the
    #      `schema_v3` the exports import is **a separate module object**.  So the reference is
    #      taken from the real module, not from `__main__`.  (Missing this at first produced a
    #      false positive: "export_kal_graph holds a copy".)
    import schema_v3 as _S
    for _m in ("export_kal_graph", "export_webgl", "export_graph"):
        _mod = __import__(_m)
        assert _mod.TYPE_COLOR is _S.TYPE_COLOR, \
            f"{_m} holds a **copy** of the palette —— it will drift eventually"
    assert _S.TYPE_COLOR == TYPE_COLOR, "the module's palette differs from __main__'s"

    #    Are **all** files that use the model under the offline pin?
    #    Without it every process pays a 5.1s round trip to huggingface.co (measured).  No
    #    error is raised —— it is merely slow.  So nobody looked, and kal_search's comment
    #    **asserted** this invariant while it was false on the host.
    # ── diff_vault —— **the single judgement** "did this document change" ─────────
    #    `touched = added | modified | renamed` (sync_v3:220) is what gets re-indexed.
    #    Let `modified` go quietly empty and `just sync` prints "modified 0 · unchanged 280 →
    #    no changes" and exits 0.  Every edit from that day on is never indexed, and search
    #    answers confidently from the old text.  No error, no staleness mark, and the output
    #    is character-for-character identical to a healthy no-change day.
    #    Replacing it with `modified = set()` still passed `just selftest` (measured, r4-guard).
    #    Neither the DB nor the model is used —— diff_vault takes plain dicts.
    class _NoDB:
        def open_table(self, _n):
            raise RuntimeError("this test does not use the DB")

    class _FakeDB:
        def __init__(s, rows): s.rows = rows
        def open_table(s, _n):
            class _T:
                def search(_s):
                    class _Q:
                        def limit(_q, _n2): return _q
                        def to_list(_q): return s.rows
                    return _Q()
            return _T()
    _row = lambda i, h, path: {"doc_id": i, "content_hash": h, "path": path}
    _new = {1: _row(1, "b", "a.md"), 2: _row(2, "same", "b.md")}
    _old = _FakeDB([_row(1, "a", "a.md"), _row(2, "same", "b.md")])
    _d = diff_vault(_new, _old)
    assert _d["modified"] == {1}, f"a changed document is missed: {_d['modified']}"
    assert _d["unchanged"] == {2}, f"an unchanged document is reported changed: {_d}"
    assert not _d["added"] and not _d["deleted"], f"add/delete detected wrongly: {_d}"
    #    A pure rename is renamed, not modified (it is not a re-extraction target)
    _d2 = diff_vault({3: _row(3, "h", "new.md")}, _FakeDB([_row(9, "h", "old.md")]))
    assert _d2["renamed"] == {9: 3} and not _d2["modified"], f"rename not separated: {_d2}"
    #    An unreadable DB must mean **everything is new** (not a silent zero)
    _d3 = diff_vault(_new, _NoDB())
    assert _d3["first_build"] and _d3["added"] == {1, 2}, f"a DB failure is swallowed: {_d3}"

    #    The file list is not written by hand —— the source is walked so **new entry points** are caught.
    import glob as _g, re as _re
    _src = os.path.dirname(os.path.abspath(__file__))
    for _f in sorted(_g.glob(os.path.join(_src, "*.py"))):
        _t = open(_f, encoding="utf-8").read()
        #  A direct `transformers` import counts too —— that is what `TRANSFORMERS_OFFLINE` is
        #  for.  A reranker or tokeniser probe arriving as a new entry point must be caught.
        #  (r4-guard V11: a file with only `from transformers import AutoModel` passed)
        _m = _re.search(r"^\s*(?:from|import) (?:sentence_)?transformers\b", _t, _re.M)
        if not _m:
            continue
        #  ⚠ Look for **the assignment**, not the word.  It started as `"HF_HUB_OFFLINE" in _head`,
        #    and the explanatory comment just above contained that word, so deleting the
        #    assignment still passed —— the guard was fooled by its own comment.  Comments are
        #    stripped, and only the real call is inspected.
        #  ⚠ Stripping comments alone is **not enough.**  This repository quotes code inside
        #    docstrings often, and the docstring right above the real pin explains this
        #    invariant in prose —— write the call text there and the guard is fooled again.
        #    (r4-guard V8b: deleting the pin and planting that text in the docstring passed.
        #     V8a —— deleting only the pin —— was caught.  The guard ran; the stripping was half-done.)
        _strip = r'"""[\s\S]*?"""' + r"|'''[\s\S]*?'''" + r"|#[^\n]*"
        _head = _re.sub(_strip, "", _t[:_m.start()])
        _covered = (_re.search(r"environ\s*\.\s*setdefault\s*\(\s*[\"']HF_HUB_OFFLINE",
                               _head)                                   # pins it itself
                    or _re.search(r"environ\s*\[\s*[\"']HF_HUB_OFFLINE", _head)
                    or _re.search(r"^\s*(?:from|import) schema_v3", _head, _re.M))  # pinned here
        assert _covered, (
            f"{os.path.basename(_f)} uses sentence_transformers with no offline pin ahead of "
            f"it —— this process round-trips to huggingface.co every time (about 5 seconds).")

    #    Identity types follow the same rule.  A name here that is not canonical leaves the
    #    **false all-clear** "0 real names" baked into HTML people hand out.  It happened (2026-08-21).
    stray = set(_S.IDENTITY_TYPES) - canon
    assert not stray, f"IDENTITY_TYPES holds non-canonical types {stray} —— the warning is permanently 0"
    assert _S.IDENTITY_TYPES, "identity types are empty —— the warning is permanently 0"
    for _m in ("export_graph", "export_webgl"):
        assert __import__(_m).IDENTITY_TYPES is _S.IDENTITY_TYPES, \
            f"{_m} holds a **copy** of the identity types"

    # A declared type must **pass through unchanged** (it must not fold into itself)
    folded = [t for t in _L.ENTITY_TYPES if norm_type(t) != t]
    assert not folded, f"a declared type folds away: {folded}"
    # Every TYPE_MAP destination must be valid
    bad = {k: v for k, v in TYPE_MAP.items() if v not in canon}
    assert not bad, f"TYPE_MAP points at a type that does not exist: {bad}"
    # The old scheme folds into the new one (so pre-re-extraction data still works)
    for _o, _e in (("pattern", "method"), ("person", "actor"), ("organization", "actor")):
        assert norm_type(_o) == _e, f"{_o} does not fold into {_e}"
    assert norm_type("anything-zzz") == "other", "an unknown type does not become other"

    # ── Self-copy detection —— must catch it **even when the code is outside the vault** ──
    #    Without this check the container (code /app · vault /vault) indexed 31 documents of a
    #    repository copy and sent them out through `claude -p`.  Checking only on the host
    #    never shows it —— on the host the path comparison works.  (2026-08-24)
    import tempfile as _tf, shutil as _sh
    _vault = _tf.mkdtemp()
    _copy = os.path.join(_vault, "any-name")        # to show that no name may be hardcoded
    os.makedirs(os.path.join(_copy, "src"))
    open(os.path.join(_copy, "src", "schema_v3.py"), "w").close()
    open(os.path.join(_copy, "justfile"), "w").close()
    os.makedirs(os.path.join(_vault, "notes"))
    _keep_vault = globals()["VAULT"]
    try:
        globals()["VAULT"] = _vault
        _self_copies.cache_clear()
        assert _self_copies() == frozenset({"any-name"}), _self_copies()
        assert is_skipped(os.path.join(_copy, "README.md")), "a repository copy was not caught"
        assert not is_skipped(os.path.join(_vault, "notes", "a.md")), "someone else's folder was excluded"
        #  One marker alone means someone else's folder —— it must not be claimed
        os.remove(os.path.join(_copy, "justfile"))
        _self_copies.cache_clear()
        assert _self_copies() == frozenset(), "one marker claimed someone else's folder"
    finally:
        globals()["VAULT"] = _keep_vault
        _self_copies.cache_clear()
        _sh.rmtree(_vault, ignore_errors=True)

    #  ── OKF keeps its date nested; this reader only ever saw flat keys ──────────────────
    #  Measured 2026-09-02: every page of the openwiki bundle returned ("", "none"), which does
    #  not error —— it removes the corpus from the time axis, so kal_timeline / as_of /
    #  since-until answer "nothing happened" instead of "I could not read the dates".
    _flow  = '---\ngenerated: { by: "process:x", at: "2026-01-02T00:00:00Z" }\n---\nb\n'
    _block = '---\ngenerated:\n  by: "process:x"\n  at: "2026-03-04T00:00:00Z"\n---\nb\n'
    assert doc_meta(_flow)[:2]  == ("2026-01-02", "generated.at"), doc_meta(_flow)
    assert doc_meta(_block)[:2] == ("2026-03-04", "generated.at"), doc_meta(_block)
    #  and it must not mistake a *different* assertion for the generation time.  `verified[].at`
    #  is when a human confirmed it and `sources[].last_modified` is about the source —— taking
    #  either would place the document on the time axis at a date nothing happened.
    _other = ('---\nverified:\n  - by: "human:t"\n    at: "2099-12-31T00:00:00Z"\n'
              'sources:\n  - resource: /x\n    last_modified: "1999-01-01"\n---\nb\n')
    assert doc_meta(_other)[:2] == ("", "none"), \
        "a verified/source date was taken for the generation time: %r" % (doc_meta(_other),)
    #  the flat vault keys keep working, and no_llm still reads through all of it.
    #  ⚠ The **whole 4-tuple**, not a slice.  Every assertion here used to slice `[:2]`/`[:3]`,
    #     which left `doc_updated` unguarded —— `upd = ""` survived as a mutation, and that field
    #     is what `effective_date` uses to stop a document being backdated.
    _flat = '---\nno_llm: true\ncaptured: 2026-04-25\nupdated: 2026-06-01\n---\nb\n'
    assert doc_meta(_flat) == ("2026-04-25", "captured", True, "2026-06-01"), doc_meta(_flat)

    #  The gate must not depend on **how** a person writes a true value.  `no_llm: true # private`
    #  is valid YAML and the obvious thing to type, and it used to leave the gate open with nothing
    #  said —— the document went to the LLM.  Both directions are checked: a false value with a
    #  comment must stay open, or the guard would block everything and be removed.
    #  (codex review 2026-09-02, blocker #8)
    #  Quoting the key or spacing the colon is ordinary YAML and used to open the gate silently.
    for _q in ('"no_llm": true', "'no_llm': true", "no_llm : true", 'no_llm: "true"',
               "NO_LLM: TRUE", "no_llm:\ttrue"):
        assert doc_meta(f'---\ntitle: "a"\n{_q}\n---\nx\n')[2], f"{_q} left the gate open"
    #  ...and the other way: an indented (nested) key, or a mention inside a value, must not gate.
    #  ⚠ A frontmatter that fails to parse must not read as consent.  Five ordinary mistakes
    #     each returned no_llm=False for a document whose author wrote the mark (2026-09-04).
    #     ⚠ The last two carry a BOM and a leading blank line.  Every earlier case begins with
    #        `---` at column zero, so `FM_OPEN_RE`'s `\ufeff?` was never exercised in the
    #        dangerous direction —— dropping it, and even making the opener match nothing at
    #        all, left this whole self-check green (measured 2026-09-04).
    for _broken in ("---\nno_llm: true\nbody\n", "---\nno_llm: true\n----\nbody\n",
                    "---\nno_llm: true\n--- x\nbody\n", " ---\nno_llm: true\n ---\nbody\n",
                    "---\nno_llm: true",
                    "\ufeff---\nno_llm: true\nbody\n", "\n---\nno_llm: true\nbody\n"):
        assert doc_meta(_broken)[2], f"a malformed frontmatter failed open: {_broken!r}"
    #     …and the other way: prose about the key is not a mark, and a file with no
    #     frontmatter attempt is untouched.  Without these the fix would block everything.
    for _openfm in ("---\ntitle: a\n---\n본문에 no_llm: true 라고 씁니다\n",
                    "no frontmatter\nno_llm: true\n",
                    "---\nno_llm: false\n---\nbody\n"):
        assert not doc_meta(_openfm)[2], f"prose about the key closed the gate: {_openfm!r}"
    #  ⚠ `classify_origin` reads the **frontmatter**, not the first 2,000 characters.  A note
    #     documenting the session URI —— at a line start, in a code block, or as a list item ——
    #     was classified `session` and vanished from `--origin vault`.  (2026-09-04)
    for _body in ('resource: "claude-session://abc"',
                  '```yaml\nresource: "claude-session://abc"\n```',
                  '- resource: "claude-session://abc"'):
        _t = '---\ntitle: 설명\ntype: note\n---\n\n' + _body + '\n'
        assert classify_origin('personal/wiki/x.md', _t) == 'vault', \
            f'a note documenting the URI was called a session: {_body!r}'
    #     …and a real session page is still recognised, or the fix would blank the origin filter.
    assert classify_origin('personal/wiki/x.md',
                           '---\ntitle: s\nsources:\n  - resource: "claude-session://a"\n---\nb\n'
                           ) == 'session', 'a real session page stopped being recognised'
    for _n2 in ("  no_llm: true", 'description: "no_llm: true"', "x_no_llm: true"):
        assert not doc_meta(f'---\ntitle: "a"\n{_n2}\n---\nx\n')[2], f"{_n2} closed the gate"
    for _y in ("true", "true # private", "true   # 사적", "yes", "on", "TRUE"):
        assert doc_meta(f'---\ntitle: "a"\nno_llm: {_y}\n---\nx\n')[2], f"no_llm: {_y} left the gate open"
    #  ⚠ `"true"` moved from this list to the blocking one on 2026-09-03.  It is *strictly* a YAML
    #     string, not the boolean —— but a person who writes it means "do not send this", and
    #     under-blocking a transmission gate is the failure that cannot be undone.  The emitter is
    #     still held to the bare form by okf_convert's own check, so nothing we write depends on it.
    for _n in ("false", "false # x", "maybe", "true-ish"):
        assert not doc_meta(f'---\ntitle: "a"\nno_llm: {_n}\n---\nx\n')[2], f"no_llm: {_n} closed the gate"

    #  ⚠ Order matters and was untested.  A page carrying **both** an OKF `generated.at` and a
    #     flat `captured:` must take the OKF one —— the flat key is a leftover from the vault
    #     original.  Measured on a real page, the two differ by eight months, so reading the wrong
    #     one silently moves the document that far on the time axis.
    _both = ('---\ngenerated: { by: "process:x", at: "2026-08-30T00:00:00Z" }\n'
             'captured: 2026-01-01\n---\nb\n')
    assert doc_meta(_both)[:2] == ("2026-08-30", "generated.at"), \
        "a flat leftover key outranked the OKF provenance: %r" % (doc_meta(_both),)

    #  ⚠ A malformed value must be refused, not passed through.  `_iso`'s `re.fullmatch` is the
    #     only thing stopping `captured: not-a-date` from becoming a document's date.
    assert doc_meta('---\ncaptured: not-a-date\n---\nb\n')[:2] == ("", "none")
    assert doc_meta('---\ngenerated:\n  at: "not-a-date"\n---\nb\n')[:2] == ("", "none")
    #  ── origin follows provenance, not the directory ─────────────────────────────────────
    #  An openwiki bundle keeps session records at `personal/sessions/…`, not the vault's
    #  `raw/conversations/sessions/`.  Keyed on the path, all 299 classified as `vault` and
    #  `kal_search(origin="session")` returned nothing (measured 2026-09-02).
    assert SESSION_URI.search('sources:\n  - resource: "codex-session://abc"\n'), \
        "a codex session URI is not recognised as session provenance"
    assert SESSION_URI.search('  - resource: claude-session://abc\n'), "unquoted form missed"
    assert not SESSION_URI.search('  - resource: "https://example.org/"\n'), \
        "an ordinary source URL was taken for session provenance"

    #  ── the kg export stays out of the index at ANY depth ─────────────────────────────────
    #  `SKIP_ROOT` used to look only at the first path segment.  Migrating the 699-page export to
    #  `personal/kg/` moved it out from under the guard while leaving the KG→note→KG feedback it
    #  exists to prevent exactly as real.
    _v = os.path.abspath(VAULT)
    assert is_skipped(os.path.join(_v, "kg", "x.md")), "the kg export is being indexed"
    assert is_skipped(os.path.join(_v, "personal", "kg", "x.md")), \
        "the kg export escapes the guard once it is nested one level down"
    assert not is_skipped(os.path.join(_v, "personal", "kgx", "x.md")), \
        "a directory merely starting with kg was skipped"
    #  the vendored upstream spec is not personal knowledge, and extracting it costs LLM calls
    assert is_skipped(os.path.join(_v, "references", "okf-SPEC-v0.2.md")), \
        "a vendored third-party specification is being indexed and extracted"
    assert not is_skipped(os.path.join(_v, "personal", "sessions", "claude", "x.md"))
    #  a generated index is navigation, and it outranked real documents when indexed
    assert is_skipped(os.path.join(_v, "personal", "sessions", "claude", "index.md")), \
        "a generated index.md is being indexed and competes with the documents it lists"
    assert not is_skipped(os.path.join(_v, "personal", "x", "index-of-things.md")), \
        "a real document whose name merely starts with index was skipped"

    #  the classification itself, so removing the provenance arm cannot pass unnoticed
    _sess = 'sources:\n  - id: "s1"\n    resource: "codex-session://abc"\n'
    assert classify_origin("personal/sessions/codex/x.md", "---\n" + _sess + "---\nb") == "session"
    assert classify_origin("raw/conversations/sessions/x.md", "---\ntitle: t\n---\nb") == "session"
    assert classify_origin("personal/wiki/concepts/x.md", "---\ntitle: t\n---\nb") == "vault"
    #  a session URI far down a long body must not reclassify an ordinary note
    assert classify_origin("personal/wiki/x.md", "---\ntitle: t\n---\n" + "x" * 3000
                           + "\nresource: claude-session://z\n") == "vault"

    # ── The rebuild must refuse to replace a populated DB with nothing ──────────────────
    #  ⚠ Run as a **subprocess against the real entry point**, not by calling the comparison.
    #     `promote_distilled`'s shrink guard was tested by calling the predicate, and replacing
    #     the production check with `if False:` still passed `just selftest` —— the guard was
    #     unreachable for months.  This drives `python schema_v3.py` with an empty vault against
    #     a DB that holds documents, and asserts the DB is still there afterwards.
    import subprocess as _sp, tempfile as _tf, lancedb as _ldb, pyarrow as _pa
    with _tf.TemporaryDirectory() as _d:
        _db = os.path.join(_d, "db")
        _c = _ldb.connect(_db)
        #  Enough rows that any sane ratio refuses going to zero.
        #  Built from the module's **own** schema —— a hand-written fixture missed `content_hash`
        #  and the run died inside `diff_vault` before ever reaching the guard, which would have
        #  read as "the guard fired".
        #  `DIM` is only known after loading the embedding model, and this fixture never reads a
        #  vector —— the guard counts rows and `diff_vault` reads `content_hash`.  Any width does.
        _sc = schemas(384)["documents"]
        _blank = {f.name: ("" if _pa.types.is_string(f.type)
                           else [] if _pa.types.is_list(f.type)
                           else 0.0 if _pa.types.is_floating(f.type) else 0)
                  for f in _sc}
        _c.create_table("documents", schema=_sc,
                        data=[{**_blank, "doc_id": i, "path": f"p{i}.md",
                               "content_hash": f"h{i}"} for i in range(50)])
        _r = _sp.run([sys.executable, os.path.abspath(__file__)],
                     env={**os.environ, "KAL_VAULT": os.path.join(_d, "empty"),
                          "KAL_PATH": _db, "KAL_HOME": _d},
                     capture_output=True, text=True)
        assert _r.returncode != 0, f"an empty vault rebuilt the DB anyway (exit {_r.returncode})"
        assert "would throw away" in _r.stdout + _r.stderr, \
            f"the refusal does not say what would be lost: {(_r.stdout + _r.stderr)[-300:]}"
        assert _ldb.connect(_db).open_table("documents").count_rows() == 50, \
            "the guard fired but the table was overwritten anyway"
        #  ⚠ **An unreadable DB must refuse too.**  "I could not tell what is there" and "there
        #     is nothing there" look identical to a caller, and treating the first as the second
        #     is how a read failure becomes a deletion.  Without this case, replacing the raise
        #     with `_prior = 0` passed the whole self-check (measured 2026-09-04).
        _tbl = os.path.join(_db, "documents.lance")
        _mode = os.stat(_tbl).st_mode
        os.chmod(_tbl, 0o000)
        try:
            _r3 = _sp.run([sys.executable, os.path.abspath(__file__)],
                          env={**os.environ, "KAL_VAULT": os.path.join(_d, "empty"),
                               "KAL_PATH": _db, "KAL_HOME": _d},
                          capture_output=True, text=True)
        finally:
            os.chmod(_tbl, _mode)
        assert _r3.returncode != 0, "an unreadable DB was overwritten anyway"
        assert "cannot tell what the DB holds" in _r3.stdout + _r3.stderr, \
            f"a read failure was not reported as one: {(_r3.stdout + _r3.stderr)[-300:]}"

        #  ⚠ And the escape hatch must actually work, or the guard becomes something people
        #     route around by deleting it.
        _r2 = _sp.run([sys.executable, os.path.abspath(__file__), "--allow-shrink"],
                      env={**os.environ, "KAL_VAULT": os.path.join(_d, "empty"),
                           "KAL_PATH": _db, "KAL_HOME": _d},
                      capture_output=True, text=True)
        assert "would throw away" not in _r2.stdout + _r2.stderr, \
            "--allow-shrink did not get past the guard"
    print("  ✅ a rebuild refuses to replace a populated DB with an empty vault (and --allow-shrink passes)")

    print("  ✅ origin from session provenance · kg export skipped at any depth")
    print("  ✅ doc_meta —— OKF generated.at (flow · block) · not a verified/source date · "
          "flat keys · no_llm")

    print(f"  ✅ schema_v3 self-check —— transmission gate · vector reuse · model guard · "
          f"type vocabulary agrees in 3 places ({len(canon)} kinds)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest(); sys.exit(0)
    # Record the run under ~/.kal/runs/ —— the web screen's "last run" only knew about runs
    # started from the web UI, so a CLI success still showed yesterday's failure as the last.
    from run_log import record
    with record("index"):
        _main_index()
