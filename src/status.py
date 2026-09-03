#!/usr/bin/env python3
"""The knowledge DB's current state and **what to run next**.

Why it lives in one place
  If the CLI, the web UI and cron each make the same judgement, they give different
  answers.  They did —— refresh_kg.py looked at the stale ratio, the README at
  hand-written numbers, and a person at file dates.  One copy of the judgement lives here.

What is inspected (all measured, nothing guessed)
  ① vault file mtime  vs  documents.indexed_at    → has indexing fallen behind
  ② documents.content_hash vs the real file hash  → did the content change (more exact than mtime)
  ③ stale_docs                                    → documents whose KG is stale
  ④ doc_hashes in lr_kg.json vs the vault now     → has extraction fallen behind
  ⑤ artifact (graph_export · galaxy.html) mtime vs meta.built_at → has the export fallen behind

Usage:
    python status.py            a table for people
    python status.py --json     for machines (the web API uses this)
"""
import argparse
import collections
import glob
import hashlib
import json
import os
import re
import pathlib
import sys
import time


# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
#  Above this share of the indexed documents disappearing at once, `sync` is not advised ——
#  the run is more likely to be looking at a vault that is not fully there than at a
#  deletion the user made.  See the note at the computation.
VAULT_SHRINK_RATIO = 0.5

KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DB = os.environ.get("KAL_PATH", os.path.join(KAL_HOME, "db"))
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# Must use the same value as refresh_kg.py.  It is imported from there.
try:
    from refresh_kg import WARN_RATIO
except Exception:
    WARN_RATIO = 0.20


def _dirsize(p):
    if not os.path.isdir(p):
        return 0
    return sum(os.path.getsize(os.path.join(r, f))
               for r, _, fs in os.walk(p) for f in fs)


def _mtime(p):
    return int(os.path.getmtime(p)) if os.path.exists(p) else 0


def _fs_free():
    """Which filesystem the DB and **the temporary directory** each sit on, and how much is left.

    They may not be the same place —— in the container the DB (`/data/kal`) is a bind mount
    pointing at the host while `/tmp` is the image overlay.  LanceDB spills to `/tmp` on large
    queries, so **a full overlay kills it regardless of how much room the DB side has.**

    It died exactly that way (2026-08-21): 46GB free on the host · 0 on the overlay →
        panicked … failed to create temp directory for LocalSpillStore: StorageFull

    This screen said only "447 old versions … free" at the time —— all it took stock of was
    `_dirsize(DB)`, so it had no eye on the filesystem that actually filled up.
    """
    import shutil as _sh
    import tempfile as _tf
    out, seen = {}, set()
    for name, path in (("db", DB), ("tmp", _tf.gettempdir())):
        try:
            u = _sh.disk_usage(path)
            dev = os.stat(path).st_dev
        except OSError:
            continue                      # no path: simply drop it (only the advice is lost)
        #  The same **device** counts once —— on the host the DB and /tmp are one filesystem,
        #  so the identical advice appeared twice with different paths.  In a container they differ.
        if dev in seen:
            continue
        seen.add(dev)
        out[name] = {"path": path, "free": u.free, "total": u.total,
                     "pct_used": round(u.used / u.total * 100, 1) if u.total else 0.0}
    return out


def _empty_status(tables):
    """The state when there is no knowledge DB yet.  **The shape is exactly the usual one.**

    ⚠ It first returned a **different shape**, `{"db":…, "empty":True, "recommend":[…]}`.
      The CLI was fine, but the web threw reading `d.indexed`, `kg.entities` and
      `db.embedding_model.split()`, and `web/src` had no ErrorBoundary, so **the screen
      went entirely blank** —— a Python exception relocated into a white browser page
      (2026-08-25, round 6).

    So the shape stays and only the values go to 0, with `empty: True` added.  Readers need
    change nothing, and anything wanting to show a notice looks at that one field.
    """
    from schema_v3 import VAULT          # the same source collect() uses
    return {
        "empty": True,
        "db": {
            "path": DB, "tables": {t: 0 for t in tables}, "disk_bytes": _dirsize(DB),
            "fs": _fs_free(), "old_versions": 0, "built_at": 0,
            "embedding_model": "", "schema_version": "", "vault_path": "",
            "vault_now": os.environ.get("KAL_VAULT_HOST") or VAULT,
        },
        "documents": {"indexed": 0, "origin": {}, "added": [], "modified": [], "deleted": [],
                      #  The empty-DB state must carry every field the live one does ——
                      #  the web reads them unguarded and a missing key blanks the screen.
                      "vault_absent": False, "vault_shrunk": False},
        "kg": {"entities": 0, "relations": 0, "stale": [], "stale_ratio": 0.0,
               "stale_error": "", "drift_error": "", "warn_ratio": WARN_RATIO,
               "extracted_at": 0, "extract_drift": 0},
        "artifacts": {"mtimes": {}, "stale": []},
        "checked_at": int(time.time()),
    }


def collect():
    import lancedb
    from schema_v3 import SKIP, is_skipped, clean, VAULT

    db = lancedb.connect(DB)
    tables = sorted(db.list_tables().tables)
    #  ⚠ **Having run nothing yet** is a normal state.  This used to die here with
    #     `ValueError: Table 'meta' was not found` —— `just status` is what `just setup`
    #     points at as the next step, the README recommends it four times, and **two issue
    #     templates ask for its output.**  The first command a fresh install types spat out
    #     an untraceable Python exception.  (deep review 2026-08-25, newcomer lens)
    need = {"meta", "documents"}
    if not need <= set(tables):
        return _empty_status(tables)
    counts = {t: db.open_table(t).count_rows() for t in tables}
    meta = {r["key"]: r["value"] for r in
            db.open_table("meta").search().limit(99).to_list()}
    built_at = int(meta.get("built_at", 0) or 0)

    docs = db.open_table("documents").search().limit(999999).to_list()
    by_path = {d["path"]: d for d in docs}
    origin = collections.Counter(d.get("origin", "vault") for d in docs)

    # ── Walk the vault for real and compare against the index ─────────────
    # mtime alone counts a touched file as changed and misses editors that leave mtime
    # alone.  The judgement is by content hash (the same formula schema_v3 stored).
    seen, added, modified = set(), [], []
    for f in sorted(glob.glob(f"{VAULT}/**/*.md", recursive=True)):
        if is_skipped(f):
            continue
        raw = open(f, encoding="utf-8", errors="ignore").read()
        body, _ = clean(raw)
        if len(body) < 60:
            continue
        rel = os.path.relpath(f, VAULT)
        seen.add(rel)
        h = hashlib.sha256(raw.encode()).hexdigest()[:16]
        d = by_path.get(rel)
        if d is None:
            added.append(rel)
        elif d.get("content_hash") != h:
            modified.append(rel)
    deleted = sorted(set(by_path) - seen)
    #  ⚠ **"the vault is not visible" and "everything was deleted" are different events.**  A
    #     container with no vault mounted, a wrong VAULT_DIR, or a path that is simply not there
    #     all produce an empty walk —— and the subtraction above then reports **every indexed
    #     document as deleted**.  Measured 2026-09-02: an empty vault against this DB printed
    #     "deleted 1115" and a high-severity "run sync", which would rebuild the index down to
    #     nothing.  This page's own comment below says a status board claiming to know what it
    #     does not is the worst failure —— this was that, on the largest number it shows.
    vault_absent = bool(by_path) and not seen
    #  ⚠ **Zero visible is not the only shape of this failure.**  One surviving file disarmed the
    #     check above: with 1,115 indexed and 1 visible it reported "deleted 1,114" and advised
    #     `sync`, which rebuilds the index down to that one document —— the very outcome the
    #     comment above says it prevents.  A half-finished iCloud/Dropbox sync, a dropped bind
    #     mount, or a wrong VAULT_DIR that happens to hold one .md all land here.
    #     A real bulk deletion is possible too, and the honest answer is the same for both: say
    #     what vanished and let a person decide, rather than advising an irreversible rebuild.
    #     Half is the line —— an editing session changes a handful of notes, not most of them.
    vault_shrunk = (not vault_absent and by_path
                    and len(deleted) > len(by_path) * VAULT_SHRINK_RATIO)
    if vault_absent:
        deleted = []

    # ── Documents whose KG is stale ───────────────────────────────────
    # A **broken query** and **no stale documents** must be told apart.  It used to be one
    # blanket `except: pass`, so both printed "0 stale documents" and the status board
    # called a broken DB healthy.  A status board claiming to know what it does not is the worst failure.
    #
    # But **a missing table is normal** —— schema_v3 drops stale_docs once indexing finishes
    # (everything was resolved).  It is also absent before the first build.
    # So absence is quietly 0, and only "present but unreadable" is raised as an error.
    stale, stale_error = [], ""
    # ⚠ `list_tables()` returns a **`ListTablesResponse`**, not a list of names
    #   (`.tables` + `.page_token`, measured on lancedb 0.37.1).  Swapping the deprecated
    #   `table_names()` for it verbatim iterates that object instead, yielding tuples like
    #   `("tables", [...])`, so membership is **always False** —— wrong with no error.
    #   With 8 tables, page_token never comes into play.
    if "stale_docs" in set(db.list_tables().tables):
        try:
            rows = db.open_table("stale_docs").search().limit(999999).to_list()
            latest = {}
            for r in sorted(rows, key=lambda x: x.get("marked_at", 0)):
                latest[r["doc_id"]] = r
            stale = list(latest.values())
        except Exception as e:
            stale_error = f"{type(e).__name__}: {e}"
    stale_ratio = len(stale) / max(len(docs), 1)

    # ── Has extraction fallen behind ──────────────────────────────────
    kg_path = os.path.join(KAL_HOME, "lr_kg.json")
    extract_drift, extracted_at, drift_error = [], 0, ""
    if os.path.exists(kg_path):
        try:
            kg = json.load(open(kg_path))
            extracted_at = int(kg.get("extracted_at", 0) or _mtime(kg_path))
            snap = kg.get("doc_hashes") or {}
            if snap:
                for f in sorted(glob.glob(f"{VAULT}/**/*.md", recursive=True)):
                    if is_skipped(f):
                        continue
                    rel = os.path.relpath(f, VAULT)
                    was = snap.get(rel)
                    if was is None:
                        continue
                    try:
                        now = hashlib.sha256(
                            open(f, encoding="utf-8", errors="ignore").read().encode()
                        ).hexdigest()[:16]
                    except OSError as e:
                        # One unreadable file must not throw away the whole scan.  The outer
                        # except used to catch it and hand back **the partially built list**
                        # as the final result, which then reported "up to date".
                        drift_error = drift_error or f"{rel}: {e}"
                        continue
                    if was != now:
                        extract_drift.append(rel)
        except Exception as e:
            drift_error = drift_error or f"{type(e).__name__}: {e}"

    # ── Have the artifacts fallen behind ──────────────────────────────
    artifacts = {
        "graph_export": _mtime(os.path.join(KAL_HOME, "graph_export", "graph3d.html")),
        # The plugin's artifacts live inside the vault.  VAULT is required to find them in a container.
        "kal_graph_json": _mtime(os.path.join(
            VAULT, ".obsidian", "plugins", "kal-galaxy", "kal-graph.json")),
        "galaxy_html": _mtime(os.path.join(REPO, "viewer", "galaxy.html")),
    }
    stale_artifacts = [k for k, v in artifacts.items() if v and built_at and v < built_at]

    return {
        "db": {
            "path": DB,
            "tables": counts,
            "disk_bytes": _dirsize(DB),
            #  **Filesystem** free space.  disk_bytes above is the DB folder's size and cannot see this.
            "fs": _fs_free(),
            # How many **old versions** remain inside the retention window (24h).  Re-indexing
            # got 20× cheaper and so runs often, while the window did not change, so the disk
            # quietly swells (445MB → 1.4GB, measured).  It shrinks on its own, but it startles.
            "old_versions": _old_versions(),
            "built_at": built_at,
            "embedding_model": meta.get("embedding_model", ""),
            "schema_version": meta.get("schema_version", ""),
            #  The vault the DB was **built from** (recorded in meta).
            "vault_path": meta.get("vault_path", ""),
            #  The vault this process is **reading**.  In a container that is /vault, so a
            #  direct comparison always looks wrong; the mount's host path is used instead.
            #  A difference means the path on screen and the DB's contents disagree ——
            #  which really happens when `KAL_VAULT=…` indexes a different vault from the CLI.
            "vault_now": os.environ.get("KAL_VAULT_HOST") or VAULT,
        },
        "documents": {
            "indexed": len(docs),
            "origin": dict(origin),
            "added": added, "modified": modified, "deleted": deleted,
            #  True when the DB holds documents and the vault yielded none —— see above.
            "vault_absent": vault_absent,
                "vault_shrunk": vault_shrunk,
        },
        "kg": {
            "entities": counts.get("lr_entities", 0),
            "relations": counts.get("lr_relations", 0),
            "stale": [{"path": r["path"], "reason": r["reason"],
                       "marked_at": int(r.get("marked_at", 0))} for r in stale],
            "stale_ratio": round(stale_ratio, 4),
            # A non-empty string means **the numbers above must not be trusted**
            "stale_error": stale_error,
            "drift_error": drift_error,
            "warn_ratio": WARN_RATIO,
            "extracted_at": extracted_at,
            "extract_drift": extract_drift,
        },
        "artifacts": {"mtimes": artifacts, "stale": stale_artifacts},
        "checked_at": int(time.time()),
    }


# Pipeline steps —— name, description, command, rough duration.  UI and CLI share this list.
#
# Display strings are English (single source; the CLI prints these). Korean UI text lives in
# web/src/i18n/catalog.ts keyed by id — keep ids stable, and add a Korean entry there when
# adding a step (web/tests/catalog.test.ts checks the id set).
#
# needs_llm  does it call `claude -p`.  In a container this may fail to authenticate
#            (macOS keeps credentials in the keychain — mounting ~/.claude does not bring them),
#            so the API checks **before** running and blocks.  Better than dying 30 minutes in.
STEPS = [
    # ── The main pipeline ─────────────────────────────────────────
    # From the notes in the vault to a searchable knowledge graph.  **Order matters.**
    # order is that order, and the UI draws the flow from it.
    {"id": "distill", "title": "Distill sessions", "group": "main", "order": 1,
     "desc": "~/.claude conversation logs → brain-ingest documents. Credentials are masked.",
     "reads": "~/.claude/projects/**", "writes": "~/.kal/distilled/",
     "cmd": ["distill_sessions.py", "--workers", "8"],
     "minutes": 20, "needs_llm": True, "writes_db": False, "vault_derived": False},
    {"id": "promote", "title": "Promote to vault", "group": "main", "order": 2,
     "desc": "Move the distilled documents into the vault and commit.",
     "reads": "~/.kal/distilled/", "writes": "vault raw/conversations/sessions/",
     #  ⚠ **It deletes and rewrites documents inside the user's vault.**  `writes_db` was
     #     False, so the web's `heavy()` showed no confirmation —— the one step that touches
     #     the vault was the one step that did not ask.  (deep review 2026-08-28)
     "writes_vault": True,
     "cmd": ["promote_distilled.py"],
     "minutes": 1, "needs_llm": False, "writes_db": False, "vault_derived": False},
    {"id": "extract", "title": "Extract knowledge graph", "group": "main", "order": 3,
     "desc": "For every 2,400-char chunk, an LLM extracts entities and relations. Unchanged chunk hashes are not re-sent.",
     "reads": "vault **/*.md", "writes": "~/.kal/lr_kg.json",
     "cmd": ["lr_extract.py"],
     "minutes": 30, "needs_llm": True, "writes_db": False, "vault_derived": True},
    {"id": "index", "title": "Rebuild knowledge DB", "group": "main", "order": 4,
     "desc": "Chunk the vault into {chunk_chars} chars, embed, then read lr_kg.json to merge and "
             "place entities. Old LanceDB versions are cleaned up at the end.",
     "reads": "vault **/*.md + ~/.kal/lr_kg.json",
     "writes": "documents · chunks · ix_* · lr_*",
     "cmd": ["schema_v3.py"],
     "minutes": 3, "needs_llm": False, "writes_db": True, "vault_derived": True},
    {"id": "export", "title": "Export graph", "group": "main", "order": 5,
     "desc": "Louvain clustering + LLM labels \u2192 viewer artifacts. The plugin bundle is only "
             "rebuilt on the host (the container has no plugin/).",
     # The container has no plugin/ (.dockerignore), so this step guarantees 3 artifacts there.
     # Run on the host with `just export`, export_all.sh also builds the plugin bundle.
     "reads": "LanceDB", "writes": "graphml · graph3d.html · kal-graph.json",
     "cmd": ["export_all.sh"],
     "minutes": 2, "needs_llm": True, "writes_db": False, "vault_derived": False},

    # ── Bundles ───────────────────────────────────────────────────
    # Re-run the steps above in order.  runs is that list.
    {"id": "refresh_kg", "title": "Refresh stale KG", "group": "combo", "order": 0,
     "desc": "Restore the KG of stale documents. The extraction cache is kept, so only changed "
             "chunks reach the LLM.",
     "runs": ["extract", "index", "export"],
     "cmd": ["refresh_kg.py"],
     "minutes": 35, "needs_llm": True, "writes_db": True, "vault_derived": False},
    {"id": "apply_aliases", "title": "Apply aliases only (fast)", "group": "combo", "order": 0,
     "desc": "Apply aliases.yml to the graph. Skips extraction, so it is fast, but newly merged "
             "entities get stitched descriptions instead of an LLM re-summary.",
     "runs": ["index", "export"],
     "cmd": ["refresh_kg.py", "--aliases-only"],
     "minutes": 5, "needs_llm": True, "writes_db": True, "vault_derived": False},
    {"id": "rebuild_all", "title": "Full rebuild", "group": "combo", "order": 0,
     "desc": "Everything from distill to export, including weight tuning and evaluation.",
     "runs": ["distill", "promote", "extract", "index", "export", "verify"],
     "cmd": ["rebuild_all.sh"],
     "minutes": 60, "needs_llm": True, "writes_db": True, "vault_derived": False},

    # ── Partial refresh ───────────────────────────────────────────
    {"id": "sync", "title": "Incremental sync", "group": "partial", "order": 0,
     "desc": "Refresh chunks, vectors and the inverted index for changed documents only. "
             "**Cannot fix the KG**: that needs an LLM, so it just marks stale_docs and moves on.",
     "reads": "vault **/*.md", "writes": "chunks · ix_* · stale_docs",
     "cmd": ["sync_v3.py"],
     "minutes": 1, "needs_llm": False, "writes_db": True, "vault_derived": True},

    # ── Checks ────────────────────────────────────────────────────
    {"id": "verify", "title": "Verify docs", "group": "check", "order": 0,
     "desc": "Do the numbers in the docs match the DB, and do relative links resolve? Changes nothing.",
     "reads": "docs/**/*.md + LanceDB", "writes": "",   # writes nothing
     "cmd": ["verify_docs.py"],
     #  ⚠ This one needs **the repository**, not just src/.  It walks `<repo>/**/*.md` and
     #    resolves every relative link, and the api image ships only `src/` and the two yml
     #    files —— so in a container it dies with "found no .md at all".  Shipping docs/ would
     #    not fix it either: the links reach `plugin/` (263MB) and `web/`, which .dockerignore
     #    excludes on purpose.  It is a host step.  (measured 2026-09-03 by running it through
     #    /api/runs: run 20260903-164655 failed with ROOTS=['/app'].)
     "minutes": 1, "needs_llm": False, "writes_db": False, "vault_derived": False, "needs_repo": True},
]
GROUPS = [
    {"id": "main", "title": "Full pipeline",
     "desc": "From the notes in your vault to a searchable knowledge graph. Order matters."},
    {"id": "combo", "title": "Bundles",
     "desc": "Re-run the steps above in sequence."},
    {"id": "partial", "title": "Partial refresh",
     "desc": "Touch only part of it. Fast, but something is left behind."},
    {"id": "check", "title": "Checks",
     "desc": "Read-only. Changes nothing."},
]
#  Numbers inside a description **follow the configuration.**  Once the chunk size became
#  changeable from the screen, hardcoding "chunk the vault into 500 chars" makes the
#  confirmation dialog lie the moment a user sets 900 —— and it lies in the very place that
#  says "this cannot be undone".  (measured 2026-08-22: after the switch to 900, the
#  re-index dialog still said 500.)
try:
    import kal_config as _kcfg
    _CFGV = _kcfg.values()
except Exception:                       # the step list must survive an unreadable config module
    _CFGV = {"chunk_chars": 500}
for _s in STEPS:
    if "{" in _s["desc"]:
        _s["desc"] = _s["desc"].format(**_CFGV)

STEP_BY_ID = {s["id"]: s for s in STEPS}


def _old_versions(older_than=3600):
    """How many old versions remain inside the retention window.  Each build adds one per table."""
    n, now = 0, time.time()
    for lance in glob.glob(os.path.join(DB, "*.lance")):
        v = os.path.join(lance, "_versions")
        try:
            n += sum(1 for f in os.listdir(v)
                     if now - os.path.getmtime(os.path.join(v, f)) > older_than)
        except OSError:
            pass
    return n


#  From how many should it speak up —— re-indexing once a day stacks one per table (8).
#  Crying at that level is the boy who cried wolf.  Five times that (=40) —— around there a
#  person starts asking "why is this so big".  At the time of measurement there were 379.
OLD_VERSION_WARN = 40
#  Filesystem free-space threshold —— measured as **absolute bytes remaining**.
#  Setting it by usage ratio (90%/95%) first produced "only 94.5GB left" on a 1TB disk.
#  A ratio means less the larger the disk.  What kills it is bytes running out.
FS_FREE_CRIT_GB = 3.0
FS_FREE_WARN_GB = 10.0


def recommend(st):
    """→ [{step, why, severity}]  most urgent first.

    severity: high (content is genuinely missing) · medium (stale) · low (tidying)
    """
    out = []
    d, k, a = st["documents"], st["kg"], st["artifacts"]

    # If the judgement itself could not run, **that fact must come first**.  Every piece of
    # advice below trusts these numbers, so if the numbers cannot be trusted, neither can it.
    for key, what in (("stale_error", "the stale-document check"), ("drift_error", "the extraction-drift check")):
        if k.get(key):
            out.append({"step": "", "severity": "high",
                        "why": f"{what} failed ({k[key]}) "
                               f"— do not trust readings like '0 stale' below"})

    if d.get("vault_absent"):
        out.append({"step": "vault", "severity": "high",
                    "why": f"the index holds {d['indexed']} documents but the vault yielded "
                           f"none — it is empty, not mounted, or VAULT_DIR points elsewhere.  "
                           f"This is not 'everything was deleted', and running sync would "
                           f"empty the index"})
    if d.get("vault_shrunk"):
        out.append({"step": "vault", "severity": "high",
                    "why": f"{len(d['deleted'])} of {d['indexed']} indexed documents are not "
                           f"in the vault — more than half.  If you deleted them, run sync "
                           f"yourself; if not, the vault is only partly visible (a sync still "
                           f"running, a dropped mount, the wrong VAULT_DIR) and sync would "
                           f"rebuild the index down to what is there"})
    n_new = len(d["added"]) + len(d["modified"]) + len(d["deleted"])
    #  A shrunken vault already got its own, louder advice —— adding "run sync" beside it is
    #  advising the very rebuild that would empty the index.
    if n_new and not d.get("vault_shrunk") and not d.get("vault_absent"):
        out.append({"step": "sync", "severity": "high",
                    "why": f"{n_new} documents are not reflected in the index "
                           f"(added {len(d['added'])} · modified {len(d['modified'])} · deleted {len(d['deleted'])}) "
                           f"— searching now returns nothing for them, or the old content"})
    if k["extract_drift"]:
        out.append({"step": "refresh_kg", "severity": "high",
                    "why": f"{len(k['extract_drift'])} documents changed since extraction "
                           f"— the chunks hold the new content while entities and relations hold the old"})
    if k["stale_ratio"] >= k["warn_ratio"]:
        out.append({"step": "refresh_kg", "severity": "medium",
                    "why": f"{len(k['stale'])} documents have a stale KG "
                           f"({k['stale_ratio']*100:.1f}% ≥ {k['warn_ratio']*100:.0f}%)"})
    elif k["stale"]:
        out.append({"step": "refresh_kg", "severity": "low",
                    "why": f"{len(k['stale'])} documents have a stale KG "
                           f"({k['stale_ratio']*100:.1f}% — under the {k['warn_ratio']*100:.0f}% threshold)"})
    #  ⚠ Read from `st["db"]`.  It first read from `d` (= st["documents"]), so the value was
    #    always 0 —— **a condition that could never fire**, newly created.  It surfaced only
    #    because nothing ever appeared on screen.  A condition added must be made to fire once.
    _db = st.get("db") or {}
    if _db.get("old_versions", 0) >= OLD_VERSION_WARN:
        out.append({"step": "", "severity": "low",
                    "why": f"{_db['old_versions']} old versions remain inside the retention window (24h) "
                           f"— the disk is inflated by that much.  It shrinks on its own; to reclaim now, "
                           f"run `just vacuum` (it deletes the rollback path, so do it with no queries running)"})
    #  **Is the filesystem filling up.**  `old_versions` above only watches the DB folder swell,
    #  and `disk_bytes` is a folder size too —— nobody was watching the filesystem itself fill.
    #  On 2026-08-21 the overlay hit 100%, LanceDB could not create its spill directory under
    #  `/tmp` and died, and this screen still said only "447 old versions … free".
    #  The DB and /tmp **may be different filesystems** (bind mount vs overlay).
    for _k, _f in sorted((st.get("db", {}).get("fs") or {}).items()):
        _gb = _f["free"] / 1e9
        _sev = ("high" if _gb < FS_FREE_CRIT_GB
                else "medium" if _gb < FS_FREE_WARN_GB else None)
        if not _sev:
            continue
        _what = ("where the knowledge DB lives" if _k == "db"
                 else "where LanceDB **spills** on large queries")
        out.append({"step": "", "severity": _sev,
                    "why": f"{_what} ({_f['path']}) has only {_gb:.1f}GB left "
                           f"({_f['pct_used']:.0f}% used).  Once full, indexing and search die with "
                           f"`No space left on device`.  "
                           f"In a container it is usually the Docker VM that is full —— "
                           f"check with `docker system df`, then `docker builder prune` "
                           f"(safe, it is rebuilt) · `just vacuum` (reclaims old versions)"})

    if a["stale"]:
        out.append({"step": "export", "severity": "low",
                    "why": f"artifacts are older than the DB: {' · '.join(a['stale'])} "
                           f"— the graph view shows old entities"})
    # When one step is flagged for several reasons, only the highest severity survives.
    # But **advice with no step is a separate fact** —— the two error channels above both
    # carry `step: ""`, and putting them in one map made the later one vanish silently.
    # Measured: turning on both stale_error and drift_error still produced only 1. (2026-08-21)
    rank = {"high": 0, "medium": 1, "low": 2}
    best, loose = {}, []
    for r in sorted(out, key=lambda x: rank[x["severity"]]):
        if r["step"]:
            best.setdefault(r["step"], r)
        else:
            loose.append(r)
    return sorted(loose + list(best.values()), key=lambda x: rank[x["severity"]])


def _ago(ts):
    if not ts:
        return "no record"
    s = max(0, int(time.time()) - ts)
    if s < 3600:
        return f"{s//60} min ago"
    if s < 86400:
        return f"{s//3600} h ago"
    return f"{s//86400} d ago"


def _fixture_if_no_db():
    """Swap in a **fixture** when there is no real DB.  Returns a cleanup function.

    The self-check was opening the real DB at `~/.kal` —— it ran only on the author's machine
    and died in CI with `Table 'lr_entities' was not found`.  `just` stops at the first
    failure, so the 20-odd self-checks after it never ran at all (reproduced 2026-08-25, round 4).

    ⚠ **When a real DB exists, it is used.**  Always covering it with a fixture would hide
       defects that only real data reveals —— a check running is not a check guarding.
    """
    global DB
    import shutil
    import fixture_db
    if fixture_db.db_is_usable(DB):
        return lambda: None                         # the real DB is intact
    _was, DB = DB, fixture_db.build()
    print(f"  ⓘ no real DB, running against a fixture ({_was} → temporary)")
    def _cleanup(_p=DB, _o=_was):
        global DB
        DB = _o
        shutil.rmtree(_p, ignore_errors=True)
    return _cleanup


def _selftest():
    """Does the status board **claim to know what it does not.**

    The worst failure of this screen is not blowing up but **looking healthy**.
    A broken stale query used to produce "0 stale documents" through `except: pass`,
    character-for-character identical to a healthy DB.

    The other direction must not be wrong either —— schema_v3 drops stale_docs once indexing
    finishes, so **a missing table is normal**.  Raising that as an error is a false alarm
    every single run.  Telling the two apart is the point of this check.
    """
    #  ── Every key in STEPS must exist in Go's `Step` struct ────────────────────────────────
    #  Go re-serialises this list, so a key absent from the struct is **dropped without a word**.
    #  That is not hypothetical: `writes_vault` went missing exactly this way and `promote` ——
    #  which rewrites documents inside the vault —— ran with no confirmation (deep review
    #  2026-08-28).  The struct's own comment records it.  Nothing checked the pairing until now.
    _go = pathlib.Path(__file__).resolve().parent.parent / "api" / "main.go"
    #  A selftest runs from the repository.  If main.go is gone, that is the finding, not a
    #  reason to skip —— skipping is how a check becomes indistinguishable from a passing one.
    assert _go.is_file(), f"api/main.go is not next to src/: {_go}"
    _body = re.search(r"type Step struct \{(.*?)\n\}", _go.read_text(encoding="utf-8"), re.S)
    assert _body, "the Step struct could not be read from api/main.go"
    _tags = set(re.findall(r'json:"([a-z_]+)', _body.group(1)))
    _used = {k for st in STEPS for k in st}
    assert _used <= _tags, (
        f"STEPS uses keys Go's Step struct does not carry, so Go drops them silently: "
        f"{sorted(_used - _tags)}")

    import lancedb.db as _db

    class _Fake:
        def __init__(s, tables): s.tables, s.page_token = tables, None

    orig_open = _db.LanceDBConnection.open_table
    orig_lt = _db.LanceDBConnection.list_tables

    # ① healthy —— no stale_docs must stay quiet (the ordinary state right after indexing)
    k = collect()["kg"]
    assert k["stale_error"] == "", f"a missing table is treated as an error: {k['stale_error']}"
    #  Not every step-less piece of advice is an "error" —— disk advice (low) lives here too.
    #  What this check prevents is a **failed judgement** (high) appearing in a healthy state.
    #  Casting the net as just `not a["step"]` catches legitimate advice and fails falsely.
    assert not [a for a in recommend(collect())
                if not a["step"] and a["severity"] == "high"], \
        "a healthy state produces error advice —— that would be a false alarm every run"

    # ② the table exists but **cannot be read** —— this must surface
    seen = {"n": 0}
    def boom(self, n, *a, **kw):
        if n == "stale_docs":
            seen["n"] += 1
            # let the counting loop at :62 through and blow up only the stale block's call
            if seen["n"] >= 2:
                raise RuntimeError("deliberate")
            return orig_open(self, "meta")
        return orig_open(self, n, *a, **kw)
    #  ⚠ **Do not append it when it is already there.**  It used to append unconditionally,
    #     and once sync_v3 started really creating stale_docs it entered the list twice.  Then
    #     the count loop at :94 opens the same table twice and the net cast as `seen >= 2`
    #     fires **in the count loop** rather than in the stale block —— in that environment
    #     this check simply died without ever verifying "does it swallow a failed query".
    #     And this sits in the middle of `just selftest`, so **everything after it went unrun**. (2026-08-24)
    _db.LanceDBConnection.list_tables = lambda self, *a, **kw: _Fake(
        list(dict.fromkeys(list(orig_lt(self).tables) + ["stale_docs"])))
    _db.LanceDBConnection.open_table = boom
    try:
        st = collect()
        assert "deliberate" in st["kg"]["stale_error"], "a failed query is still swallowed"
        adv = [a for a in recommend(st) if not a["step"]]
        assert adv, "it failed and does not surface in the advice —— invisible on screen"
        assert "do not trust" in adv[0]["why"], "it does not say what must not be trusted"
    finally:
        _db.LanceDBConnection.open_table = orig_open
        _db.LanceDBConnection.list_tables = orig_lt

    # ③ `list_tables()` is a **ListTablesResponse**, not a list.
    #    Naively swapping in `table_names()` iterates ("tables", [...]) tuples instead,
    #    so membership is always False —— wrong with no error.
    import lancedb
    r = lancedb.connect(DB).list_tables()
    assert hasattr(r, "tables") and not isinstance(r, list), \
        "the shape of list_tables() changed —— revisit the membership check in collect()"

    # ④ Does error advice **kill the CLI output.**  `--json` returns earlier above, so the
    #    web and the API never take this path —— only the CLI died, which is why it took so long to find.
    #    ⑤ Do **both** error channels survive.  Both carry `step: ""`, so putting them in one
    #    map makes the later one vanish silently.
    fake = {"documents": {"added": [], "modified": [], "deleted": []},
            "kg": {"stale_error": "OSError: A", "drift_error": "OSError: B",
                   "extract_drift": [], "stale": [], "stale_ratio": 0.0, "warn_ratio": 0.2},
            "artifacts": {"stale": []}}
    recs = recommend(fake)
    loose = [a for a in recs if not a["step"]]
    assert len(loose) == 2, f"only {len(loose)} of 2 error advices survived —— dedup flattens them"
    assert {"A" in a["why"] for a in loose} == {True, False}, "the same advice appears twice"
    #  ★ Call **the real output function**, not a stand-in.  It used to imitate it with
    #    `STEP_BY_ID.get(...)`, and reverting the CLI to the original `STEP_BY_ID[...]` still
    #    passed —— a test that guarded nothing.
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        print_recommend(recs)            # a KeyError('') death is caught right here
    txt = buf.getvalue()
    assert "OSError: A" in txt and "OSError: B" in txt, \
        f"error advice never reaches the screen:\n{txt}"
    assert STEP_BY_ID.get("") is None, "an empty step is in STEP_BY_ID —— this check becomes meaningless"

    # ⑧ Does **every step record that it ran.**
    #    Why `run_log.py:4-8` exists: the web screen's "last run" knows only about runs
    #    started from the web.  Run from the CLI and nothing is recorded, so "three successes
    #    today, and the screen still shows yesterday's failure as the last" —— the user
    #    concludes it is broken right now.  Yet only 5 of 10 steps were recording, and
    #    **nothing checked the two lists against each other** (r4-fresh, round 5).  Now it does.
    #
    #    Python records with `record("<id>")`, shell with `run_log.py --step <id>`.
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _srcdir = os.path.join(_root, "src")
    #  ⚠ **Strip comments and docstrings first.**  Otherwise the guard is fooled by its own
    #    documentation —— `run_log.write()`'s docstring carries the usage example
    #    `python run_log.py --step export …`, so deleting the real call still passed.  This
    #    repository fell into that trap 3 times (comment · docstring · shell example).  Strip by default.
    import glob as _g
    import re as _re1
    _strip1 = r'"""[\s\S]*?"""' + r"|'''[\s\S]*?'''" + r"|#[^\n]*"
    _blob = ""
    for _f in sorted(os.listdir(_srcdir)):
        if _f.endswith((".py", ".sh")) and _f != "status.py":
            _blob += _re1.sub(_strip1, "",
                              open(os.path.join(_srcdir, _f), encoding="utf-8").read())
    #  ⚠ Conditional expressions like `record("a" if cond else "b")` must be caught too ——
    #    looking only for the literal `record("refresh_kg")` reported two healthy steps as
    #    "missing".  A name appearing literally inside a `record(` call counts.
    import re as _re2
    _recorded = set()
    for _m2 in _re2.finditer(r"record\(([^)]*)\)", _blob):
        _recorded |= set(_re2.findall(r'"([a-z_]+)"', _m2.group(1)))
    _recorded |= set(_re2.findall(r"--step\s+([a-z_]+)", _blob))
    _missing = [s_["id"] for s_ in STEPS if s_["id"] not in _recorded]
    assert not _missing, (
        f"steps that record no run: {_missing} —— run from the CLI, the screen keeps "
        f"showing an old run as 'the last one' (run_log.py:4-8)")

    # ⑦ Does **the justfile recipe match the declared command.**
    #    When the container has no LLM authentication the UI says "run `just <id>` on the
    #    host".  For that guidance to be right, the recipe must match `cmd` here.
    #    In fact `apply_aliases` declared `--aliases-only` (5 min) while the recipe ran
    #    `--force` (35 min, re-running extraction) —— 7× slower, and it consumes
    #    homonyms.yml as well.  (r4-ux, round 5)
    #
    #    Matching on the name alone is not enough.  **The arguments are checked too.**
    import re as _re
    _jf = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "justfile")
    if os.path.isfile(_jf):
        _txt = open(_jf, encoding="utf-8").read()
        _alias = dict(_re.findall(r"^alias\s+(\S+)\s*:=\s*(\S+)", _txt, _re.M))
        #  ⚠ Recipes take arguments (`sync *args:`).  Looking only for a `:` after the name
        #    misses 6 of 10 —— the guard then covers 4 and passes quietly (measured 2026-08-21).
        #  ⚠ Argument **defaults live in the signature** (`distill *args="--workers 8":`).
        #    Reading only the body misses them and calls a healthy recipe wrong —— the boy who
        #    cried wolf.  Signature and body are read together.  (measured 2026-08-21)
        _recipe = {m.group(1): m.group(2) + "\n" + m.group(3) for m in
                   _re.finditer(r"^([a-z][a-z0-9-]*)([^:\n]*):\n((?:[ \t]+[^\n]*\n)+)",
                                _txt, _re.M)}
        for _s in STEPS:
            _name = _alias.get(_s["id"], _s["id"])
            _body = _recipe.get(_name)
            if _body is None:
                continue                      # some steps have no recipe (UI only)
            _script = _s["cmd"][0]
            if _script not in _body:
                continue                      # a bundle recipe that runs a different script
            for _arg in _s["cmd"][1:]:
                assert _arg in _body, (
                    f"just {_s['id']} differs from the declaration —— it declares "
                    f"`{_script} {' '.join(_s['cmd'][1:])}` but the recipe has no `{_arg}`.  The UI advertises this command.")

    # ⑦-c **Is the empty-DB state the *same shape* as the usual one.**
    #    Returning a different shape made the web's `StatusPanel` throw reading `d.indexed`,
    #    and with no ErrorBoundary the screen went entirely blank —— a Python traceback
    #    relocated into a white browser page (2026-08-25, round 6).  That time it was only
    #    checked by hand and **no assertion was left behind.**  So it would drift again.
    _real = collect()                       # whatever is populated: fixture or real DB
    _empty = _empty_status(["documents"])

    #  ⚠ **Dynamic key maps** are not descended into.  For those, "what is present" is the
    #     content rather than the structure, and the web receives them as `Record<string, T>`
    #     and reads with `?? 0` —— empty is safe.  Left in, things like
    #     `artifacts.mtimes.galaxy_html` get called a "missing field" (measured).
    _DYNAMIC = {"tables", "origin", "mtimes"}

    def _shape(d, path=""):
        out = set()
        for k, v in d.items():
            out.add(path + k)
            if isinstance(v, dict) and k not in _DYNAMIC:
                out |= _shape(v, path + k + ".")
        return out

    _missing = _shape(_real) - _shape(_empty)
    assert not _missing, (
        "the empty-DB state is missing fields —— the web throws reading them and the screen blanks: "
        + " · ".join(sorted(_missing)))
    #  The types must match too.  `recommend` once diverged between `string[]` and
    #  `Recommendation[]` (raised in round 6).
    for _k in ("documents", "kg", "db", "artifacts"):
        assert type(_real[_k]) is type(_empty[_k]), f"the type of {_k} differs"
    assert isinstance(_empty["documents"]["indexed"], int), "indexed is not an integer"
    assert isinstance(_empty["db"]["embedding_model"], str), \
        "embedding_model is not a string —— the web calls .split() on it"

    # ⑦-b **Can the tests even fail.**
    #    Leaving through a `finally:` **replaces** the exception in flight —— a broken assert
    #    still exits 0.  A test exists, is wired up, and is structurally unable to fail.
    #    Measured (2026-08-25): `export_graph` and `export_kal_graph` were both like this,
    #    and **the same commit** used the correct shape in two other files.
    #
    #    ⚠ It started as a regex for `sys.exit(0)` after `finally:`.  Round 6 got around it
    #      **six ways**: `return` · `raise SystemExit(0)` · `exit(0)` · `sys.exit(not 1)` ·
    #      a one-line `finally: sys.exit(0)` · `except: pass`.
    #      So it reads the `ast` —— a syntactic structure does not wobble on spelling.
    #
    #    What this check **cannot see**: anything that only shows up when run (an unreachable
    #    assert, say).  Injecting `assert False` into all 28 tests would be exact, but model
    #    loading makes that impractical every run.  Only **known dangerous shapes** are blocked here.
    import ast as _ast, glob as _glob

    def _exits_zero(node):
        """Is this node 'end quietly with 0'."""
        if isinstance(node, _ast.Return):
            return True
        if isinstance(node, _ast.Raise) and isinstance(node.exc, _ast.Call):
            f = node.exc.func
            if getattr(f, "id", None) == "SystemExit":
                return not node.exc.args or _is_zero(node.exc.args[0])
        if isinstance(node, _ast.Expr) and isinstance(node.value, _ast.Call):
            f = node.value.func
            name = getattr(f, "attr", None) or getattr(f, "id", None)
            if name in ("exit", "_exit"):
                a = node.value.args
                return not a or _is_zero(a[0])
        return False

    def _is_zero(n):
        if isinstance(n, _ast.Constant):
            return n.value in (0, None, False)
        return True          # something like `not 1` —— undecidable counts as dangerous

    _srcdir0 = os.path.dirname(os.path.abspath(__file__))
    _cant_fail = []
    for _f in sorted(_g.glob(os.path.join(_srcdir0, "*.py"))):
        try:
            tree = _ast.parse(open(_f, encoding="utf-8").read())
        except SyntaxError:
            continue
        def _guards_selftest(try_node):
            """Does this try **wrap a self-check.**

            ⚠ Narrowing the scope is the point.  Catching every `except Exception: pass`
              first raised 21 findings and **all of them were fine** —— best effort (skip
              when a config cannot be read) is a legitimate idiom.  A check with 21 false
              positives is a check nobody reads.  Only a try that calls `_selftest()` counts.
            """
            for n in _ast.walk(try_node):
                if isinstance(n, _ast.Call):
                    nm = getattr(n.func, "attr", None) or getattr(n.func, "id", "")
                    if "selftest" in str(nm).lower():
                        return True
            return False

        for node in _ast.walk(tree):
            if isinstance(node, _ast.Try) and _guards_selftest(node):
                for stmt in node.finalbody:
                    for sub in _ast.walk(stmt):
                        if _exits_zero(sub):
                            _cant_fail.append(
                                f"{os.path.basename(_f)}:{sub.lineno} "
                                f"(exits 0 inside a finally)")
                            break
                #  Swallowing an assert with `except Exception: pass` is the same crime
                for h in node.handlers:
                    body = [x for x in h.body if not isinstance(x, _ast.Pass)]
                    if not body and h.type is not None:
                        nm = getattr(h.type, "id", "")
                        if nm in ("Exception", "BaseException", "AssertionError"):
                            _cant_fail.append(
                                f"{os.path.basename(_f)}:{h.lineno} "
                                f"(except {nm}: pass —— it swallows the assert)")
    assert not _cant_fail, (
        "these tests **cannot fail** (the exception is replaced or swallowed): "
        + " · ".join(_cant_fail))

    # ⑧ **Is the test wired into the list.**
    #    `entity_resolve.py` held a dense set of tests guarding merge regressions, and it was
    #    not in `just selftest` —— the other 18 use a `--selftest` flag while this file was a
    #    file was a bare `__main__`, so it never made the list.  It ran only when that file was
    #    executed by hand, so the regressions written to guard something guarded nothing day to
    #    day.  `run_log.py` was the same.  **A test existing and a test running differ.** (2026-08-21)
    _srcdir = os.path.dirname(os.path.abspath(__file__))
    #  Anything that calls a real LLM or needs a human hand stays out of the fast list.
    #  It is recorded here **with its reason**, so it is distinguishable from an oversight.
    _EXEMPT = {"claude_cli.py": "makes 2 real haiku calls (up to 210s, and it costs) —— run by hand"}
    _Q3, _S3 = chr(34) * 3, chr(39) * 3
    if os.path.isfile(_jf):
        #  ⚠ **Do not search the whole justfile.**  Nearly every script also appears in an
        #    ordinary recipe (`search:` calls kal_search.py), so deleting the wiring still
        #    passes —— a guard present but silent.  It was written that way at first and a
        #    mutation exposed it (2026-08-21).  Only the **bodies** of the `selftest` and
        #    `mcp-test` recipes are read.
        _jt = open(_jf, encoding="utf-8").read()
        _recipes = "".join(
            m.group(0) for m in _re.finditer(
                r"^(?:selftest|mcp-test)[^:\n]*:[^\n]*\n((?:[ \t]+[^\n]*\n|\n)+)", _jt, _re.M))
        assert _recipes.strip(), "no selftest recipe found in the justfile —— this guard is blind"
        for _fn in sorted(os.listdir(_srcdir)):
            if not _fn.endswith(".py"):
                continue
            _t = open(os.path.join(_srcdir, _fn), encoding="utf-8").read()
            #  Tests come in two shapes —— `def _selftest()` (18 of them) and a bare
            #  `__main__` (5).  Looking only for the first misses the second and vice versa.
            #  It first looked only at `__main__` and was failing to cover all 18
            #  (measured 2026-08-21, right after this guard was added).
            if _fn.startswith("test_"):
                #  A name starting with test_ is a test —— wherever its asserts sit.
                #  `test_stale_resolve.py` keeps its asserts inside `def main()`, so neither
                #  of the two shapes below caught it.  Deleting its wiring stayed silent.
                _i = 0
            elif _re.search(r"^def _?selftest", _t, _re.M):
                _i = 0                        # function form —— the whole file is read
            else:
                _i = _t.find("\nif __name__")
                if _i < 0:
                    continue
            #  ⚠ Counting an `assert` inside a comment or docstring calls a file with no
            #    tests a test.  This session fell into that trap three times.  Strip first.
            _strip8 = (_Q3 + r"[\s\S]*?" + _Q3 + "|" + _S3 + r"[\s\S]*?" + _S3
                       + r"|#[^\n]*")
            if "assert " not in _re.sub(_strip8, "", _t[_i:]):
                continue                      # it has __main__ but is not a test (a CLI entry point)
            if _fn in _EXEMPT:
                continue
            assert _fn in _recipes, (
                f"{_fn} has tests in its __main__ and is not in the justfile —— nobody runs them.  "
                f"Add it to `just selftest`, or record why it cannot go there in status.py's _EXEMPT.")

    # ⑧b **Is any module-level constant assigned twice.**
    #    A second assignment at module level is legal Python and **silently wins**, so it is
    #    invisible to the reader and —— worse —— to a mutation: a mutation aimed at the first
    #    definition changes a name that no longer resolves to the code under test, and reports
    #    "the check does not fire" about a check that does.  That happened here on 2026-09-04:
    #    a botched edit left `FM_OPEN_RE` defined twice in frontmatter.py and the mutation sweep
    #    read as green.
    #      ⚠ `ruff`'s F811 does **not** cover this, which is worth stating because it is the
    #        obvious place to look.  F811 is "redefinition of an unused **name**" —— imports,
    #        functions, classes.  Measured: with a duplicate `FM_OPEN_RE = …` appended,
    #        `ruff check --select ALL` reports nothing.  Reassignment is ordinary Python; only
    #        "twice at module top level, in one file" is the suspicious shape, and that is what
    #        this walks.
    #      Only **direct** children of the module body count.  A name assigned in both arms of a
    #      try/except or if/else is nested inside that statement, not a top-level duplicate, and
    #      those are the legitimate patterns this must not flag.
    import ast as _ast
    for _f in sorted(_glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "*.py"))):
        try:
            _tree = _ast.parse(open(_f, encoding="utf-8").read())
        except SyntaxError:
            continue
        _seen, _dup = {}, []
        for _node in _tree.body:
            if not isinstance(_node, _ast.Assign):
                continue
            #  ⚠ **Refining a value is not shadowing it.**  `VAULT = env` / validate /
            #     `VAULT = expanduser(VAULT)` is the ordinary shape (eval_sessions.py:39-44), and
            #     a check that fires on it is a check somebody deletes the first time it does.
            #     What separates the two is whether the later assignment **reads** the name: the
            #     refinement does, the duplicate definition ignores what came before entirely.
            _reads = {n.id for n in _ast.walk(_node.value) if isinstance(n, _ast.Name)}
            for _t2 in _node.targets:
                if isinstance(_t2, _ast.Name):
                    if _t2.id in _seen and _t2.id not in _reads:
                        _dup.append((_t2.id, _seen[_t2.id], _node.lineno))
                    _seen[_t2.id] = _node.lineno
        assert not _dup, (
            f"{os.path.basename(_f)}: assigned twice at module level, and the later one wins "
            f"silently —— " + ", ".join(f"`{n}` at :{a} then :{b}" for n, a, b in _dup))

    # ⑨ **Are published ports bound to loopback only.**
    #    This app has no authentication (docs/ARCHITECTURE.md:628).  Loopback binding is the
    #    **only** thing standing between it and "anyone on the network can run the pipeline
    #    and edit the vault".  Yet the only thing holding that invariant was human attention ——
    #    `docs/STACK.md:248`'s "measured: connection refused from the LAN IP" was done by hand.
    #    In fact the `proxy` profile sat there bypassing the rule wholesale with `"80:80"`,
    #    while the same file recommended turning that profile on (2026-08-21).
    #    One deleted prefix opens it silently.  So it is pinned by a check.
    _root = os.path.dirname(_srcdir)
    _pubs = []
    for _cf in sorted(glob.glob(os.path.join(_root, "docker-compose*.yml"))):
        _in = False
        for _ln in open(_cf, encoding="utf-8"):
            if _re.match(r"^\s*ports:", _ln):
                _in = True
                continue
            if not _in:
                continue
            _m = _re.match(r'^\s*-\s*"?([^"\s]+:\d+(?::\d+)?(?:/\w+)?)"?\s*$', _ln)
            if _m:
                _pubs.append((os.path.basename(_cf), _m.group(1)))
            elif _ln.strip() and not _ln.strip().startswith("#"):
                _in = False
    #  A compose file with no ports found at all means the parser died —— and then this guard
    #  passes quietly as "everything safe".  Their presence is confirmed first.
    if glob.glob(os.path.join(_root, "docker-compose*.yml")):
        assert _pubs, "no published ports found in compose —— this guard has gone blind"
    for _cf, _p in _pubs:
        assert _re.match(r"^(127\.0\.0\.1|localhost|\$\{[A-Z_]+:-(127\.0\.0\.1|localhost)\}):", _p), (
            f"`{_p}` in {_cf} is not bound to loopback —— this app has **no authentication.** "
            f"Anyone on the network could run the pipeline and edit the vault. "
            f"Prefix `127.0.0.1:`, or default it to loopback with `${{VAR:-127.0.0.1}}`.")

    # ⑪ **Does the README still describe the outbound controls the code implements.**
    #    This is structure, not a number, so verify_docs cannot see it —— that one compares DB
    #    figures.  Rename an environment variable or a frontmatter key and leave the README
    #    alone, and the docs start lying quietly.  In the one place that says what leaves the
    #    machine.  (2026-08-22)
    #
    #    ⚠ The anchor is asserted, not merely tested for.  This used to read
    #      `if "<section title>" in _rt:` and do nothing when it was absent —— and when the
    #      README was rewritten (2026-09-01) that section went away, so the whole guard became
    #      a no-op with nothing to report.  A guard that disappears with its subject is worse
    #      than no guard: it still reads as coverage.  If the section is renamed, this must
    #      fail and be re-pointed on purpose.
    #
    #    Scope note: the README no longer tabulates SKIP / FM_KEEP / DATE_KEYS, so those are
    #    not asserted here —— demanding documentation the README does not claim to carry only
    #    trains people to delete the check.  What it does claim is the two user-facing
    #    controls, and those are exactly what a reader acts on.
    _rd = os.path.join(os.path.dirname(_srcdir), "README.md")
    if os.path.isfile(_rd):
        _rt = open(_rd, encoding="utf-8").read()
        _sec = "## What stays on your machine"
        assert _sec in _rt, (
            f"README has no `{_sec}` section —— that is where the outbound controls are "
            f"documented, and this guard reads it.  Re-point it if the section was renamed.")
        import schema_v3 as _sv
        #  The frontmatter key that keeps one note out of extraction (schema_v3.doc_meta).
        assert "no_llm" in _rt, (
            "README does not mention `no_llm` —— it is the only per-note way to keep a note "
            "out of extraction, and nothing but the docs tells a user it exists.")
        #  The environment variable that blocks whole folders (lr_extract.NO_LLM).
        assert "KAL_NO_LLM" in _rt, (
            "README does not mention `KAL_NO_LLM` —— the folder-level outbound control is "
            "invisible without it.")
        #  ⚠ The *matching rule* matters as much as the name.  It is path components anchored
        #    at the vault root, not a substring: `KAL_NO_LLM=private` must not match
        #    `my-private-notes/`.  lr_extract's self-check pins the behaviour; this pins that
        #    the README states it, because a user who reads "substring" blocks the wrong thing.
        assert "path components" in _rt, (
            "README does not say KAL_NO_LLM matches path components from the vault root —— "
            "a user reading it as a substring match will believe the wrong folders are blocked.")
        assert _sv.SKIP, "schema_v3.SKIP is empty —— the display list lost its contents"

    # ⑫ **Did a real secret get into `.env.example`.**
    #    This file **is committed.**  `.env` is gitignored; the sample is not —— commit it
    #    once with a token filled in and it stays in the history forever.  The README says
    #    "leave the sample's values empty", and the only thing enforcing that rule was human
    #    memory.  A shape seen repeatedly this session: a rule that lives only in the docs.
    _ex = os.path.join(os.path.dirname(_srcdir), ".env.example")
    if os.path.isfile(_ex):
        _SECRETish = ("TOKEN", "SECRET", "PASSWORD", "APIKEY", "API_KEY", "CREDENTIAL")
        for _ln in open(_ex, encoding="utf-8"):
            _ln = _ln.strip()
            if not _ln or _ln.startswith("#") or "=" not in _ln:
                continue
            _k, _v = _ln.split("=", 1)
            if any(s in _k.upper() for s in _SECRETish):
                assert not _v.strip(), (
                    f"`{_k}` in .env.example has a value filled in —— this file is committed.  "
                    f"Empty it, and keep the real value only in the gitignored `.env`.")

    # ⑬ **Does a step description match the configuration.**
    #    Once the chunk size became changeable from the screen, a hardcoded number in a
    #    description becomes a lie the moment a user changes it —— and it lies in the
    #    confirmation dialog that says "this cannot be undone".  Measured (2026-08-22): after
    #    switching to 900, the dialog still said "chunk the vault into 500 chars".
    for _s in STEPS:
        assert "{" not in _s["desc"], (
            f"step `{_s['id']}` has an unsubstituted placeholder in its description: {_s['desc'][:60]!r}")
    _idx = STEP_BY_ID.get("index")
    if _idx:
        #  ⚠ This assertion used to interpolate the number **with a Korean particle glued on**,
        #     so translating the catalog would have silently disarmed it.  It reads the number alone.
        assert str(_CFGV["chunk_chars"]) in _idx["desc"], (
            f"the chunk size in the index description differs from the configuration "
            f"({_CFGV['chunk_chars']}) —— the dialog states the wrong number: {_idx['desc'][:60]!r}")

    # ⑥ **Can the disk advice fire.**  It first read the value from the wrong key and was
    #    always 0 —— a condition present and unable to trigger.  It surfaced only because
    #    nothing ever appeared on screen.  A condition added must be made to fire once.
    quiet = dict(fake, kg=dict(fake["kg"], stale_error="", drift_error=""))
    assert not [a for a in recommend(quiet) if "old versions" in a["why"]], \
        "disk advice appears with no db information"
    loud = dict(quiet, db={"old_versions": OLD_VERSION_WARN})
    hit = [a for a in recommend(loud) if "old versions" in a["why"]]
    assert hit, f"{OLD_VERSION_WARN} old versions and no advice —— the condition cannot fire"
    assert hit[0]["step"] == "" and hit[0]["severity"] == "low"
    under = dict(quiet, db={"old_versions": OLD_VERSION_WARN - 1})
    assert not [a for a in recommend(under) if "old versions" in a["why"]], \
        "it fires below the threshold —— shouting after one re-index is the boy who cried wolf"

    # ⑩ **Can the filesystem advice fire.**  Same reason as ⑥ above —— on 2026-08-21 the
    #    overlay hit 100% and LanceDB died, and this screen had no eye at all with which to
    #    say so (it was measuring `_dirsize(DB)` alone).
    _base = dict(quiet, db={"old_versions": 0})
    def _with(free_gb, extra=None):
        fs = {"db": {"path": "/data/kal", "free": free_gb * 1e9,
                     "total": 100e9, "pct_used": 90.0}}
        if extra is not None:
            fs["tmp"] = {"path": "/tmp", "free": extra * 1e9,
                         "total": 100e9, "pct_used": 99.0}
        return dict(_base, db=dict(_base["db"], fs=fs))
    def _fs_hits(st):
        return [a for a in recommend(st) if "GB left" in a["why"]]

    assert not _fs_hits(_with(50)), "it fires with 50GB left —— the boy who cried wolf"
    warn = _fs_hits(_with(FS_FREE_WARN_GB - 1))
    assert warn and warn[0]["severity"] == "medium", ("the warning threshold does not fire", warn)
    crit = _fs_hits(_with(FS_FREE_CRIT_GB - 1))
    assert crit and crit[0]["severity"] == "high", ("the critical threshold does not fire", crit)
    #  ⚠ Measured as **absolute free space**, not a ratio.  Setting it at 90%/95% first
    #    produced "only 94.5GB left" on a 1TB disk —— a ratio means less the larger the disk.
    assert not _fs_hits(_with(94.5)), "94.5GB free and it shouts, going by usage ratio alone"
    #  When the DB and /tmp are on **different** filesystems, each is named (containers are)
    two = _fs_hits(_with(50, extra=1))
    assert len(two) == 1 and "spills" in two[0]["why"], ("it fails to name the spill side", two)

    #  On the same device, does `_fs_free()` fold them into one?  The test above injects `fs`
    #  directly and bypasses this function, so the function itself is called here.  Pointing
    #  the DB at a temporary directory guarantees the same device —— no reliance on the environment.
    import tempfile as _tf2
    _keep = globals()["DB"]
    try:
        globals()["DB"] = _tf2.gettempdir()
        assert len(_fs_free()) == 1, \
            f"one filesystem counted as two —— the same advice appears twice with different paths: {_fs_free()}"
    finally:
        globals()["DB"] = _keep

    assert "documents" in set(r.tables), "the table list cannot be read"

    #  ⑥ **An absent vault must not read as a mass deletion.**  This runs the **real** collect(),
    #     not a hand-built dict —— a first version of this check only exercised `recommend()`, and
    #     two mutations (blanking the computation, and removing the advice) both stayed green.
    #     A check that cannot see the production line is not a check for it.
    import tempfile as _tf, shutil as _sh
    import fixture_db as _fx
    import schema_v3 as _S
    _empty, _fdb = _tf.mkdtemp(), _fx.build()
    _keepdb, _keepvault = globals()["DB"], _S.VAULT
    try:
        globals()["DB"] = _fdb
        _S.VAULT = _empty
        _d = collect()["documents"]
        assert _d["vault_absent"] is True, "an empty vault was not recognised as absent"
        assert _d["deleted"] == [], f"an empty vault still reported {len(_d['deleted'])} deletions"
        #  The other direction —— a vault that really has the documents is not called absent.
        _real = _tf.mkdtemp()
        import lancedb as _lc
        for _r in _lc.connect(_fdb).open_table("documents").search().limit(99).to_list():
            _fp = os.path.join(_real, _r["path"])
            os.makedirs(os.path.dirname(_fp), exist_ok=True)
            open(_fp, "w", encoding="utf-8").write("x" * 200 + "\n")
        _S.VAULT = _real
        assert collect()["documents"]["vault_absent"] is False, \
            "a vault holding the documents was called absent"
        #  …and a vault holding **only some** of them is neither absent nor a mass deletion.  This
        #  runs the real collect(), so blanking the computation is caught here and not only in
        #  recommend()'s hand-built dicts.
        _rows = _lc.connect(_fdb).open_table("documents").search().limit(99).to_list()
        if len(_rows) > 1:
            _part = _tf.mkdtemp()
            _fp = os.path.join(_part, _rows[0]["path"])
            os.makedirs(os.path.dirname(_fp), exist_ok=True)
            open(_fp, "w", encoding="utf-8").write("x" * 200 + "\n")
            _S.VAULT = _part
            _dd = collect()["documents"]
            assert _dd["vault_absent"] is False, "a partly visible vault was called absent"
            assert _dd["vault_shrunk"] is True, \
                "most of the documents missing did not read as a shrunken vault"
            _sh.rmtree(_part, ignore_errors=True)

        #  ⚠ **Exactly at the line.**  The arm above samples one point (2 of 3 missing against a
        #     boundary of 1.5), so `>` and `>=` agree there and swapping them survived the whole
        #     self-check (measured 2026-09-04).  With three documents no integer can land on the
        #     boundary at all, so this builds a **four**-row table: half missing is 2 against 2,
        #     where `>` says "not shrunken" and `>=` says "shrunken" —— `sync` on one side, `vault`
        #     on the other, so the operator is the entire answer.
        #       ⚠ `meta` is not optional in the fixture: `collect()` returns `_empty_status` unless
        #         both tables exist, and a first version without it reported `indexed=0`, which made
        #         every assertion below pass against a computation that never ran.  Paths are flat
        #         and `schema_v3.VAULT` is rebound too, because `is_skipped` resolves against **its**
        #         global —— with either wrong, nothing matches and `deleted` is simply everything.
        import schema_v3 as _sv
        _bdir, _bvault = _tf.mkdtemp(), _tf.mkdtemp()
        _bdb = _lc.connect(_bdir)
        _bdb.create_table("documents", data=[
            {"doc_id": f"b{i}", "path": f"n{i}.md", "no_llm": False,
             "title": f"n{i}", "mtime": 1.0, "sha": "b" * 8} for i in range(4)])
        _bdb.create_table("meta", data=[{"key": "built_at", "value": "0"}])
        _keepsv = _sv.VAULT
        try:
            globals()["DB"], _S.VAULT, _sv.VAULT = _bdir, _bvault, _bvault
            for _n in ("n0.md", "n1.md"):
                open(os.path.join(_bvault, _n), "w", encoding="utf-8").write("x" * 200 + "\n")
            _bd = collect()["documents"]
            assert len(_bd["deleted"]) == 2, \
                f"the fixture is not on the boundary —— deleted={len(_bd['deleted'])}, expected 2"
            assert _bd["vault_shrunk"] is False, (
                "half the documents missing read as a shrunken vault —— the comparison is `>` "
                "(more than half), and `>=` would advise `vault` on an ordinary half-empty scan")
            #  …and one past the line fires, or the assertion above is satisfied by a computation
            #  that never returns True at all.
            os.remove(os.path.join(_bvault, "n1.md"))
            assert collect()["documents"]["vault_shrunk"] is True, \
                "three of four missing did not read as a shrunken vault"
        finally:
            _sv.VAULT = _keepsv
            _sh.rmtree(_bvault, ignore_errors=True)
            _sh.rmtree(_bdir, ignore_errors=True)

        _sh.rmtree(_real, ignore_errors=True)
    finally:
        globals()["DB"], _S.VAULT = _keepdb, _keepvault
        _sh.rmtree(_empty, ignore_errors=True)
        _sh.rmtree(_fdb, ignore_errors=True)
    #     The dict-only arm below stays as well: recommend() must keep telling the two apart,
    #     and it is the part the screen actually reads.  A container that mounts no vault
    #     deleted —— measured "deleted 1115" plus a high-severity "run sync", which would have
    #     rebuilt the index down to nothing.  Both directions are checked: a real deletion must
    #     still be reported, or the guard would be a blanket that hides the thing it guards.
    absent = {"documents": {"indexed": 1115, "added": [], "modified": [], "deleted": [],
                            "vault_absent": True},
              "kg": {"extract_drift": [], "stale": [], "stale_ratio": 0.0, "warn_ratio": 0.2},
              "artifacts": {"stale": []}}
    got = recommend(absent)
    assert any(a["step"] == "vault" for a in got), "an absent vault produced no advice"
    assert not any(a["step"] == "sync" for a in got), \
        "an absent vault still advised sync —— that empties the index"

    #  ⚠ **Zero visible is not the only shape.**  One surviving file disarmed the check: with
    #     1,115 indexed and 1 visible the board said "deleted 1,114" and advised sync, which
    #     rebuilds the index down to that one document.  Three rows, so the boundary is pinned
    #     from both sides —— a guard that fired on ordinary edits would be removed at once.
    for _del, _want_vault, _want_sync in ((2, False, True),        # an ordinary editing session
                                          (557, False, True),      # just under the line
                                          (558, True, False),      # just over it
                                          (1114, True, False)):    # a mount that half-appeared
        _d = {"documents": {"indexed": 1115, "added": [], "modified": [],
                            "deleted": [f"{i}.md" for i in range(_del)],
                            "vault_absent": False,
                            "vault_shrunk": _del > 1115 * VAULT_SHRINK_RATIO},
              "kg": {"extract_drift": [], "stale": [], "stale_ratio": 0.0, "warn_ratio": 0.2},
              "artifacts": {"stale": []}}
        _steps = [a["step"] for a in recommend(_d)]
        assert ("vault" in _steps) is _want_vault, f"{_del} deleted: vault advice wrong ({_steps})"
        assert ("sync" in _steps) is _want_sync, f"{_del} deleted: sync advice wrong ({_steps})"
    real = {"documents": {"indexed": 1115, "added": [], "modified": [], "deleted": ["a.md", "b.md"],
                          "vault_absent": False},
            "kg": {"extract_drift": [], "stale": [], "stale_ratio": 0.0, "warn_ratio": 0.2},
            "artifacts": {"stale": []}}
    got = recommend(real)
    assert any(a["step"] == "sync" for a in got), "a real deletion stopped being reported"
    assert not any(a["step"] == "vault" for a in got), "a healthy vault was called absent"
    print("  ✅ status self-check —— an absent vault is told apart from a mass deletion (both ways)")
    print("  ✅ status self-check —— tells a missing table (normal) from a failed query (an error)")


def main():
    if "--selftest" in sys.argv:
        _c = _fixture_if_no_db()
        try:
            _selftest()
        finally:
            _c()
        return
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    st = collect()
    st["recommend"] = recommend(st)
    if a.json:
        print(json.dumps(st, ensure_ascii=False, indent=1))
        return

    if st.get("empty"):
        #  **Not having run anything yet is not a failure.**  It guides and exits 0.
        print(f"\n  knowledge DB — {DB.replace(os.path.expanduser('~'), '~')}")
        print(f"    has not been built yet.")
        print(f"\n  What to run next")
        print(f"    just plan          # see how long it will take first (changes nothing)")
        print(f"    just run index     # read the notes and build the knowledge DB\n")
        return

    db, d, k, ar = st["db"], st["documents"], st["kg"], st["artifacts"]

    #  ⚠ If `KAL_DIR` in `.env` points somewhere wrong, docker **creates that path without a
    #    word** and the screen shows "0 documents" —— the user thinks the index is gone.
    #    This check lived in `set_vault.check_data_dir()` and was called only from
    #    `just vault`.  Where the user actually sees "0" is here and in the web UI
    #    (deep review 2026-08-25 —— the function's own docstring said exactly that).
    try:
        import set_vault
        for _msg in set_vault.check_data_dir():
            print(f"\n  ⚠ {_msg}")
    except Exception:
        pass                            # status must appear even if the check cannot run
    print(f"\n  knowledge DB — {db['path'].replace(os.path.expanduser('~'), '~')}")
    print(f"    last rebuild  {_ago(db['built_at'])}"
          f"  ({time.strftime('%Y-%m-%d %H:%M', time.localtime(db['built_at'])) if db['built_at'] else '-'})")
    print(f"    disk          {db['disk_bytes']/1e6:.0f}MB · embedding {db['embedding_model']}")
    print(f"    documents {d['indexed']:,}  ({' · '.join(f'{x} {y}' for x, y in d['origin'].items())})")
    print(f"    entities {k['entities']:,} · relations {k['relations']:,} "
          f"· chunks {db['tables'].get('chunks', 0):,}")

    print(f"\n  vault comparison")
    if not (d["added"] or d["modified"] or d["deleted"]):
        print("    ✅ the index matches the vault")
    else:
        for lbl, xs in (("added", d["added"]), ("modified", d["modified"]), ("deleted", d["deleted"])):
            if xs:
                print(f"    {lbl} {len(xs)}: " + " · ".join(xs[:3])
                      + (f" … and {len(xs)-3} more" if len(xs) > 3 else ""))

    print(f"\n  knowledge graph")
    print(f"    last extraction  {_ago(k['extracted_at'])}")
    if k["extract_drift"]:
        print(f"    ⚠ {len(k['extract_drift'])} documents changed since extraction: "
              + " · ".join(k["extract_drift"][:3]))
    if k["stale"]:
        by = collections.Counter(r["reason"] for r in k["stale"])
        print(f"    stale documents {len(k['stale'])} / {d['indexed']} = {k['stale_ratio']*100:.1f}% "
              f"({' · '.join(f'{x} {y}' for x, y in by.most_common())})")
    else:
        print("    ✅ no stale documents")

    if ar["stale"]:
        print(f"\n  artifacts\n    ⚠ older than the DB: {' · '.join(ar['stale'])}")

    print_recommend(st["recommend"])
    print()


def print_recommend(recs):
    """Print the advice.  **It is a separate function so that it can be tested** ——
    inlined in main(), the self-check can only see a *stand-in* like `STEP_BY_ID.get(...)`
    and passes even when the real output path is broken.  That happened (mutation, 2026-08-21).
    """
    print(f"\n  What to run next")
    if not recs:
        print("    ✅ nothing to run right now.")
    else:
        mark = {"high": "🔴", "medium": "🟡", "low": "⚪"}
        for r in recs:
            #  Some advice has an empty `step` —— "the judgement itself failed, so do not
            #  trust the numbers below".  There is no step to run, hence no title or command.
            #  ⚠ It used to index straight in with `STEP_BY_ID[r["step"]]` and died with
            #    **KeyError('')**.  The very channel built to say "these numbers are not
            #    trustworthy" killed the exact command you run when you doubt them.
            #    (`--json` returns earlier, so the web and API were fine and only the CLI
            #    died —— which is why it took even longer to find.)  2026-08-21
            s = STEP_BY_ID.get(r["step"])
            if s is None:
                print(f"    {mark[r['severity']]} {r['why']}")
                continue
            print(f"    {mark[r['severity']]} {s['title']}  (~{s['minutes']} min)")
            print(f"       {r['why']}")
            print(f"       python {' '.join(s['cmd'])}")


if __name__ == "__main__":
    main()
