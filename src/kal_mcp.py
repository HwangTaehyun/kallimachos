#!/usr/bin/env python
"""kal_mcp — opens the personal knowledge DB through 5 MCP tools.  Design: docs/TEMPORAL_DESIGN.md §3

    kal_search     natural-language exploration (the main entry point)
    kal_entity     what a known name is + its sources + a change summary
    kal_timeline   the whole history of changes
    kal_neighbors  one hop in the graph
    kal_doc        citation verification — the source text

⚠ **MCP is the second outbound boundary.**
`schema_v3.SKIP` decides "do we index this locally"; `no_llm` decides "may this leave the
machine".  The two judgements are not the same (lr_extract.py:125-136).  Being indexed does
not make a document exportable, so every tool filters by `no_llm` again at query time.

⚠ **stdio only.**  It opens no network listener.  This repository's web UI and API have no
authentication, and loopback binding is the only defence (docs/STACK.md §7).  Opening a port
in MCP would collapse that premise.
"""
import vault_path
import json, logging, os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The embedding model is already cached locally.  But sentence-transformers checks
# huggingface for updates by default —— measured **13.2s → 4.7s**.  The main reason the
# first MCP search took 24.4 seconds, and a network dependency on the request path besides.
# (To change models, drop these two lines briefly and fetch once.)
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# This is a stdio server, so **stdout belongs to the protocol** —— one print breaks the
# session.  stderr is safe, and Claude Code keeps it in a file.  There used to be no log at
# all, so a swallowed exception left no trace anywhere.  Raise it with KAL_LOG=DEBUG.
logging.basicConfig(stream=sys.stderr, format="kal %(levelname)s %(message)s",
                    level=os.environ.get("KAL_LOG", "WARNING").upper())
log = logging.getLogger("kal")

from mcp.server.mcpserver import MCPServer

import kal_search as K
from entity_resolve import merge_key
from schema_v3 import FM_KEEP as _FM_KEEP, llm_gate, REDACTED, NO_LLM_RE, doc_meta

VAULT = vault_path.vault()

# Fields that must never ride in a response.  Blocked **per field**, not per path ——
# exposure is a question of which value, not of which folder.
#   abs_path        /Users/<user>/… absolute paths
#   vector          useless here and only inflates the response
#   session_*       encoded local paths · employer names · infrastructure topology (280 docs)
DENY_FM = ("session_id", "session_project", "distilled_from")
# Uses **the same fence** as schema_v3.doc_meta.  Let the two diverge and the gate opens.
from frontmatter import FM_RE      # the one fence —— see src/frontmatter.py
# What is actually used is an allowlist (kal_doc): schema_v3.FM_KEEP plus the time-axis keys
# —— a deny-list defaults to "a new key leaks", which makes it useless as a boundary.
FM_ALLOW = set(_FM_KEEP) | {"created", "updated", "captured", "generated_at",
                            "generated_by", "doc_type", "sources", "type"}

#  ⚠ An LLM calls these tools, so **the arguments cannot be trusted.**  A negative value
#     flips a Python slice and disables the cap entirely —— measured (deep review 2026-08-25):
#       kal_search(top=-1)      → 184 hits · 179KB (~45k tokens)
#       kal_neighbors(limit=-1) → 169 of them, while the response **lies** with truncated: true
#       kal_doc(max_chars=-1)   → all 25,800 characters, still saying truncated: true
#     So it is clamped at the door, and the truncation flag is computed from **the clamped value**.
def _clamp(v, lo, hi, dflt):
    try:
        v = int(v)
    except (TypeError, ValueError, OverflowError):
        #  OverflowError means `float('inf')` —— JSON accepts `Infinity`, so an LLM really
        #  can send it.  Uncaught, the whole tool dies.
        return dflt
    return max(lo, min(v, hi))


DOCS_CAP = 10          # measured: uncapped, kal_entity("obsidian") alone is 3.8k tokens
NEIGHBOR_CAP = 20      # attaching docs[] to all 143 relations comes to 24.4k tokens
CAND_CAP = 10

_db = None


def db():
    """Connect to the knowledge DB.  **When it is absent, fail in a sentence.**

    ⚠ Why the guard is here —— it started in `tbl()`, and a test caught "empty DB, yet
    success".  The real failure happens not in `tbl()` but in **`KAL.__init__`** (the
    constructor in `kal_search.py` opens `chunks` and `documents` immediately).  A guard in
    `tbl()` is never reached at all.
    """
    global _db
    if _db is None:
        try:
            _db = K.KAL()
        except Exception as e:
            if "not found" not in str(e).lower():
                raise
            raise NotIndexed(_not_indexed_msg()) from e
    return _db


class NotIndexed(RuntimeError):
    """The knowledge DB does not exist yet.  **Not a broken install — a next step to take.**"""


def _not_indexed_msg(name: str = "") -> str:
    """The empty-DB message.  The first sentence anyone who installed the plugin will read.

    lancedb's `ValueError: Table 'chunks' was not found` used to surface as-is —— that reads
    as "the install is broken", and what to do next is written nowhere.
    In reality **the index has simply not been run yet**.  (deep review 2026-08-23)
    """
    what = f"has no '{name}' table" if name else "has no index yet"
    return (
        f"The knowledge DB {what} —— the index has not been run.\n"
        f"  DB path: {os.environ.get('KAL_PATH', '(KAL_PATH unset)')}\n"
        f"  vault:   {os.environ.get('KAL_VAULT', '(KAL_VAULT unset)')}\n"
        f"  Run this once first:  python src/schema_v3.py\n"
        f"  In a container:  docker run --rm -v ~/.kal:/data -v <vault>:/vault:ro "
        f"ghcr.io/hwangtaehyun/kal:{_plugin_version()} src/schema_v3.py\n"
        #  ⚠ That image is **not on GHCR yet.**  README §Install ① says so, but someone who
        #     installed via the plugin does not read the README —— this sentence is the first
        #     guidance they see.  Following it verbatim gives `manifest unknown`.
        #     (deep review 2026-08-28)
        f"  ⚠ That image is not on a public registry yet.  Build it from the repository first:\n"
        f"     just build-kal && docker tag kal:local "
        f"ghcr.io/hwangtaehyun/kal:{_plugin_version()}"
    )


def tbl(name):
    """KAL holds its lancedb connection in self.db.  No dedicated accessor is added.

    `db()` has already verified `chunks` and `documents`, so what reaches here is a missing
    KG table (the extraction has not been run).  It is reported in the same sentence.
    """
    try:
        return db().db.open_table(name)
    except NotIndexed:
        raise
    except Exception as e:
        if "not found" not in str(e).lower():
            raise
        raise NotIndexed(_not_indexed_msg(name)) from e


# ─────────────────────────── provenance ───────────────────────────

def _docs_index():
    """doc_id → document metadata for responses.  no_llm documents are **never included.**"""
    out = {}
    for d in tbl("documents").to_arrow().to_pylist():
        if d.get("no_llm"):
            continue
        out[d["doc_id"]] = {
            "doc_id": d["doc_id"], "path": d["path"], "title": d.get("title", ""),
            "date": d.get("doc_date", ""), "date_src": d.get("date_src", "none"),
            "updated": d.get("doc_updated", ""), "origin": d.get("origin", ""),
        }
    return out


_DOCS = None
_BLOCKED = None       # doc_ids of no_llm documents.  Used to block derived text
_ENTS = None          # every entity.  resolve()'s partial-match path used to read this each time
_STAMP = None         # the DB timestamp when the cache was built


#  The tables the sensor watches.  **What the cache holds + the completion marker (meta).**
#
#  It used to watch `documents` alone.  But the build writes documents 2nd and `lr_entities`
#  7th —— for roughly 3 seconds in between, the stamp is **already final** while the entity
#  table is still the old one.  Fill `_ENTS` inside that window and the stamp never changes
#  again, so **for the server's whole life** it serves old entities.  (measured by r2-verify:
#  a 2.97-3.35s window; cache at v152 with disk at v157, and 30 fresh() calls never invalidated)
#
#  It was not a security problem —— `blocked()` reads `documents`, which is itself the sensor,
#  so the no_llm gate was always current.  What broke was name resolution (merge_key/candidates).
#
#  Cost: documents alone 0.83ms → these three 1.80ms (mean of 200 runs, measured).  fresh()
#  runs on every tool call, so 1ms is affordable.  All 8 tables cost 5.10ms, which is too
#  much (ix_postings has 2.1M rows and is expensive, and no cache holds it).
STAMP_TABLES = ("meta", "documents", "lr_entities")


def _db_stamp(db_dir=None):
    """When was the DB last updated.  The cheapest way to notice a re-index.

    ⚠ **Never read the mtime of the `documents.lance` directory itself.**  It holds no files
    at the top level, only subdirectories, so updates are not reflected —— measured, the
    sensor had been **frozen for 3.3 days**.  lance writes a manifest into `_versions/` on
    every commit, so the newest timestamp in there is what gets read.
    """
    best = 0.0
    for name in STAMP_TABLES:
        for sub in ("_versions", "data"):
            d = os.path.join(db_dir or DB_DIR, f"{name}.lance", sub)
            try:
                best = max([best, os.path.getmtime(d)] +
                           [os.path.getmtime(os.path.join(d, f)) for f in os.listdir(d)])
            except OSError:
                pass
    return best


def fresh():
    """Drop the cache when it is stale.

    Without this, a re-index leaves the old document list in use **until the server restarts**.
    That is not merely slow —— **the `no_llm` gate goes stale**, and documents newly marked as
    excluded from transmission keep going out.  A security problem.
    """
    global _DOCS, _BLOCKED, _ENTS, _STAMP
    st = _db_stamp()
    if _STAMP is not None and st != _STAMP:
        _DOCS = _BLOCKED = _ENTS = None
    _STAMP = st


def blocked():
    global _BLOCKED
    if _BLOCKED is None:
        _BLOCKED = {d["doc_id"] for d in tbl("documents").to_arrow().to_pylist()
                    if d.get("no_llm")}
    return _BLOCKED


def _with_blocked(ids):
    """For tests —— swap the blocked set.

    The real vault has **zero** `no_llm: true` documents, so the gate never executes.  That
    leaves the self-check unable to verify the gate, and in a mutation test replacing
    `if ids <= blocked()` with `if False` really did pass.
    """
    global _BLOCKED
    old, _BLOCKED = _BLOCKED, set(ids)
    return old


def gate(row):
    """Block the **derived text** of entities and relations.

    Filtering the document list alone is not enough —— description and timeline extracted from
    a no_llm document go out with docs[] empty: **sensitive sentences with no source**.  Worse.

    Returns: (row, None) to pass · (None, response) to block
    """
    # The judgement lives in `schema_v3.llm_gate` alone —— export_kal_graph uses the same one.
    # Duplicating the policy makes it diverge, and the diverged side is the one that leaks.
    # (The `ids and` in the `if ids and ids <= b` that used to live here let **entities of
    #  unknown provenance** straight through.  The empty set is a subset of anything.)
    v = llm_gate(row.get("doc_ids"), blocked())
    if v == "block":
        return None, {"matched": None, "error": "blocked",
                      "hint": "every source document for this entity is excluded from transmission (no_llm)."}
    if v == "redact":
        row = dict(row)
        row["description"] = REDACTED
        row["timeline"] = "[]"
    return row, None


# Repository convention: KAL_HOME + KAL_PATH (same as kal_search and schema_v3).
_KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
DB_DIR = os.environ.get("KAL_PATH", os.path.join(_KAL_HOME, "db"))


def docs_of(ids, cap=DOCS_CAP):
    """Provenance.  **Always carried in every entity and relation response.**

    "look it up with another tool if you need to" fails —— models mostly do not make that
    second call, and an unsourced claim goes out as it is.

    There is a cap, but **the response records that it truncated.**  Without that, the model
    believes 10 is all there is.
    """
    global _DOCS
    if _DOCS is None:
        _DOCS = _docs_index()
    rows = [_DOCS[i] for i in ids if i in _DOCS]
    rows.sort(key=lambda r: r["date"] or "", reverse=True)      # newest first
    out = {"docs": rows[:cap], "docs_total": len(rows),
           "docs_truncated": len(rows) > cap}
    if len(rows) > cap:
        # Saying "truncated" with no way to reach the rest is a dead end.  ids are cheap (66 ≈ 253 tokens).
        out["doc_ids_all"] = [r["doc_id"] for r in rows]
    return out


_SRC_RE = re.compile(r"^sources:\s*(.+)$", re.M)
SLUG_RE = re.compile(r"[\w-]{1,64}")          # no path separators, no dots
SRC_DIR = os.path.realpath(os.path.join(VAULT, "wiki", "sources"))
_URL_RE = re.compile(r"https?://[^\s)\]>\"']+")


def refs_of(doc_ids):
    """External references.  **This is a two-hop join.**

        frontmatter `sources: ["s1:<slug>"]`  ← an internal slug.  Not a URL.
          → wiki/sources/<slug>.md
            → the URL in **that note's body**

    Measured: of the 59 documents carrying `sources:`, 0 have a URL in the frontmatter.  The
    first design read URLs from the frontmatter, which would have returned empty forever.

    refs_status separates "no sources" (none) from "could not resolve" (unresolved) —— with
    both as an empty array, a model cannot tell "no basis" from "I could not find it".
    """
    global _DOCS
    if _DOCS is None:
        _DOCS = _docs_index()
    slugs, unresolved = [], False
    for i in doc_ids[:DOCS_CAP]:
        d = _DOCS.get(i)
        if not d:
            continue
        f = os.path.join(VAULT, d["path"])
        try:
            head = open(f, encoding="utf-8", errors="ignore").read(1500)
        except OSError:
            continue
        m = _SRC_RE.search(head.split("\n---", 1)[0])
        if m:
            # `:` must be in the character class —— without it "s1:slug" splits into "s1" and
            # "slug", and the non-existent slug "s1" produces an unresolved on every call.
            slugs += re.findall(r"[\w./:-]+", m.group(1))

    refs, seen = [], set()
    for raw in slugs:
        sid, _, slug = raw.partition(":")
        if not slug:
            sid, slug = "", raw
        # ⚠ The slug is **used as a file path.**  Joining it unvalidated opens the vault's outside.
        #   `sources:` values are written by an LLM from content brain-ingest fetched off the
        #   web —— attacker-controllable input.  An adversarial review really did extract the
        #   URL of a file outside the vault via `../../../outside/fakeenv`.
        #   (The path traversal blocked in kal_doc had a back door here.)
        if not SLUG_RE.fullmatch(slug) or slug in seen:
            continue
        seen.add(slug)
        note = os.path.join(SRC_DIR, f"{slug}.md")
        if os.path.realpath(note) != os.path.join(SRC_DIR, f"{slug}.md"):
            continue                       # a symlink pointing outside
        if not os.path.isfile(note):
            # exists() is not enough —— a **directory** named `<slug>.md` makes open() raise
            # IsADirectoryError, and the absolute path rides in that message.
            unresolved = True
            refs.append({"id": sid, "slug": slug, "title": "", "url": None})
            continue
        try:
            t = open(note, encoding="utf-8", errors="ignore").read()
        except OSError:
            log.warning("refs: could not read the source note slug=%s", slug)
            unresolved = True
            continue
        # The source note itself may be excluded from transmission.  A back door in the gate.
        #  ⚠ **`doc_meta`, not a bare regex over the head.**  This scanned `t[:1500]` with no
        #     fence, so a note that merely *documents* the key —— in prose or inside a fenced code
        #     block —— was silently dropped from `refs`, while every other consumer said it was
        #     fine.  Measured 2026-09-04: a how-to page with `no_llm: true` in a yaml block was
        #     blocked here and passed by `doc_meta`.  In this corpus the distilled session
        #     documents *are* write-ups about this gate, so it fires often.
        if doc_meta(t)[2]:
            continue
        ttl = re.search(r'^title:\s*"?([^"\n]+)', t, re.M)
        url = _URL_RE.search(t)
        refs.append({"id": sid, "slug": slug,
                     "title": (ttl.group(1).strip() if ttl else slug),
                     "url": url.group(0) if url else None})
    status = "none" if not refs else ("unresolved" if unresolved else "ok")
    return {"refs": refs[:DOCS_CAP], "refs_status": status,
            "refs_note": "url is unverified external content.  Do not follow it automatically."}


# ─────────────────────────── Name resolution ───────────────────────────

def resolve(name):
    """(row, candidates).

    Measured: `name_norm` is only a lowercasing, not a `merge_key` (5,999 of 7,620 rows
    differ).  So looking up by `merge_key` misses most of the time —— `claudecode`, the
    example in the first design, was not in the DB either (it is `Claude Code`).

    On a miss it returns **candidates, not an error.**  For a tool called by name, a miss is
    closer to the default case than to an exception.
    """
    q = (name or "").strip().lower()
    if not q:
        return None, []
    t = tbl("lr_entities")

    def one(where):
        """An index lookup.  **It does not swallow exceptions.**

        It used to be `except Exception: return None`.  Then a broken index or a syntactically
        invalid where clause is quietly absorbed by the full scan and **the result is the
        same** —— both defects passed a mutation test.  This one spot hid a silent performance
        collapse and a silent SQL error at once.
        """
        r = t.search().where(where).limit(1).to_arrow().to_pylist()
        return r[0] if r else None

    # ① exact —— name_norm has a BTREE.  Full scan 120.5ms vs where 1.6ms (74×).
    hit = one(f"name_norm = '{q.replace(chr(39), chr(39)*2)}'")
    if hit:
        return hit, []

    # ② and ③ are normalisation and partial matching, which no index can narrow.  The whole
    # table must be read —— **hence the cache.**  Reading it each time costs 140.8ms to
    # materialise 7,620 rows, which was 95% of the 229.1ms partial-match path.  Exact match
    # is 1.9ms, so only the "candidate list" path the docstring advertises was 120× slower.
    #
    # Invalidation is already handled by `fresh()`'s stamp (0.2ms per call).  It has the same
    # lifetime as `_DOCS` and `_BLOCKED` —— not a new slot, one more rider on what exists.
    global _ENTS
    if _ENTS is None:
        _ENTS = t.to_arrow().to_pylist()
    rows = _ENTS
    mk = merge_key(q)
    for r in rows:
        if merge_key(r.get("name") or "") == mk:
            return r, []
    cands = [r for r in rows if q in (r.get("name_norm") or "")]
    cands.sort(key=lambda r: -(r.get("degree") or 0))
    return None, [{"name": c["name"], "type": c.get("type", ""),
                   "degree": c.get("degree", 0),
                   "doc_count": len(c.get("doc_ids") or [])}
                  for c in cands[:CAND_CAP]]


def miss(name, cands):
    """A failure **always carries an error key**.

    One rule throughout —— `error` present means failure; absent means a usable answer.
    A miss used to carry no error, so a model's natural check (`if "error" in r`) let a name
    miss pass as a success.  `hits: []` is not a failure, so it gets no error.
    """
    hint = ("pass one of the candidates back **verbatim**." if cands
            else "not even a similar name exists.  Find it with kal_search(query) first.")
    return {"error": "name_not_found", "matched": None, "query": name,
            "candidates": cands, "hint": hint}


DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def bad_date(**kw):
    """**Never let a malformed date through quietly.**

    Comparison is on strings, so as_of="May 2026" sorts above every change and "2026-13-99"
    filters as if it were a valid date.  Both give a wrong answer with no symptom.
    """
    for k, v in kw.items():
        if v and not DATE_RE.fullmatch(v):
            return {"error": "bad_date", "arg": k, "got": v,
                    "expected": "YYYY-MM-DD"}
    return None


def events_of(row, until=None, since=None, cap=20):
    """Raw fragments as dated **events**.  The detail a summary threw away lives here.

    A different axis from `timeline`:
        timeline   "what **changed**"           an LLM verdict · measured, present on 0.85%
        events     "when did it **say what**"   verbatim · present on 99.3%

    The point is to let the consuming LLM read it **for itself** rather than trust our
    summariser's verdict.  So nothing is blurred.
    """
    try:
        ev = json.loads(row.get("events") or "[]")
    except Exception:
        log.warning("events JSON failed to parse name=%s", row.get("name"))
        return [], 0
    if since:
        ev = [c for c in ev if c.get("at", "") >= since]
    if until:
        ev = [c for c in ev if c.get("at", "") <= until]
    return ev[-cap:], len(ev)      # the newest cap.  Same direction as _fold_ev


def timeline_of(row, until=None, since=None):
    tl = []
    try:
        tl = json.loads(row.get("timeline") or "[]")
    except Exception:
        log.warning("timeline JSON failed to parse name=%s", row.get("name"))
        tl = []
    if since:
        tl = [c for c in tl if c.get("at", "") >= since]
    if until:
        tl = [c for c in tl if c.get("at", "") <= until]
    return tl


# ─────────────────────────── Tools ───────────────────────────

def _plugin_version() -> str:
    """Read `.claude-plugin/plugin.json` as the source of truth.

    Why read it here —— a version in two places will diverge.  The manifest is what the host
    reads and `serverInfo.version` is what the server says, and when they differ there is no
    telling which to believe.  Measured (2026-08-23): this was empty and the host read no version.
    """
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        j = os.path.join(os.path.dirname(here), ".claude-plugin", "plugin.json")
        with open(j, encoding="utf-8") as fh:
            return json.load(fh).get("version") or "0.0.0"
    except Exception:
        # Failing to read a version is no reason for the server not to start.
        return "0.0.0"


app = MCPServer(name="kal", version=_plugin_version(), instructions=(
    "Search a personal knowledge DB.  Every entity and relation response always carries its "
    "source documents (docs) and external references (refs) —— quote them directly.\n"
    "Time arguments: as_of = one **state** at that point (kal_entity).  "
    "since/until = the **list of changes** in that range (kal_timeline)."))


@app.tool(description=(
    "Search the knowledge DB in natural language.  The main entry point when you do not know "
    "what you are looking for.  If you already know a name, use kal_entity instead."))
def kal_search(query: str, top: int = 20, origin: str | None = None) -> dict:
    """origin: 'vault' (notes written by hand) | 'session' (distilled from a conversation) | None (all)"""
    top = _clamp(top, 1, 50, 20)
    fresh()
    if origin not in (None, "vault", "session"):
        return {"error": "bad_origin", "got": origin,
                "expected": ["vault", "session", None]}
    global _DOCS
    if _DOCS is None:
        _DOCS = _docs_index()
    # search() returns a 3-tuple (rows, mode, weights), not a dict.
    # Treating it as a dict meant **every call was dying** —— and the self-check passed
    # because it called no tools at all.  _selftest now calls all five.
    rows, _mode, _w = db().search(query, top=top, origin=origin)
    rows = [r for r in rows if r.get("doc_id") in _DOCS]     # no_llm excluded
    ids = [r["doc_id"] for r in rows]
    # rows carry no body text.  Evidence fragments come separately from snippets().
    # snippets() returns **a {doc_id: [text]} dict** (kal_search.py:236).
    # Iterated as a list it yields int keys and blows up in int.get, and the old version
    # swallowed that, so **every search always returned empty excerpts** —— the main entry
    # point offered 0 sentences to quote.  Same family as the 3-tuple bug.  No longer swallowed.
    snip = {d: [t[:400] for t in (v or [])][:2]
            for d, v in (db().snippets(query, ids) or {}).items()}
    hits = [{**_DOCS[r["doc_id"]],                # abs_path is not in here (deny-list)
             "score": r.get("score", 0),
             "snippets": snip.get(r["doc_id"], [])}
            for r in rows]
    return {"query": query, "hits": hits, "hit_count": len(hits),
            "note": "for an entity's identity or time axis, pick a name from the hits' titles or bodies and call kal_entity(name)."}


@app.tool(description=(
    "Profile, sources and change summary for an entity you can name.  An inexact name returns candidates.\n"
    "With as_of='YYYY-MM-DD' it returns **one state at that point**.  "
    "For a **list** of changes, as in 'when did it change', use kal_timeline."))
def kal_entity(name: str, as_of: str | None = None) -> dict:
    """as_of: 'YYYY-MM-DD'.  The state up to that point.  Omitted means now."""
    fresh()
    bad = bad_date(as_of=as_of)
    if bad:
        return bad
    row, cands = resolve(name)
    if row is None:
        return miss(name, cands)
    row, blk = gate(row)
    if blk:
        return blk
    ids = list(row.get("doc_ids") or [])
    tl = timeline_of(row, until=as_of)
    out = {"matched": row["name"], "type": row.get("type", ""),
           "description": row.get("description", ""),
           "degree": row.get("degree", 0),
           "first_seen": row.get("first_seen", ""),
           "last_seen": row.get("last_seen", ""),
           "change_count": len(tl),
           "latest_change": tl[-1] if tl else None,
           # The bodies are not carried —— up to 122 of them would blow up the response.
           # Only the count is reported; kal_timeline supplies them when needed.
           "events_total": events_of(row)[1],
           **docs_of(ids), **refs_of(ids)}
    if as_of:
        out["as_of"] = as_of
        # Not counting changes **after** as_of would lie that "then and now are the same".
        # timeline_of(until=as_of) does not look past it, so it is checked separately here.
        after = [c for c in timeline_of(row) if c.get("at", "") > as_of]
        if row.get("first_seen") and as_of < row["first_seen"]:
            out["note"] = f"it had not appeared yet at this point (first seen {row['first_seen']})."
        elif after:
            out["changes_after"] = after
            out["note"] = (f"⚠ **{len(after)} changes have been recorded since this point.** "
                           "description is the **current** state and therefore differs from then.  "
                           "Read changes_after and work backwards.")
        else:
            out["note"] = ("description is the **current** state.  No changes have been recorded "
                           "since this point —— though it may not have been a change-extraction target.")
    return out


@app.tool(description=(
    "The **complete list of changes** an entity went through over time.  "
    "since/until='YYYY-MM-DD' narrows the range.  "
    "For **one state** at a given point, use kal_entity(name, as_of=…).\n"
    "Most entities have no changes, so an empty list is the common result."))
def kal_timeline(name: str, since: str | None = None, until: str | None = None) -> dict:
    """since/until: 'YYYY-MM-DD'."""
    fresh()
    bad = bad_date(since=since, until=until)
    if bad:
        return bad
    row, cands = resolve(name)
    if row is None:
        return miss(name, cands)
    row, blk = gate(row)
    if blk:
        return blk
    ids = list(row.get("doc_ids") or [])
    tl = timeline_of(row, until=until, since=since)
    ev, ev_total = events_of(row, until=until, since=since)
    return {"matched": row["name"],
            "first_seen": row.get("first_seen", ""),
            "last_seen": row.get("last_seen", ""),
            "timeline": tl, "change_count": len(tl),
            # Even with 0 changes there are events —— measured, timeline 0.85% vs events 99.3%.
            # This is the axis that can answer "what was known about it then".
            "events": ev, "events_total": ev_total,
            "events_truncated": ev_total > len(ev),
            "events_note": (
                "rev=current is that document's **present** content; superseded is its earlier content.  "
                "at_exact=false means the date is **an estimate** —— only the day the document "
                "was created, not the day it changed.  Records before 2026-08-20 are like this."),
            # Change extraction ran only on entities with 8 or more description fragments (3.4%).
            # So an empty list must not be asserted as "it did not change" —— it may simply not
            # have been a target, and the current schema cannot tell the two apart.
            "note": ("0 changes —— but **read events.** The change verdict ran only on entities "
                     "with 8 or more fragments (3.4% of them), so 0 means either 'it did not "
                     "change' or 'it was never assessed'.  events is verbatim, so you can "
                     "judge for yourself." if not tl else ""),
            **docs_of(ids), **refs_of(ids)}


@app.tool(description=(
    "Whatever an entity is directly connected to (one hop in the graph), with the relation "
    "descriptions.  min_degree filters out passing mentions (default 1).  limit defaults to 20."))
def kal_neighbors(name: str, min_degree: int = 1, limit: int = NEIGHBOR_CAP) -> dict:
    """min_degree: filter out passing mentions.  limit: cap on neighbours (default 20)."""
    #  The cap is NEIGHBOR_CAP itself.  Setting hi to 100 nullifies that constant's rationale
    #  ("attaching docs[] to all 143 relations comes to 24.4k tokens") ——
    #  measured (2026-08-25, round 2): limit=100 gives 42.5KB (~12k tokens).
    limit = _clamp(limit, 1, NEIGHBOR_CAP, NEIGHBOR_CAP)
    min_degree = _clamp(min_degree, 0, 1000, 1)
    fresh()
    row, cands = resolve(name)
    if row is None:
        return miss(name, cands)
    row, blk = gate(row)
    if blk:
        return blk
    global _DOCS
    if _DOCS is None:
        _DOCS = _docs_index()
    eid = row["entity_id"]

    # Narrowed by index.  This used to materialise all 12,251 lr_relations rows and all
    # 7,620 lr_entities rows into Python objects **on every call** —— measured 185ms·108MB
    # + 103ms·68MB.  At 10× the documents that alone becomes 2.1 seconds and 1GB.
    #   relation lookup  158.5ms → 8.3ms  (19×)
    #   degree           102.8ms → 6.3ms  (16×, neighbours only)
    rels = tbl("lr_relations").search().where(
        f"src_id = {eid} OR tgt_id = {eid}").to_arrow().to_pylist()
    nb_ids = sorted({r["tgt_id"] if r["src_id"] == eid else r["src_id"] for r in rels})
    deg, blocked_ents = {}, set()
    if nb_ids:
        # Too long an IN clause breaks the query.  It is sent in batches.
        for i in range(0, len(nb_ids), 500):
            chunk = ",".join(str(x) for x in nb_ids[i:i + 500])
            for e in tbl("lr_entities").search().where(
                    f"entity_id IN ({chunk})").to_arrow().to_pylist():
                deg[e["entity_id"]] = e.get("degree") or 0
                # A neighbour's name is derived text too.  Degree alone must not leak a name.
                eids = set(e.get("doc_ids") or ())
                if blocked() and (not eids or eids <= blocked()):
                    blocked_ents.add(e["entity_id"])

    out = []
    for r in rels:
        other_id = r["tgt_id"] if r["src_id"] == eid else r["src_id"]
        if other_id in blocked_ents:
            continue                       # the neighbouring **entity** is itself excluded
        if deg.get(other_id, 0) < min_degree:
            continue
        raw_ids = r.get("doc_ids") or []
        rids = [i for i in raw_ids if i not in blocked()]
        if blocked() and (not raw_ids or not rids):
            # A relation whose sources are all blocked, or **entirely absent**.  It used to be
            # `raw_ids and …`, so a source-less relation always passed.
            continue
        # A thin source rides **along with it**.  Ids alone meant 5 kal_doc calls per citation.
        src = [{"doc_id": i, "path": _DOCS[i]["path"], "date": _DOCS[i]["date"]}
               for i in rids[:3] if i in _DOCS]
        item = {"name": r["tgt_name"] if r["src_id"] == eid else r["src_name"],
                "degree": deg.get(other_id, 0),
                "relation": (r.get("description") or "")[:220],
                "docs": src}
        # A relation's **state transition**.  An axis an entity timeline cannot express.
        # Measured, only 11 of 12,251 have a value, so it rides only when present —— attaching
        # an empty array every time only inflates the response and reads as "no changes".
        rtl = timeline_of(r)
        if rtl:
            item["timeline"] = rtl
        out.append(item)
    out.sort(key=lambda x: -x["degree"])
    return {"matched": row["name"], "neighbors": out[:limit],
            "neighbor_total": len(out), "neighbors_truncated": len(out) > limit,
            "note": "read the source text of a relation's documents with kal_doc(doc_id).",
            **docs_of(list(row.get("doc_ids") or [])),
            **refs_of(list(row.get("doc_ids") or []))}


@app.tool(description=(
    "Read the source text to verify a citation.  doc_id comes from the docs[] of other tools."))
def kal_doc(doc_id: int, max_chars: int = 4000) -> dict:
    """It takes no path.

    This repository has already met and fixed a vulnerability of this exact shape
    (docs/STACK.md §7 — `GET /api/runs/..%2F..%2F..%2Ftmp%2Fproof` returned file contents
    verbatim).  A free-form path is not reopened.  A doc_id exists only for indexed
    documents, which makes it an allowlist in itself.
    """
    max_chars = _clamp(max_chars, 200, 20000, 4000)
    fresh()
    global _DOCS
    if _DOCS is None:
        _DOCS = _docs_index()
    d = _DOCS.get(doc_id)
    if not d:
        return {"error": "not_found",
                "hint": "the document is not indexed, or is blocked from transmission.  Use a doc_id from docs[] verbatim."}
    #  ⚠ `d["path"]` comes **from the index**.  Locally that index is ours; in the cloud it is
    #     a file the user uploaded —— an absolute path or `..` makes os.path.join drop VAULT.
    #     Measured (deep review 2026-08-29, llm-tool lens): "../secret.txt" in a forged index
    #     came back as content.  realpath checks VAULT; if not, it takes the missing-file path.
    _root = os.path.realpath(VAULT)
    _p = os.path.realpath(os.path.join(_root, d["path"]))
    try:
        if not _p.startswith(_root + os.sep):
            raise OSError("path escapes vault")
        t = open(_p, encoding="utf-8", errors="ignore").read()
    except OSError:
        # Passing the exception string through sends a /Users/<user>/… absolute path to the
        # LLM —— which nullifies the rule that blocks abs_path.
        #
        # There is one place with no source text —— the cloud.  Only the index (db/) is
        # uploaded there, never the notes.  In that case **an approximation stitched from the
        # chunks** is returned: chunks passed through masking before indexing and carry no
        # frontmatter, so no allowlist is needed.  no_llm documents are not in _DOCS at all
        # and never reach here.  A file deleted locally goes the same way —— indexed but missing means the chunks are its last form.
        rows = [r for r in tbl("chunks").search().where(f"doc_id = {int(doc_id)}").limit(100000).to_list()]
        if not rows:
            return {"error": "unreadable", "doc_id": doc_id}
        t = "\n\n".join(r["text"] for r in sorted(rows, key=lambda r: r.get("seq", 0)))
        return {**d, "content": t[:max_chars], "truncated": len(t) > max_chars,
                "total_chars": len(t), "source": "chunks",
                "hint": "the source file is gone, so the index's chunks were stitched together.  Sentences may overlap at the seams."}
    m = FM_RE.match(t)
    if t.lstrip("\ufeff").lstrip().startswith("---") and not m:
        # An opening `---` that fails to parse = there is frontmatter and it cannot be read.
        # Sending it anyway **nullifies the allowlist and the no_llm recheck at once** and the
        # whole source text goes out (reproduced with a BOM, a `...` terminator, and a missing
        # closing `---`).  Block rather than open.
        log.warning("frontmatter failed to parse doc_id=%s — refusing to send", doc_id)
        return {"error": "unparsable_frontmatter", "doc_id": doc_id,
                "hint": "the frontmatter cannot be read, so nothing is sent."}
    if m:
        # This is an **allowlist**.  A deny-list breaks two ways —— a new key simply goes out,
        # and in block/list style only the key line is removed while the value lines remain.
        keep, take = [], False
        for ln in m.group(1).replace("\r", "").split("\n"):
            if ln[:1] in (" ", "\t", "-"):
                # A value continuation line.  But **a key can appear here too** ——
                # indented as `  session_project: …` it inherited the previous allowed key
                # and went straight out.  Anything key-shaped is re-checked.
                k = re.match(r"[ \t]*-?[ \t]*([\w-]+)\s*:", ln)
                if k and k.group(1).lower() not in FM_ALLOW:
                    take = False
                    continue
                if take:
                    keep.append(ln)
                continue
            take = ln.split(":", 1)[0].strip().lower() in FM_ALLOW
            if take:
                keep.append(ln)
        t = "---\n" + "\n".join(keep) + "\n---\n" + t[m.end():]
        # The index may be stale.  The source's no_llm mark is checked again **at read time**.
        if NO_LLM_RE.search(m.group(1)):
            return {"error": "blocked", "doc_id": doc_id,
                    "hint": "this document carries the no_llm (excluded from transmission) mark."}
    return {**d, "content": t[:max_chars],
            "truncated": len(t) > max_chars, "total_chars": len(t)}


def _selftest():
    """A plumbing check.  It reads the DB and never writes.

    ⚠ The old version **called no tools at all.**  So it passed while `kal_search` died on
    every call (treating a 3-tuple return as a dict).  All five are now called —— a
    self-check that cannot fail is not a check.
    """
    # ── Name resolution ──
    assert resolve("")[0] is None, "an empty name must be a miss"

    #  ⚠ **The refs gate reads the frontmatter, not the head of the file.**  It used to be
    #     `NO_LLM_RE.search(t[:1500])` with no fence, so a note that merely *documents* the key
    #     —— in prose or inside a fenced code block —— was silently dropped from `refs` while
    #     every other consumer passed it.  In this corpus the distilled session documents are
    #     write-ups about this very gate, so it fired often.  (reproduced 2026-09-04)
    _marked = '---\ntitle: a\nno_llm: true\n---\n본문\n'
    _talks  = '---\ntitle: how-to\n---\n```yaml\nno_llm: true\n```\n'
    assert doc_meta(_marked)[2], "a marked note stopped being gated"
    assert not doc_meta(_talks)[2], "a note documenting the key was gated"
    import inspect as _insp
    assert "NO_LLM_RE.search(t[" not in _insp.getsource(sys.modules[__name__]), \
        "the refs path went back to scanning the head of the file instead of the frontmatter"
    row, _ = resolve("obsidian")
    assert row, "if obsidian is not found, name resolution is broken"
    assert resolve("claudecode")[0], "the merge_key path is broken (name_norm alone misses)"
    assert resolve("no-such-entity-zzz")[0] is None

    # ── Path traversal (an adversarial review read a file outside the vault) ──
    for bad in ("../../../etc/passwd", "..", "a/b", "x.y", "../secret"):
        assert not SLUG_RE.fullmatch(bad), f"slug validation lets {bad!r} through"
    assert SLUG_RE.fullmatch("agent-termination-patterns")

    # ── Date validation (string comparison quietly returns the wrong era) ──
    assert bad_date(as_of="May 2026"), "a malformed date passes"
    assert bad_date(as_of="2026-05"), "a partial date passes"
    assert bad_date(as_of="2026-05-01") is None

    # ── Does the index path **actually** produce an answer ──
    # In the old version a failing `one()` was absorbed by the full scan, so the check passed
    # even with a broken index.  The index path is called on its own.
    _t = tbl("lr_entities")
    _probe = _t.search().where("name_norm = 'obsidian'").limit(1).to_arrow().to_pylist()
    assert _probe, "the name_norm index lookup produces no answer"

    # ── Does a single quote break the where clause ──
    # Without escaping it becomes `name_norm = 'it's …'` and the SQL blows up.
    # The except then swallows it and the full scan absorbs it, so **the result looks fine.**
    # Hence an escaped string is thrown at it directly.
    # one() no longer swallows exceptions, so a missing escape makes resolve blow up.
    assert resolve("it's not there")[0] is None, "a quoted name matched the wrong thing"

    # ── Does the transmission gate **actually** block ──
    # The real vault has 0 no_llm documents, so without injection this code never executes.
    # A blocked set is planted so all three branches are walked.
    _e = resolve("obsidian")[0]
    _ids = list(_e.get("doc_ids") or [])
    assert len(_ids) >= 2, "this check needs an entity with 2 or more documents"
    _old = _with_blocked(_ids)                      # ① everything blocked
    try:
        _r, _b = gate(_e)
        assert _r is None and _b and _b.get("error") == "blocked", "a full block does not take"
        _with_blocked(_ids[:1])                     # ② only some blocked
        _r, _b = gate(_e)
        assert _b is None and "excluded from transmission" in (_r.get("description") or ""), "a partial block does not redact the body"
        assert _r.get("timeline") == "[]", "partially blocked, yet the timeline survived"
        _with_blocked([])                           # ③ nothing blocked
        _r, _b = gate(_e)
        assert _b is None and _r is _e, "nothing is blocked, yet the original is withheld"
        # ④ An entity of **unknown** provenance.  Passing it while a block is in force makes
        #    it a sensitive sentence with 0 sources —— exactly what the gate exists to stop.
        _with_blocked([-1])
        _r, _b = gate({"name": "x", "doc_ids": [], "description": "a potentially sensitive sentence"})
        assert _r is None and _b and _b.get("error") == "blocked", \
            "an entity with empty doc_ids passes straight through while a block is in force"
    finally:
        globals()["_BLOCKED"] = _old

    # ── An unreadable frontmatter must **block** (fail closed) ──
    # An adversarial review nullified the allowlist and the no_llm recheck at once in four
    # ways: a BOM, a `...` terminator, whitespace after the opening line, and no closing `---`.
    for _bad in ("﻿---\nno_llm: true\nsession_project: X\n---\nbody",
                 "---\nno_llm: true\n...\nbody",
                 "--- \nno_llm: true\n---\nbody",
                 "---\nno_llm: true\nsession_project: X\nbody"):
        # Three of them (BOM, `...` terminator, whitespace after the opening line) **the fence
        # must catch**.  A narrow fence returns None here, and then no_llm goes unseen.
        if "body" in _bad and _bad.count("---") >= 2 or "..." in _bad:
            _m = FM_RE.match(_bad)
            if _bad.rstrip().endswith("body") and ("---\nbody" in _bad or "...\nbody" in _bad):
                assert _m, f"the fence cannot read {_bad[:14]!r} —— no_llm leaks through"
                assert "no_llm" in _m.group(1), "the fence missed no_llm"

    # ── Smuggling a key by indenting it ──
    _fm = "title: t\n  session_project: LEAK\ntags: x\n"
    _keep, _take = [], False
    for _ln in _fm.split("\n"):
        if _ln[:1] in (" ", "\t", "-"):
            _k = re.match(r"[ \t]*-?[ \t]*([\w-]+)\s*:", _ln)
            if _k and _k.group(1).lower() not in FM_ALLOW:
                _take = False; continue
            if _take: _keep.append(_ln)
            continue
        _take = _ln.split(":", 1)[0].strip().lower() in FM_ALLOW
        if _take: _keep.append(_ln)
    assert not any("LEAK" in x for x in _keep), "an indented denied key passes through"

    # ── Does the DB-update sensor **actually** move ──
    # The old version read the mtime of the `documents.lance` directory, which holds no files
    # at the top level, and was measured frozen for 3.3 days.  Checking the comparison misses this.
    _st = _db_stamp()
    assert _st > 0, "the DB timestamp cannot be read"
    import os.path as _op
    #  ⚠ It used to compare against `documents.lance` **alone**, because that was all the
    #    sensor watched.  Now it watches all of `STAMP_TABLES`, and `meta` is written **last**
    #    in the build, so comparing against documents alone fails on a few seconds of skew
    #    even when healthy (4 seconds measured).  The comparison set matches the sensor's.
    #    The walk is deliberately different (entry mtimes at the table root), not a reimplementation.
    _real = max(_op.getmtime(_op.join(DB_DIR, f"{t}.lance", x))
                for t in STAMP_TABLES
                for x in os.listdir(_op.join(DB_DIR, f"{t}.lance")))
    assert abs(_st - _real) < 1, \
        f"the sensor cannot see the real update ({_real:.0f}) —— it says {_st:.0f}"

    # ── Do **all five** tools go through fresh() ──
    # Miss one and that tool alone uses a stale blocked set.  The result looks the same.
    _seen = []
    _orig_fresh = globals()["fresh"]
    globals()["fresh"] = lambda: (_seen.append(1), _orig_fresh())[1]
    try:
        for _f, _a in ((kal_entity, ("obsidian",)), (kal_timeline, ("obsidian",)),
                       (kal_neighbors, ("obsidian",)), (kal_doc, (999999,)),
                       (kal_search, ("x",))):
            _n = len(_seen)
            _f(*_a)
            assert len(_seen) - _n == 1, \
                f"{_f.__name__} calls fresh() {len(_seen)-_n} times (expected 1)"
    finally:
        globals()["fresh"] = _orig_fresh

    # ── When a re-index is detected, is **the entity cache** cleared too ──
    # `_ENTS` rides on the stamp cache to stop partial-match resolve() from reading 7,620
    # rows every time.  But nothing checked that it was ever invalidated —— a mutation that
    # deleted only `_ENTS = None` from `fresh()` **passed the self-check**
    # (2026-08-21).
    #
    # Miss it and MCP keeps returning vanished entities after a re-index, with no error.
    # `_DOCS` and `_BLOCKED` are the no_llm gate, which is worse; they are guarded too.
    globals()["_ENTS"] = ["polluted"]
    globals()["_DOCS"] = {"polluted": 1}
    globals()["_BLOCKED"] = {-1}
    globals()["_STAMP"] = -1.0            # so the next fresh() must see "it changed"
    fresh()
    for _nm in ("_ENTS", "_DOCS", "_BLOCKED"):
        assert globals()[_nm] is None, f"{_nm} is not cleared when a re-index is detected"
    assert resolve("obsidian")[0], "resolve broke after the cache was invalidated"

    # ── Is cache invalidation actually wired up ──
    # The old version checked with _DOCS empty and therefore **always passed**.  Fill it first.
    global _STAMP, _DOCS
    fresh()
    _docs_index() if _DOCS is None else None
    _DOCS = _DOCS or _docs_index()
    assert _DOCS, "the document index is empty"
    _STAMP = _db_stamp() - 1        # pretend the DB has changed
    fresh()
    assert _DOCS is None, "a re-index goes unnoticed —— the no_llm gate goes stale"
    _DOCS = _docs_index()           # restored for the checks that follow

    # ── **Actually** call all five tools ──
    e = kal_entity("obsidian")
    assert e.get("matched"), f"kal_entity failed: {str(e)[:120]}"
    assert "docs" in e and "refs_status" in e, "provenance is missing"
    assert all("abs_path" not in d for d in e["docs"]), "abs_path leaked"
    assert e["docs_total"] >= len(e["docs"]), "the truncation flag is inverted"
    if e["docs_truncated"]:
        assert e.get("doc_ids_all"), "truncated with no way to reach the rest"
    # Check directly that the truncation flag **turns on**.  Hardcoding False still passes,
    # because the if above is simply skipped —— so an input that really truncates is given.
    _d = docs_of(list(row.get("doc_ids") or []), cap=1)
    if _d["docs_total"] > 1:
        assert _d["docs_truncated"] is True, "truncated, yet truncated is False"
        assert len(_d["docs"]) == 1 and _d.get("doc_ids_all")
    _d0 = docs_of(list(row.get("doc_ids") or [])[:1], cap=10)
    assert _d0["docs_truncated"] is False, "not truncated, yet truncated is True"

    t = kal_timeline("obsidian")
    assert "timeline" in t and isinstance(t["timeline"], list)
    assert kal_timeline("obsidian", since="nonsense").get("error") == "bad_date"

    # A failure always carries an error key —— a model's `if "error" in r` is the only rule
    _m = kal_entity("no-such-entity-zzz")
    assert _m.get("error") == "name_not_found", "a name miss carries no error key"
    assert _m.get("hint"), "a miss offers no next action"

    # Does as_of avoid lying that "then and now are the same"
    _c = resolve("claude code")[0]
    _tl = timeline_of(_c)
    if len(_tl) >= 2:
        _early = _tl[0]["at"]
        _r = kal_entity("claude code", as_of=_early)
        assert _r.get("changes_after"), "changes after as_of are not reported"
        assert "since this point" in (_r.get("note") or ""), "the note does not flag later changes"

    n = kal_neighbors("obsidian", limit=3)
    assert "refs_status" in n, "refs is missing from neighbors (the promise is that it always rides)"
    assert len(n.get("neighbors", [])) <= 3, "limit does not take"
    # Does the relation filter actually narrow?  Scanning everything explodes the neighbour
    # count —— and limit hides that, so it looks healthy from outside.
    _all = tbl("lr_relations").count_rows()
    assert 0 < n["neighbor_total"] < _all / 4, \
        f"{n['neighbor_total']} neighbours — the relation filter does not narrow (total {_all})"
    _self = resolve("obsidian")[0]["entity_id"]
    assert all(x["name"].lower() != "obsidian" for x in n["neighbors"]), "the entity is its own neighbour"

    q = kal_search("obsidian", top=3)
    assert "hits" in q and "error" not in q, f"kal_search failed: {str(q)[:160]}"
    # Are the excerpts **actually** filled?  snippets() is a dict, and the version that
    # iterated it as a list gave an empty array on every search while the except swallowed it.
    assert any(h.get("snippets") for h in q["hits"]), "every excerpt is empty"
    assert all("abs_path" not in h for h in q["hits"]), "abs_path leaked into the search results"
    assert kal_search("x", origin="nonsense").get("error") == "bad_origin"

    if e["docs"]:
        d = kal_doc(e["docs"][0]["doc_id"])
        assert "content" in d, f"kal_doc failed: {str(d)[:120]}"
        head = d["content"][:800]
        for k in DENY_FM:
            assert f"\n{k}:" not in head, f"{k} leaked"
    assert kal_doc(999999).get("error") == "not_found"

    # ── The timeline filter ──
    # ⚠ This line **deliberately** makes `timeline JSON failed to parse name=None` appear on
    #   stderr above.  A warning mixed into a passing log is not a defect —— the real DB is
    #   clean (0 parse failures across 7,620 entities and 12,246 relations, measured 2026-08-21).
    #   name is None because what is passed here is a minimal dict with no name.
    assert timeline_of({"timeline": "broken-json"}) == [], "a parse failure must give an empty list"
    tl = [{"at": "2026-01-01", "change": "a"}, {"at": "2026-06-01", "change": "b"}]
    assert len(timeline_of({"timeline": json.dumps(tl)}, until="2026-03-01")) == 1
    assert len(timeline_of({"timeline": json.dumps(tl)}, since="2026-03-01")) == 1

    # ── Does the sensor notice a change in lr_entities ──────────────────
    #    It used to watch documents alone.  The build writes documents 2nd and lr_entities
    #    7th —— for about 3 seconds in between the stamp is already final while the entities
    #    are still old.  A cache filled in that window is **never invalidated again.**
    #    The real DB is left alone —— this runs against a fake directory.
    import tempfile
    with tempfile.TemporaryDirectory() as _d:
        def _touch(tbl, off):
            v = os.path.join(_d, f"{tbl}.lance", "_versions")
            os.makedirs(v, exist_ok=True)
            f = os.path.join(v, "1.manifest")
            open(f, "w").close()
            #  ⚠ utime on the files alone is not enough —— `_db_stamp` also reads
            #    **directory mtimes** (which is why this function was fixed in the first
            #    place: on a table holding only subdirectories, file mtimes do not move).
            #    Leave the directory and its creation time (= now) overrides the synthetic value.
            os.utime(f, (off, off)); os.utime(v, (off, off))
        base = 1_700_000_000
        for _t in STAMP_TABLES:
            _touch(_t, base)
        s0 = _db_stamp(_d)
        _touch("lr_entities", base + 10)          # documents is left untouched
        s1 = _db_stamp(_d)
        assert s1 > s0, "only lr_entities changed and the sensor did not move —— the cache stays stale"
        _touch("meta", base + 20)
        assert _db_stamp(_d) > s1, "the sensor misses a change to meta (the completion marker)"
    #    The logic above only holds while meta is written **last**
    import schema_v3 as _S3
    assert _S3.TABLE_ORDER[-1] == "meta", \
        f"meta is not last ({_S3.TABLE_ORDER[-1]}) —— it stops being a completion marker"
    assert set(STAMP_TABLES) <= set(_S3.TABLE_ORDER), \
        f"the sensor watches a table that does not exist: {set(STAMP_TABLES) - set(_S3.TABLE_ORDER)}"


    # ⑭ An empty knowledge DB must fail **in a sentence**.
    #
    #    Anyone who installed the plugin starts with no DB.  lancedb's
    #    `ValueError: Table 'chunks' was not found` used to surface as-is from there, and that
    #    reads as "the install is broken".  In reality the index has simply not been run.
    #    (deep-review 2026-08-23)
    #    ⚠ Changing `os.environ["KAL_PATH"]` **does not work** —— `K.KAL()`'s default path is
    #      a constant kal_search froze at import time, so changing env afterwards still looks
    #      at the real DB.  Written that way once, it was caught as "empty DB, yet success".
    #      Only **connecting directly** to an empty path reaches the guard.
    import tempfile as _tf
    global _db
    _saved_db = _db
    try:
        with _tf.TemporaryDirectory() as _empty:
            _db = None
            _saved_ctor = K.KAL
            K.KAL = lambda *a, **k: _saved_ctor(path=os.path.join(_empty, "db"))
            try:
                db()
                raise AssertionError("tbl() succeeded on an empty DB —— the guard is dead")
            except NotIndexed as _e:
                _m = str(_e)
                #  ⚠ Loose search terms pass **by appearing on some other line**.  It started
                #    as ("index", "schema_v3.py", "KAL_PATH"), and deleting the diagnosis
                #    sentence still matched via `what`, while deleting the action sentence
                #    still matched via the docker example.  **Diagnosis and action are split.**
                for _need in (
                    "the index has not been run",     # diagnosis —— what is wrong
                    "Run this once first",            # action —— what to do about it
                    "schema_v3.py",                   # the actual command for that action
                    "KAL_PATH",                       # where it is looking
                ):
                    assert _need in _m, f"the empty-DB message lacks '{_need}': {_m[:160]}"
            except AssertionError:
                raise
            except Exception as _e:
                raise AssertionError(
                    f"an empty DB leaked as {type(_e).__name__} rather than NotIndexed —— "
                    f"whoever installed it sees a traceback: {_e}")
    finally:
        _db = _saved_db
        K.KAL = _saved_ctor

    # ⑭-b **A path in the index cannot read outside the vault.**  In the cloud the index is a user upload.
    #      A real file must exist outside for this to test anything —— with no file, "escape failed" and "no such file" look alike.
    import tempfile as _tf
    with _tf.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as _out:
        _out.write("CANARY-OUTSIDE-VAULT-6f1c\n")
    try:
        _rel = os.path.relpath(_out.name, VAULT)          # ../../…/tmp/x.md
        for _bad in (_rel, _out.name):
            _saved_doc = _DOCS
            _DOCS = {**(_DOCS or {}), 987654321: {"doc_id": 987654321, "path": _bad, "title": "", "date": "", "date_src": "none", "updated": "", "origin": ""}}
            try:
                _r = kal_doc(987654321)
            finally:
                _DOCS = _saved_doc
            assert "CANARY-OUTSIDE-VAULT" not in json.dumps(_r, ensure_ascii=False), f"a file outside the vault was read: {_bad!r}"
    finally:
        os.unlink(_out.name)

    # ⑮ The host must have a version to read.  An empty string hides plugin updates.
    #
    #    ⚠ Checking `_plugin_version()` alone **is not enough** —— the helper can return the
    #      right value and the host still gets an empty string if it is never passed to
    #      MCPServer.  Written that way once, it missed a mutation deleting `version=`.
    _v = getattr(app, "version", None)
    assert _v, f"serverInfo.version is empty —— version= was never passed to MCPServer: {_v!r}"
    assert _v == _plugin_version(), \
        f"the version the server states ({_v}) differs from the manifest ({_plugin_version()})"
    import json as _json, os.path as _op
    _mj = _op.join(_op.dirname(_op.dirname(_op.abspath(__file__))), ".claude-plugin", "plugin.json")
    assert _v == _json.load(open(_mj, encoding="utf-8"))["version"], \
        "serverInfo.version and plugin.json have diverged —— there is no telling which to believe"

    print(f"  ✅ self-check passed — all 5 tools called · {row['name']} · "
          f"docs {e['docs_total']} · refs {e['refs_status']} · search {q['hit_count']} hits · "
          f"empty-DB message · v{_v}")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        app.run(transport="stdio")
