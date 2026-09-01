#!/usr/bin/env python3
"""Export LanceDB's entities and relations as a WebGL galaxy graph.

How it differs from export_graph.py
  That one draws 700 nodes in Canvas 2D and also writes Obsidian notes.
  This one draws **10,000 nodes in WebGL2**.  It reads LanceDB directly, bypassing Obsidian.
  (Obsidian's graph reads only [[links]], so representing this data would need copies as notes,
   and those copies inflated the vault to 1,196 pages and buried the curated notes.)

The layout is precomputed here
  With no runtime physics engine the browser only has to draw — a settled picture the moment it
  loads, 0 dependencies, 60fps even on weak hardware.  In exchange it is static: nodes cannot be dragged.

  Forces: edge springs (attraction) + local KDTree repulsion + gravity toward the origin.
      Instead of O(n²) global repulsion, a KDTree pushes only the neighbours within a radius (O(n log n) at n=10,000).

Usage:
  python export_webgl.py                      # everything with degree>=1
  python export_webgl.py --min-degree 3       # sparser
  python export_webgl.py --iters 400          # a longer layout
"""
import vault_path
import os, re, sys, json, base64, argparse, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import lancedb
from scipy.spatial import cKDTree


# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
# Where the vault lives.  Mounted at /vault inside the container (see docker-compose).
VAULT = vault_path.vault()
DB = os.environ.get("KAL_PATH", os.path.join(KAL_HOME, "db"))
OUT = os.environ.get("KAL_OUT", os.path.join(KAL_HOME, "graph_export"))

# There is exactly one palette, `schema_v3.TYPE_COLOR` —— a copy here would diverge (it did).
from schema_v3 import (TYPE_COLOR, IDENTITY_TYPES, count_identity,
                       llm_gate, REDACTED)
TYPE_ORDER = list(TYPE_COLOR)


def clean_desc(s, limit=240):
    """lr_extract's group_nodes joins the per-chunk descriptions with spaces into one paragraph.
    That is unreadable, so it is split by sentence, deduplicated, and only the first two are kept."""
    s = re.sub(r"\s+", " ", (s or "")).strip()
    if not s:
        return ""
    parts, seen = [], set()
    for p in re.split(r"(?<=[.。!?])\s+|\s{2,}", s):
        p = p.strip(" .·")
        if len(p) < 12:
            continue
        k = p[:40].lower()
        if k in seen:
            continue
        seen.add(k)
        parts.append(p)
        if len(parts) == 2:
            break
    out = ". ".join(parts) if parts else s
    return out[:limit].rstrip() + ("…" if len(out) > limit else "")


def load(min_degree):
    db = lancedb.connect(DB)
    #  Only the columns read —— otherwise the whole 384-float vector gets dragged along.
    #  ⚠ A relation's `doc_ids` **must** be included.  The transmission gate reads it, and
    #    without it this becomes `llm_gate(None, blocked)`; the empty set is a subset of
    #    anything, so **every relation is blocked** —— silently.  (The list an external review
    #    gave omitted this, because it was measured before the gate existed.  2026-08-21)
    E = db.open_table("lr_entities").search().select(
        ["entity_id", "name", "type", "description", "doc_ids", "degree"]
    ).limit(999999).to_list()
    R = db.open_table("lr_relations").search().select(
        ["src_id", "tgt_id", "doc_ids", "description"]
    ).limit(999999).to_list()
    D = {r["doc_id"]: r for r in
         db.open_table("documents").search().limit(999999).to_list()}
    #  ⛔ Anything drawn from a `no_llm: true` document is not exported.
    #
    #  This file had **no gate either.**  An earlier commit (df237b3) added the identity
    #  warning here and not the transmission gate —— the same file said "7,974 names are in
    #  here" while never filtering the documents forbidden from transmission.
    #
    #  The pipeline does not call it (it is in none of rebuild_all.sh, export_all.sh or
    #  status.STEPS).  But `docs/PIPELINE.md` still carries the command, so it is **one line away**.
    #  And the artifact `galaxy.html` is self-contained and built to be handed to people, with
    #  11× more names in it than graph3d.html.
    #
    #  The policy must be **the same across all three exports** —— it uses the same
    #  `schema_v3.llm_gate` as export_graph and export_kal_graph.  (r4-sec, round 5, 2026-08-21)
    blocked = {d["doc_id"] for d in D.values() if d.get("no_llm")}
    if blocked:
        keep_e = []
        for e in E:
            v = llm_gate(e.get("doc_ids"), blocked)
            if v == "block":
                continue                      # drop the whole entity —— the name is derived too
            if v == "redact":
                e = dict(e); e["description"] = REDACTED
            keep_e.append(e)
        print(f"   {len(blocked)} document(s) excluded from transmission → {len(E) - len(keep_e)} entity(ies) dropped")
        E = keep_e
        kept_r = []
        for r in R:
            v = llm_gate(r.get("doc_ids"), blocked)
            if v == "block":
                continue
            if v == "redact":
                r = dict(r); r["description"] = REDACTED
            kept_r.append(r)
        R = kept_r
        D = {k: v for k, v in D.items() if k not in blocked}   # the path is derived too

    E = [e for e in E if e["degree"] >= min_degree]
    keep = {e["entity_id"]: i for i, e in enumerate(E)}
    R = [r for r in R if r["src_id"] in keep and r["tgt_id"] in keep]
    return E, R, D, keep


def layout(n, src, dst, iters, seed=7):
    """3D force-directed.  Repulsion is local, through a KDTree — O(n²) is unusable at 10,000 nodes."""
    rng = np.random.default_rng(seed)
    pos = rng.normal(0, 30, (n, 3)).astype(np.float64)

    deg = np.bincount(np.concatenate([src, dst]), minlength=n).astype(np.float64)
    mass = 1.0 + deg                      # a hub is heavy and moves less → it stays central
    k = 12.0                              # the ideal spacing
    for it in range(iters):
        t = 1.0 - it / iters               # cooling
        f = np.zeros_like(pos)

        # ① attraction — edges as springs
        d = pos[dst] - pos[src]
        L = np.linalg.norm(d, axis=1, keepdims=True) + 1e-9
        a = d * (L / k) * 0.06
        np.add.at(f, src, a)
        np.add.at(f, dst, -a)

        # ② repulsion — only between neighbours within a radius.  Gravity handles the global case
        r_cut = k * 2.6
        tree = cKDTree(pos)
        pairs = tree.query_pairs(r=r_cut, output_type="ndarray")
        if len(pairs):
            if len(pairs) > 1_500_000:     # prevents an explosion during the early clumping
                pairs = pairs[rng.choice(len(pairs), 1_500_000, replace=False)]
            i, j = pairs[:, 0], pairs[:, 1]
            dv = pos[i] - pos[j]
            dist2 = (dv * dv).sum(1, keepdims=True) + 1e-6
            rep = dv * (k * k / dist2) * 0.9
            np.add.at(f, i, rep)
            np.add.at(f, j, -rep)

        # ③ gravity — so disconnected components do not fly off forever
        f -= pos * 0.008

        step = (f / mass[:, None]) * (2.0 + 8.0 * t)
        lim = k * (0.8 + 2.0 * t)          # the per-step movement cap
        sl = np.linalg.norm(step, axis=1, keepdims=True) + 1e-9
        step = step * np.minimum(1.0, lim / sl)
        pos += step

    pos -= pos.mean(0)
    pos /= (np.abs(pos).max() + 1e-9) / 100.0     # normalised to ±100
    return pos.astype(np.float32)


def b64(arr):
    return base64.b64encode(np.ascontiguousarray(arr).tobytes()).decode()


def build(min_degree, iters):
    t0 = time.time()
    E, R, D, keep = load(min_degree)
    n = len(E)
    print(f"  nodes {n:,} · edges {len(R):,}   (degree>={min_degree})")

    src = np.array([keep[r["src_id"]] for r in R], dtype=np.int32)
    dst = np.array([keep[r["tgt_id"]] for r in R], dtype=np.int32)
    print(f"  computing the layout… ({iters} iterations)")
    pos = layout(n, src, dst, iters)
    print(f"    {time.time()-t0:.1f}s")

    tidx = {t: i for i, t in enumerate(TYPE_ORDER)}
    types = np.array([tidx.get(e["type"], tidx["other"]) for e in E], dtype=np.uint8)
    degs = np.array([min(e["degree"], 65535) for e in E], dtype=np.uint16)

    docs = []
    for e in E:
        p = [D[x]["path"] for x in e["doc_ids"] if x in D][:4]
        docs.append(p)

    data = {
        "n": n, "m": len(R),
        # Used in the obsidian://open?vault=… link.  The vault folder name is the vault name.
        "vault": os.path.basename(os.path.expanduser(VAULT)),
        "pos": b64(pos),                                  # float32 [n,3]
        "type": b64(types),                               # uint8  [n]
        "deg": b64(degs),                                 # uint16 [n]
        "edge": b64(np.stack([src, dst], 1).astype(np.uint32)),   # uint32 [m,2]
        "name": [e["name"] for e in E],
        "desc": [clean_desc(e["description"]) for e in E],
        "docs": docs,
        "types": TYPE_ORDER,
        "colors": [TYPE_COLOR[t] for t in TYPE_ORDER],
        "counts": [int((types == i).sum()) for i in range(len(TYPE_ORDER))],
    }
    return data


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


def _selftest():
    """Does the transmission gate **actually run.**

    The vault has 0 `no_llm` documents, so on a normal run it never executes once —— which is
    how this file went without a gate.  One document is blocked by force and inspected.
    (The same approach as export_graph's and export_kal_graph's self-checks.)

    The layout is not run.  The gate is inside `load()`, and that is the extent of the test.
    """
    import collections, lancedb as _l
    _real = _l.connect
    _db = _real(DB)
    _E = _db.open_table("lr_entities").search().limit(999999).to_list()
    _docs = _db.open_table("documents").search().limit(999999).to_list()
    _tgt = collections.Counter(x for e in _E for x in (e.get("doc_ids") or [])).most_common(1)[0][0]
    _tgt_path = next(d["path"] for d in _docs if d["doc_id"] == _tgt)

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
            rows = [dict(r) for r in _docs]
            for r in rows:
                if r["doc_id"] == _tgt:
                    r["no_llm"] = True
            return _T(rows)

    _l.connect = lambda *a, **k: _DB(_db)
    try:
        _Eg, _Rg, _Dg, _keep = load(1)
    finally:
        _l.connect = _real

    _should = {e["entity_id"] for e in _E if set(e.get("doc_ids") or ()) <= {_tgt}}
    assert _should, "the test does not hold on this vault (no sole-sourced entity)"
    _kept = {e["entity_id"] for e in _Eg}
    assert not (_should & _kept), \
        f"{len(_should & _kept)} entity(ies) sourced only from the blocked document still go out"
    assert any(e["description"] == REDACTED for e in _Eg), "not one description was redacted"
    assert _tgt not in _Dg, "the blocked document survives in D —— its path goes out through docs[]"
    assert all(_tgt_path != (d.get("path") or "") for d in _Dg.values()), "the path survives"
    print(f"  ✅ export_webgl transmission gate (1 forced → {len(_should)} entity(ies) dropped · no path leak)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-degree", type=int, default=1)
    ap.add_argument("--iters", type=int, default=300)
    ap.add_argument("--out", default=None)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _c = _fixture_if_no_db()
        try:
            _selftest()
        finally:
            _c()
        sys.exit(0)

    os.makedirs(OUT, exist_ok=True)
    data = build(a.min_degree, a.iters)
    tpl = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "webgl_viewer.html"), encoding="utf-8").read()
    html = tpl.replace("__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    path = a.out or os.path.join(OUT, "galaxy.html")
    # The same treatment as graph3d.html —— this file has 0 dependencies too and is built **to
    # be handed on**, with the entity names inside it.  Yet the warning existed only on
    # graph3d (700 nodes) and not here (7,974 nodes).  The side carrying 11× more names was undefended.
    # A build log disappears when the terminal closes, so the notice is baked **into the file**.  (2026-08-21)
    #  ⚠ `data` holds no entity list —— only the shape the viewer uses (a type order plus a
    #    count array).  Counting `data["nodes"]` at first would have taken an empty list and
    #    printed **0**.  That is exactly the bug this commit fixes (an alarm that cannot sound).
    #    So it counts from the array the screen really uses, and an assertion confirms that array is complete.
    _idx = {t: i for i, t in enumerate(data["types"])}
    n_all = len(data["name"])
    n_id = sum(data["counts"][_idx[t]] for t in IDENTITY_TYPES if t in _idx)
    assert sum(data["counts"]) == n_all, \
        f"type total {sum(data['counts'])} ≠ nodes {n_all} —— the warning's number cannot be trusted"
    id_lbl = "·".join(IDENTITY_TYPES)
    html += (f"\n<!--\n  ⚠ This file is self-contained.  Handing it on carries {n_all} entity"
             f" names with it\n     ({id_lbl} {n_id} — real names and organisations possible).\n"
             f"  Credential masking has been applied, but identity information is not checked.\n-->\n")
    open(path, "w", encoding="utf-8").write(html)
    print(f"\n  {path}   {os.path.getsize(path)/1e6:.1f} MB")
    print(f"  Open it in a browser (0 dependencies)")
    print(f"     ⚠ {n_all} entity name(s) ({id_lbl} {n_id}) are inside the file")
