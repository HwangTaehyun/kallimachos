#!/usr/bin/env python3
"""Export LanceDB's entities and relations as JSON for the Galaxy View plugin to read.

Why it is needed
  Galaxy View draws the vault's **note link graph** (resolvedLinks).  What we want to see is
  not notes but **entities and relations**.  So the plugin gets one more data source, and the
  file it reads is built here.

  Making kg/ notes so Obsidian reads them as [[links]] is another option, but that inflates the
  vault by 700 pages and buries the curated notes.  This path leaves the vault untouched.

Output location
  <vault>/.obsidian/plugins/kal-galaxy/kal-graph.json
  Inside the plugin folder, so Obsidian's note search and graph never pick it up.

Usage:
  python export_kal_graph.py                    # everything with degree>=1
  python export_kal_graph.py --min-degree 2
"""
import vault_path
import os, re, sys, json, shutil, argparse, collections
import lancedb


# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
# Community colours are kept apart from type colours so they cannot clash.  8 types against
COMM_COLORS = [
    # Golden-angle (137.5°) traversal with 3-way alternation of lightness and saturation.
    # A hand-picked palette produced pairs at ΔE 6.7 (measured), so different communities looked alike.
    # Communities are ordered by descending size, so ids 0,1,2… are the large ones — the golden
    # angle spreads those first ids as far apart on the wheel as it can, separating exactly the ones that dominate the screen.
    "#eb7a7a", "#2cf266", "#cda5e9", "#ebdd7a", "#2cd1f2",
    "#e9a5ca", "#96eb7a", "#342cf2", "#e9bca5", "#7aebc1",
    "#e12cf2", "#dae9a5", "#7ab2eb", "#f22c55", "#a5e9ab",
    "#a57aeb", "#f2b02c", "#a5e9e6", "#eb7acf", "#86f22c",
]
LABEL_CACHE = os.path.join(KAL_HOME, "comm_label_cache.jsonl")

#  ⚠ The label language is **English**, matching the rest of the UI. It used to ask for Korean,
#     which meant the web UI and plugin panels were fully English except for this one list of
#     topic names coming out of the data layer (2026-09-01).
#     Changing this invalidates LABEL_CACHE entries — the cache key is a sha1 of the member names,
#     not of the prompt, so **old Korean labels keep being served**. Delete
#     ~/.kal/comm_label_cache.jsonl to re-label.
LABEL_SYS = """You name clusters in a knowledge graph. Below are the entities in one cluster
(most-connected first). Judge **what they cover together** and output exactly one JSON object.

{"label": "...", "summary": "..."}

label   2-5 words. The subject this cluster covers. English. Keep proper nouns in their original
        form (including non-English ones).
        Do not just list entity names — those are already on screen.
        No abstract flourishes. "Search system design" yes, "A journey through knowledge" no.
summary One sentence (under 90 characters). What you see when you open this cluster.

Output nothing but the JSON."""


def label_communities(big, E, workers=6):
    """An LLM writes a label for each community.

    Why an LLM — 'qmd · BM25 · Hermes' tells you what is inside but not **what they deal with
    together**.  That is a judgement no list of names can make.  The entity list stays on as a
    subtitle so verification is still possible.

    The cache key is a hash of the top member names **plus a fingerprint of the prompt**.  A
    boundary can shift a little without a new call as long as the top members are the same

    ⚠ Why the prompt goes into the key: it used to be member names alone.  So editing the prompt
      **kept returning the old answer** —— when the label language changed from Korean to
      English the screen stayed Korean, and it only took effect if a person remembered to delete
      the cache file (2026-09-01).  "Invalidation a person has to remember" is eventually not
      done.  Now a changed prompt changes the key and it is called again automatically.
    """
    import hashlib, json as _json
    from concurrent.futures import ThreadPoolExecutor
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from lr_extract import call_text

    cache = {}
    if os.path.exists(LABEL_CACHE):
        for line in open(LABEL_CACHE, encoding="utf-8"):
            try:
                r = _json.loads(line); cache[r["k"]] = r["v"]
            except Exception:
                pass

    #  The prompt fingerprint —— this has to be in the key for a prompt change to invalidate the cache.
    psig = hashlib.sha1(LABEL_SYS.encode()).hexdigest()[:8]

    jobs = []
    for i, c in enumerate(big):
        top = sorted(c, key=lambda n: -E[n]["degree"])[:25]
        names = [E[n]["name"] for n in top]
        key = hashlib.sha1(("|".join(names[:12]) + "\x00" + psig).encode()).hexdigest()[:16]
        jobs.append((i, key, names, sorted(c, key=lambda n: -E[n]["degree"])[:3]))

    todo = [j for j in jobs if j[1] not in cache]
    print(f"  cluster labels — {len(jobs)} (cached {len(jobs)-len(todo)} · new {len(todo)})")

    def work(job):
        _, key, names, _ = job
        raw = call_text(f"{LABEL_SYS}\n\nEntities: {', '.join(names)}")
        m = re.search(r"\{[\s\S]*?\}", raw or "")
        if not m:
            return key, None
        try:
            d = _json.loads(m.group(0))
            return key, {"label": str(d.get("label", ""))[:40],
                         "summary": str(d.get("summary", ""))[:120]}
        except Exception:
            return key, None

    if todo:
        with ThreadPoolExecutor(max_workers=workers) as ex, \
             open(LABEL_CACHE, "a", encoding="utf-8") as fh:
            for key, v in ex.map(work, todo):
                if v:
                    cache[key] = v
                    fh.write(_json.dumps({"k": key, "v": v}, ensure_ascii=False) + "\n")

    out = []
    for i, key, names, top3 in jobs:
        v = cache.get(key)
        members = " · ".join(E[n]["name"] for n in top3)
        out.append({"label": (v or {}).get("label") or members,   # on failure, fall back to the name list
                    "summary": (v or {}).get("summary", ""),
                    "members": members})
    return out


#  ⚠ 'Other' is a **contract with the UI**, not a display string. plugin/src/layout/groupIndex.ts
#     (`OTHER_GROUP`), plugin/src/data/kdbGraph.ts and ControlPanel.ts special-case exactly this
#     string to sort the bucket to the bottom. It read the Korean word for "other" here while the UI was renamed to
#     'Other', so that special-casing silently stopped working (2026-09-01). Keep both sides equal.
MIN_COMM = 30          # clusters smaller than this fold into 'Other' (465 cannot all be listed)
TOP_COMM = 20          # the most that go into the filter list


def communities(n_nodes, edges):
    """Louvain clustering → (community id per node, community metadata).

    Why communities as well as types
      A type (concept/tool/…) says **what something is**; a community says **what it is used
      alongside**.  'BM25' and 'LanceDB' have different types (concept/tool) and live in the
      same subject.  Measured modularity 0.864 — this graph really does have distinct subject clumps.

    The names come from label_communities() —— **an LLM writes the subject name** and the top 3
    entities by degree ride along as a subtitle for verification.  (This docstring said "the LLM
    is not consulted" for a while.  It was missed when LLM labelling arrived.)
    """
    if "--no-communities" in sys.argv:
        print("  · --no-communities — exporting without communities (the viewer's community mode is disabled)")
        return [-1] * n_nodes, []
    try:
        import networkx as nx
    except ImportError:
        # ⚠ Returning quietly here exports **a graph with no communities at all** as a clean
        #   exit.  The viewer's community mode, colours and labels all die while the exit code
        #   is 0, so automation (refresh_kg.py) records a success.  It fails instead.
        raise SystemExit(
            "networkx is missing, so communities cannot be built.\n"
            "  uv add networkx      # or  .venv/bin/pip install networkx\n"
            "  To export without communities, pass --no-communities")
    G = nx.Graph()
    G.add_nodes_from(range(n_nodes))
    G.add_edges_from(edges)
    parts = nx.community.louvain_communities(G, seed=7)   # a fixed seed = reproducible
    parts = sorted(parts, key=len, reverse=True)
    mod = nx.community.modularity(G, parts)
    big = [c for c in parts if len(c) >= MIN_COMM][:TOP_COMM]
    cid = [-1] * n_nodes                                   # -1 = Other
    for i, c in enumerate(big):
        for n in c:
            cid[n] = i
    print(f"  {len(parts)} communities · modularity {mod:.3f} · "
          f"{len(big)} in the filter (covering {sum(len(c) for c in big):,} nodes)")
    return cid, big

DB = os.environ.get("KAL_PATH", os.path.join(KAL_HOME, "db"))
# Where the vault lives.  Mounted at /vault inside the container (see docker-compose).
VAULT = vault_path.vault()
#  ⚠ **The canonical copy lives in KAL_HOME, not in the vault.**  It used to be written only into
#     `<vault>/.obsidian/plugins/kal-galaxy/`, and that one location decided a deployment shape:
#     the api container had to mount the vault **just to serve the galaxy view**, because
#     `api/main.go` read the file from there (its only use of the vault mount).
#
#     Nothing in the file justifies that.  It is built from LanceDB (`source: "lancedb"`), and the
#     document references inside it are **vault-relative** —— 371 in `entities[].docs[]`, 208 in
#     `relations[].docs[]`, none absolute (measured 2026-09-02).  The file is portable; only the
#     *reader* was not: an Obsidian plugin cannot open a file outside its own vault.
#
#     So: KAL_HOME holds the canonical copy (already mounted at /data/kal), and a vault copy is
#     written **only when the vault really is an Obsidian vault** —— it has a `.obsidian/`.  Since
#     the vault may now be an openwiki bundle, writing there would create a plugin folder inside a
#     git repository no plugin will ever open.
VAULT_DEST = ".obsidian/plugins/kal-galaxy/kal-graph.json"
DEST = VAULT_DEST                      # kept: the self-check pins the plugin id against this
HOME_DEST = os.path.join(KAL_HOME, "graph_export", "kal-graph.json")


def is_obsidian_vault(root):
    """Does this folder have a `.obsidian/` —— i.e. will a plugin ever read a file placed in it."""
    return os.path.isdir(os.path.join(root, ".obsidian"))

# Must be the same set as schema_v3.CANON_TYPES —— a type with no colour turns grey in the viewer.


from lr_extract import SUMMARY_MAX_CHARS   # the profile cap is set in one place only
from schema_v3 import llm_gate, REDACTED, TYPE_COLOR   # the transmission gate too


def clean(s, limit=SUMMARY_MAX_CHARS):
    """Cut a paragraph made of stitched-together fragment descriptions down to something readable.
    After lr_extract's profile summary has run, most of them are already one paragraph.

    The cap must equal lr_extract.SUMMARY_MAX_CHARS.  Left at 600, it re-cut profiles already
    summarised to 900 characters and appended a '…', so cards showed text broken mid-sentence
    (measured: 'Taehyun Hwang' truncated at 601 characters).
    It was a value left over from before the summariser existed."""
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s[:limit].rstrip() + ("…" if len(s) > limit else "")


def build(min_degree):
    db = lancedb.connect(DB)
    #  Only the columns read.  384-float vectors are not dragged along.
    #  ⚠ `doc_ids` is read by the transmission gate, so it is required on **both** sides ——
    #    without it this becomes `llm_gate(None, blocked)` and everything is blocked (silently).
    #  ⚠ `documents` is not projected: the fake table the self-check installs has no
    #    `.select()`, and at 376 rows there is nothing to gain.
    E = db.open_table("lr_entities").search().select(
        ["entity_id", "name", "type", "description", "doc_ids", "degree"]
    ).limit(999999).to_list()
    R = db.open_table("lr_relations").search().select(
        ["src_id", "tgt_id", "doc_ids", "description", "keywords"]
    ).limit(999999).to_list()
    D = {r["doc_id"]: r for r in
         db.open_table("documents").search().limit(999999).to_list()}

    # ── The transmission gate ──────────────────────────────────────────
    # This JSON leaves through an unauthenticated `GET /api/graph` and also ships in the plugin.
    # MCP blocks derived text drawn from `no_llm` documents (kal_mcp.gate) and there was none of
    # that here —— not two implementations of one rule but **only one side of it**.  With 0
    # no_llm documents in the vault today it is latent, but the moment the first one appears MCP
    # blocks and the web passes.  The policy is decided in schema_v3.llm_gate alone.
    #
    # ⚠ Filter **before building `idx`.**  Filtering afterwards throws rels' s/t indices out of step.
    blocked = {d["doc_id"] for d in D.values() if d.get("no_llm")}
    if blocked:
        keep = []
        for e in E:
            v = llm_gate(e.get("doc_ids"), blocked)
            if v == "block":
                continue                      # drop the whole entity —— the name is derived too
            if v == "redact":
                e = dict(e); e["description"] = REDACTED
            keep.append(e)
        print(f"   {len(blocked)} document(s) excluded from transmission → {len(E) - len(keep)} entity(ies) dropped")
        E = keep

    E = [e for e in E if e["degree"] >= min_degree]
    idx = {e["entity_id"]: i for i, e in enumerate(E)}
    R = [r for r in R if r["src_id"] in idx and r["tgt_id"] in idx]
    if blocked:
        kept = []
        for r in R:
            v = llm_gate(r.get("doc_ids"), blocked)
            if v == "block":
                continue
            if v == "redact":
                r = dict(r); r["description"] = REDACTED
            kept.append(r)
        R = kept

    cid, big = communities(len(E), [(idx[r["src_id"]], idx[r["tgt_id"]]) for r in R])

    ents = [{
        "id": e["entity_id"],
        "name": e["name"],
        "type": e["type"],
        "comm": cid[i],                      # cluster id (-1 = Other)
        "deg": e["degree"],
        "desc": clean(e["description"]),
        # A vault-relative path.  The plugin opens it directly with getAbstractFileByPath
        "docs": [D[x]["path"] for x in e["doc_ids"] if x in D and x not in blocked][:12],
    } for i, e in enumerate(E)]

    rels = [{
        "s": idx[r["src_id"]],
        "t": idx[r["tgt_id"]],
        "desc": clean(r["description"], 400),
        "kw": list(r["keywords"])[:6],
        "docs": [D[x]["path"] for x in r["doc_ids"] if x in D and x not in blocked][:8],
    } for r in R]

    meta = {r["key"]: r["value"] for r in
            db.open_table("meta").search().limit(99).to_list()}
    types = collections.Counter(e["type"] for e in E)
    # An LLM writes the community names (cached on a sha1 of the top-12 member names).  The top 3 ride along as a subtitle
    labels = label_communities(big, E)
    comms = []
    for i, c in enumerate(big):
        lb = labels[i]
        comms.append({"id": i, "count": len(c),
                      "name": lb["label"],           # the subject name the LLM wrote
                      "members": lb["members"],      # the 3 representative entities (a subtitle for verification)
                      "summary": lb["summary"],      # a one-sentence summary (shown on hover)
                      "color": COMM_COLORS[i % len(COMM_COLORS)]})
    n_other = sum(1 for x in cid if x < 0)
    if n_other:
        comms.append({"id": -1, "count": n_other, "name": "Other",
                      "members": "", "summary": "Entities that did not fall into a cluster",
                      "color": "#6b7280"})

    return {
        "version": 1,
        "source": "lancedb",
        "built_at": meta.get("built_at", ""),
        "min_degree": min_degree,
        "types": [{"name": k, "count": v, "color": TYPE_COLOR.get(k, TYPE_COLOR["other"])}
                  for k, v in types.most_common()],
        "communities": comms,
        "entities": ents,
        "relations": rels,
    }


def _fixture_if_no_db():
    """Swap in a **fixture** when there is no real DB.  Returns a cleanup function.

    The self-check was opening the real DB at `~/.kal` —— it ran only on the author's machine
    and died in CI with `Table 'lr_entities' was not found`.  `just` stops at the first
    failure, so the 20-odd self-checks after it never ran (reproduced 2026-08-25, round 4).

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


def _check_other_contract():
    """The 'Other' bucket name must be **byte-identical** on both sides.

    Why this is a check and not a comment: the UI special-cases the literal string to push that
    bucket to the bottom of the list. When the data layer said the Korean word for "other" and the UI said 'Other',
    nothing threw — the bucket just quietly stopped being special (2026-09-01). A mismatch here
    is invisible at runtime, which is exactly the class of defect this repository gates by machine.

    Needs no DB, so it runs in --selftest on any machine.
    """
    import re
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    here = open(os.path.abspath(__file__), encoding="utf-8").read()
    mine = re.search(r'"name": "([^"]+)",\n\s*"members": "", "summary"', here)
    if not mine:
        return ["export_kal_graph: could not find the 'Other' bucket name — has the shape changed?"]
    name = mine.group(1)
    bad = []
    ts = os.path.join(repo, "plugin/src/layout/groupIndex.ts")
    if os.path.exists(ts):
        m = re.search(r"OTHER_GROUP\s*=\s*[\'\"]([^\'\"]+)", open(ts, encoding="utf-8").read())
        if not m:
            bad.append("plugin/src/layout/groupIndex.ts has no OTHER_GROUP")
        elif m.group(1) != name:
            bad.append(f"'Other' bucket name differs: exporter {name!r} != OTHER_GROUP {m.group(1)!r} "
                       "— the UI stops special-casing it and nothing throws")
    return bad


def _selftest():
    """Does the transmission gate **actually run.**

    This vault has 0 documents with `no_llm: true`, so on a normal run the gate never executes
    once.  Delete the gate entirely and nothing breaks —— `kal_mcp` has a history of passing a
    mutation test for exactly that reason.
    So one document is **forcibly blocked here** and the result inspected.

    Three things are checked:
      ① an entity sourced only from that document drops out **entirely** (its name is derived too)
      ② an entity with other sources survives but has its description redacted
      ③ the blocked document's **path** appears nowhere (the path itself is information)
    """
    import collections
    db = lancedb.connect(DB)
    E = db.open_table("lr_entities").search().limit(999999).to_list()
    docs = db.open_table("documents").search().limit(999999).to_list()
    cnt = collections.Counter(x for e in E for x in (e.get("doc_ids") or []))
    tgt, n_ent = cnt.most_common(1)[0]
    tgt_path = next(d["path"] for d in docs if d["doc_id"] == tgt)

    class _T:
        def __init__(s, rows): s.rows = rows
        def search(s): return s
        def limit(s, n): return s
        def to_list(s): return s.rows
    class _DB:
        def __init__(s, d): s.d = d
        def open_table(s, n):
            if n != "documents":
                return s.d.open_table(n)
            rows = [dict(r) for r in docs]
            for r in rows:
                if r["doc_id"] == tgt:
                    r["no_llm"] = True
            return _T(rows)

    # ⚠ Clustering is **turned off.**  The gate test does not need communities, and leaving it
    #   on makes an LLM write community names.  With a warm cache that is 2 seconds, but the
    #   cache key is `sha1(the top 12 member names)`, so **one re-extraction invalidates all
    #   20**.  Then `just selftest` calls the LLM 20 times and fails inside a container, which
    #   has no claude auth, for reasons unrelated to the gate.  A self-check must be fast and dependency-free.
    import lancedb as _l
    _real = _l.connect
    _l.connect = lambda *a, **k: _DB(db)
    _argv = sys.argv
    sys.argv = list(_argv) + ["--no-communities"]
    try:
        g = build(min_degree=1)
    finally:
        _l.connect = _real
        sys.argv = _argv

    # ⚠ Do not count with `len(E) - len(result)` —— that mixes in what min_degree and the ghost
    #   cleanup removed (measured 605 = 385 from the gate + 220 from everything else).  To see
    #   the gate alone, **compute separately which entities the gate should catch** and compare only those.
    should_drop = {e["entity_id"] for e in E
                   if set(e.get("doc_ids") or ()) <= {tgt}}          # sourced from that document only
    kept = {e["id"] for e in g["entities"]}
    leaked = should_drop & kept
    red = sum(1 for e in g["entities"] if e["desc"] == REDACTED)
    paths = {p for e in g["entities"] for p in e["docs"]}

    assert should_drop, "the test is meaningless —— 0 entities are sourced only from that document"
    assert not leaked, \
        f"{len(leaked)} entity(ies) passed the gate —— sourced only from the blocked document and still exported"
    assert red > 0, "not one partially blocked entity had its description redacted"
    assert tgt_path not in paths, f"the blocked document's path survived in docs[]: {tgt_path}"
    print(f"  ✅ export gate —— 1 document blocked: of that document's {n_ent} entities, "
          f"all {len(should_drop)} sole-sourced ones dropped · {red} redacted · 0 path leaks")


def _check_plugin_id():
    """Does the id in `manifest.json` match **the install path the code uses**.

    Obsidian identifies a plugin by its folder name (= the id).  So when the id and the path
    diverge, the export reports success and the view is blank —— with no error.
    This repository was in that state (2026-08-25): the source read `kal-graph.json` while the
    vault held only `kdb-graph.json`, so a rebuild turned it off.

    The id **must not collide** with the upstream community store's `galaxy-view` (by Rick).
    A collision means two plugins fighting over the same folder.
    """
    import json
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    mid = json.loads(open(os.path.join(repo, "plugin", "manifest.json")).read())["id"]
    assert mid != "galaxy-view", \
        "the manifest id equals upstream's (Rick's) galaxy-view —— the install folders collide"

    want = f"plugins/{mid}/"
    #  Every file that hardcodes the path.  One divergence and the screen quietly goes blank.
    #  ⚠ The list is **not written by hand.**  It started as 7 typed entries and
    #     `api/main.go:462` was missing —— so the id changed and that one file kept reading the
    #     old path, and `/api/graph` returned 404.  No error, just no galaxy view.  The failure
    #     of keeping a list in two places was committed **by the very guard meant to prevent
    #     divergence** (2026-08-25).
    #     Now the repository is walked to **find** the files that use `plugins/<name>`.
    import subprocess
    out = subprocess.run(["git", "-C", repo, "grep", "-lE",
                          r'plugins[/"][ ,]*"?[A-Za-z0-9_-]+', "--",
                          "src", "api", "web", "plugin/src", "plugin/*.mjs", "skills"],
                         capture_output=True, text=True)
    owners = [f for f in out.stdout.split()
              #  Exclude this file itself —— the explanation above quotes the old id
              if f != "src/export_kal_graph.py"
              #  Build output and dependencies are not source
              and "/node_modules/" not in f and "/dist/" not in f]
    assert owners, "not one file using the install path was found —— the check is spinning"
    #  **Both syntaxes** that write the path are read.  Watching only slashes misses
    #  `os.path.join(..., "plugins", "galaxy-view", ...)` —— and the first version of this
    #  check really did let that form in `status.py` through (caught by a mutation test).
    forms = [r"plugins/([A-Za-z0-9_-]+)",
             r'"plugins"\s*,\s*"([A-Za-z0-9_-]+)"']
    bad = []
    for rel in owners:
        txt = open(os.path.join(repo, rel), encoding="utf-8").read()
        #  ⚠ The rule "it must use our id" was only right while the list was **hand-written**.
        #     Now that the repository is walked, files referring to someone else's plugin
        #     folder are caught too (the ghost-edges of `plugins/constellation`).
        #     The real invariant is just one: "do not use a name that is not ours".
        for rx in forms:
            for m in re.finditer(rx, txt):
                if m.group(1) not in (mid, "constellation"):  # constellation = someone else's plugin
                    bad.append(f"{rel}: plugins/{m.group(1)} (the manifest says {mid})")
    assert not bad, "an install path differs from the manifest id:\n  " + "\n  ".join(bad)

    #  DEST's folder segment must hold the same value (pinned separately from the grep above)
    assert f"/{mid}/" in "/" + DEST, f"DEST={DEST} differs from id={mid}"
    print(f"  ✅ plugin id matches —— {mid} ({len(owners)} file(s))")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        for _m in _check_other_contract():
            print(f"  ❌ {_m}")
            sys.exit(1)
        _c = _fixture_if_no_db()
        try:
            _check_plugin_id(); _selftest()
        #  ⚠ **`sys.exit(0)` must not sit inside a `finally`.**  Leaving through a finally
        #     **replaces** the exception in flight —— a broken assert still exits 0, and the
        #     self-check becomes structurally unable to fail.  This repository really was in
        #     that state (round 4 created it and round 5 caught it): the same commit used the
        #     correct shape in `export_webgl` and `status`.
        finally:
            _c()
        sys.exit(0)
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-degree", type=int, default=1)
    ap.add_argument("--vault", default=VAULT)
    ap.add_argument("--out", default=None)
    # An escape hatch for when networkx cannot be installed.  Without it, it fails rather than quietly exporting without communities
    ap.add_argument("--no-vault-copy", action="store_true",
                    help="do not write the Obsidian plugin's copy, even into a real vault")
    ap.add_argument("--no-communities", action="store_true",
                    help="skip Louvain clustering (the viewer's community mode is disabled)")
    a = ap.parse_args()

    data = build(a.min_degree)
    #  The canonical copy.  `--out` still overrides it, for experiments and for the self-check.
    path = a.out or HOME_DEST
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    mb = os.path.getsize(path) / 1e6
    #  The vault copy exists for one reader —— the Obsidian plugin —— so it is written only where
    #  that reader can exist.  `--no-vault-copy` is for a run that must not touch the vault at all.
    if not a.out and not a.no_vault_copy:
        if is_obsidian_vault(a.vault):
            vp = os.path.join(a.vault, VAULT_DEST)
            os.makedirs(os.path.dirname(vp), exist_ok=True)
            shutil.copyfile(path, vp)
            print(f"  → {vp}   (copy for the Obsidian plugin)")
        else:
            print(f"  ⓘ {a.vault} has no .obsidian/ —— no plugin copy written "
                  f"(an openwiki bundle is not an Obsidian vault)")
    print(f"  entities {len(data['entities']):,} · relations {len(data['relations']):,}"
          f"   (degree>={a.min_degree})")
    print(f"  types: " + " · ".join(f"{t['name']} {t['count']:,}" for t in data["types"]))
    for c in data["communities"][:6]:
        print(f"    community {c['id']:>2}  {c['count']:>4}  {c['name'][:26]:<28}{c.get('members','')[:34]}")
    print(f"  → {path}   {mb:.1f} MB")
