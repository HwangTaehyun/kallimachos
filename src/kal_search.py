#!/usr/bin/env python3
"""knowledge DB search — a 4-way fusion of BM25 + chunk vectors + entity vectors + relation vectors.

CLI:
  kal_search.py "how is the inverted index built"
  kal_search.py "US-China AI competition" --mode graph --top 20
  kal_search.py "pm2 pitfalls" --origin vault --json

Modes (all measured — tune_alpha.py · ablate_params.py):
  default  BM25 .18 · chunk .05 · entity .18 · relation .59   ← the default (rationale in the PRESETS comment)
  graph    BM25 .20 · chunk .00 · entity .30 · relation .50   only with a freshly extracted KG
  keyword  BM25 only                                          when you know the exact term
  vector   chunk vectors only                                 meaning without a KG
  legacy   the pre-tuning baseline — for comparison

Per-query automatic mode switching was **removed**.  Measured, the rule did harm
(always-default 0.776 vs the auto rule 0.759, p=0.0027).
"""
import os, sys, json, argparse, collections, functools

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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import lancedb

DB = os.environ.get("KAL_PATH", os.path.join(KAL_HOME, "db"))
_M = None

# Candidate depth — measured by ablate_params.py ②.  300/300/30/40 is best at 0.776.
# 120/120 (the old default) gives 0.769; growing entity/relation to 60/80 makes it worse, 0.756.
# RRF accumulates, so more relation candidates let weakly matched documents push out the top.
DEPTH = {"bm25": 300, "chunk": 300, "entity": 30, "relation": 40}
# 1-hop expansion factor — measured by ablate_params.py ①.
#   0.0 → 0.730 (Δ-0.039, p<0.001)   turning expansion off is clearly worse
#   0.5 → 0.769                       the value originally picked arbitrarily
#   1.0 → 0.774 (Δ+0.005, p=0.034)   there is no basis for discounting an indirect path
HOP = 1.0
# How many documents to hand over as evidence — ablate_params.py ③.  Recall@20 saturates at 0.939.
# N=10 misses 21% of the relevant documents.
ANSWER_N = 20

PRESETS = {                       # (bm25, chunk, entity, relation)
    # ── Measured (tune_alpha.py · 58 queries · a 991-pair gold set) ──
    #
    # ⚠ **Read which corpus these numbers came from first.**  This spot used to say "a
    #   distilled corpus of 376 documents", and that was false (adversarial review 2026-08-18):
    #     · the default mode is precompute(vault_only=True) and scores only the **98 documents**
    #       with origin=="vault".  The 278 session documents are deleted before scoring.
    #     · --full-corpus keeps 376 as candidates, but condensed scoring removes unjudged
    #       documents from the ranking —— so the session documents drop out again.  Measured:
    #       **43.8% of the default weights' top 10 are session documents**, and condensed removes them free.
    #     · the 991-pair gold set covers only **94 documents**.  **75% of the 376 in production have no judgement.**
    #   So **session search quality has nothing to do with these numbers.**  Production nDCG@10
    #   lies somewhere in [0.549, 0.806] (lower = sessions counted irrelevant, upper = ignored),
    #   and that span is roughly 15× the effect sizes argued over below.  README.md's "session
    #   search quality is effectively unverified" is this same point, and it applies to these weights.
    #
    # The ranking of each component run alone is solid (full conditions):
    #   chunk vectors only 0.689 · BM25 only 0.713 · entities only 0.752 · relation vectors only 0.776
    #   → chunk vectors are **the weakest signal**.  Weighting them heavily makes the blend worse.
    #     chunk 0.00→0.803 · 0.05→0.804 · 0.20→0.787 · 0.40→0.769 · 0.80→0.723
    #   This curve is deterministic (no random element) and reproduces.
    #
    # ⚠ But **the case for choosing 0.05 is weak.**  From the adversarial review of 2026-08-18:
    #
    #   ① the value is not on the grid.  grid()'s chunk values are {0, .20, .35, .45, .55, .70, .80},
    #      so (0.18, 0.05, 0.18, 0.59) appears nowhere among the 146 grid points.
    #      Therefore "146 grid points · 139 survive BH · significant in 19 of 20 splits" is all
    #      **a statement about grid points, not about this default.**
    #
    #   ② the edge over 0.20 is Δ+0.017 · 95% CI [+0.002,+0.034] · Wilcoxon p=0.059 ·
    #      dz 0.277 (small) · 25 improved, 18 worsened, 15 unchanged across 58 queries.
    #      Exactly the magnitude limitation L4 records as "a +0.02-class effect cannot be
    #      settled at n=58".  p did not clear 0.05.  The CI excluding 0 is a bootstrap of the
    #      **mean** while p is a **rank** test — they do not measure the same thing.  Not a contradiction; unrelated.
    #
    #   ③ what used to be written here, "1st in 3 of 4 staleness scenarios", **does not hold.**
    #      That table was **a single draw** with documents randomly hidden.  Re-measured across
    #      8 seeds, the gap disappears into the variance:
    #                       8-seed mean   seed range   gap
    #        20% stale   .05 0.7673      0.065        0.0065   ← the variance is 10× larger
    #                   .20 0.7608   0.049
    #        50% stale   .05 0.7277      0.055        0.0027   ← the variance is 20× larger
    #                   .20 0.7304   0.033
    #      That is, this table cannot distinguish 0.05 from 0.20.  "It flips here" fails the same way.
    #      Only a healthy KG (0.804 vs 0.787) and total absence (0.711 vs 0.714) are decisive,
    #      and in the latter 0.20 is marginally better.
    #
    # So the honest summary is this:
    #   **not "0.05 is better", but "it leads narrowly in the healthy state and is never worse,
    #     so it was picked out of a tie."**  To revert, (0.15,0.20,0.15,0.50) is also defensible.
    #   Settling it needs more queries (L4), or a staleness simulation rebuilt over repeated seeds.
    #
    #   baseline (BM25 .2 / chunk .8)   nDCG@10 0.707
    #   default                         nDCG@10 0.804   ← off-grid.  See ①–③ above
    "default": (0.18, 0.05, 0.18, 0.59),
    "graph":   (0.20, 0.00, 0.30, 0.50),   # chunks excluded entirely.  When the KG is known fresh
    "keyword": (1.00, 0.00, 0.00, 0.00),   # only when you know the exact term
    "vector":  (0.00, 1.00, 0.00, 0.00),   # meaning without a KG
    "legacy":  (0.20, 0.80, 0.00, 0.00),   # the pre-tuning baseline — for comparison
}
# Automatic mode selection was taken out.  Measured with tune_alpha.py --check-auto:
#   always-default 0.776 · auto (rule) 0.759 · oracle ceiling 0.792
#   auto vs the best fixed  Δ-0.017  p=0.0027  → the rule **does harm**
# Even the oracle is only +0.033, so per-query mode switching is not a useful lever on this corpus.
def model():
    global _M
    if _M is None:
        from sentence_transformers import SentenceTransformer
        db = lancedb.connect(DB)
        meta = {r["key"]: r["value"] for r in db.open_table("meta").search().limit(99).to_list()}
        _M = SentenceTransformer(meta.get("embedding_model", "intfloat/multilingual-e5-small"))
    return _M


def _expected_model():
    """Which model the index was built with.  Querying with a different one **silently** degrades search."""
    try:
        db = lancedb.connect(DB)
        meta = {r["key"]: r["value"] for r in db.open_table("meta").search().limit(99).to_list()}
        return meta.get("embedding_model", "intfloat/multilingual-e5-small")
    except Exception:
        return "intfloat/multilingual-e5-small"


def _remote_encode(url, q):
    """Ask the shared embedding service (cloud/embed/server.py, proto: kal.embed.v1.EmbedService).

    Why it exists —— loading the model in this process takes RSS from 106MB to 1,051MB and the
    first query to 8.0 seconds (measured 2026-08-29).  In hosting, where a process runs per user,
    that 1GB multiplies by the user count.  Self-hosting sets nothing here and keeps loading in
    process exactly as before —— the default behaviour does not change.
    """
    import json as _json
    import urllib.request
    body = _json.dumps({"queries": [q]}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/kal.embed.v1.EmbedService/Embed", data=body,
                                 headers={"Content-Type": "application/json"})
    timeout = float(os.environ.get("KAL_EMBED_TIMEOUT", "30"))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = _json.load(r)
    got = out.get("model", "")
    want = _expected_model()
    if got != want:
        #  Fail loudly rather than degrade quietly —— mixed vectors make results odd with no traceable cause.
        raise RuntimeError(f"the embedding model differs from the index: service {got!r} vs index {want!r}")
    vecs = out.get("vectors") or []
    if not vecs:
        raise RuntimeError("the embedding service returned no vector")
    return np.asarray(vecs[0]["values"], dtype="float32")


@functools.lru_cache(maxsize=128)
def encode_query(q):
    """Query embedding.  **Cached within the process.**

    A single `kal_search` was embedding the same query twice —— once in `search()`, and again
    when calling `snippets()` with its results.  Measured at 12.5ms × 2, so 12% of the 103ms
    warm latency went into rebuilding a value already in hand.

    Why a cache rather than threading it through as a parameter: it leaves the callers'
    contract alone, and future callers are fixed automatically too.  The model is pinned
    offline (HF_HUB_OFFLINE), so the same string always gives the same vector —— there is no
    case in which the cache can be wrong.

    **Read the return value only.**  It hands back the cached array itself.  `.tolist()` and
    `np.array(...)` copy, so anything after those is safe.
    """
    #  With a shared embedding service, ask it (hosting).  Without one, load here as before (self-hosting).
    url = os.environ.get("KAL_EMBED_URL")
    if url:
        return _remote_encode(url, q)
    return model().encode(["query: " + q], normalize_embeddings=True)[0]


def fuse(norm, w):
    """Weighted sum of the normalised component scores —— **the answer this system produces.**

    ⚠ `zip` silently truncates to the shorter side.  A missing component or weight raises
      nothing, and the result is still 20 good-looking documents —— right paths, right titles,
      plausible order.  The default weights are (bm25 .18, chunk .05, entity .18, relation .59),
      so **relations alone carry 59%**.  Drop the last two and 77% of the weight disappears
      while the screen looks identical.  Noticing it takes re-running `tune_alpha.py` against
      the 991-pair gold set —— a research script nobody calls.  (r4-guard mutation, 2026-08-21)

    So a length mismatch is **blocked**.  Silent truncation is this function's only trap.
    """
    #  The count comes from `PRESETS`.  Checking only that the two match **each other** is not
    #  enough —— a caller slicing both the same way, as `norm[:2], w[:2]`, still matches, and
    #  that was exactly the mutation r4-guard planted.  Adding a component now requires editing
    #  PRESETS too, and that is the right coupling.
    n_want = len(next(iter(PRESETS.values())))
    assert len(norm) == len(w) == n_want, \
        f"{len(norm)} components · {len(w)} weights · PRESETS has {n_want} —— " \
        f"zip would truncate silently"
    keys = set().union(*[set(n) for n in norm]) if norm else set()
    return {k: sum(wi * n.get(k, 0.0) for n, wi in zip(norm, w)) for k in keys}


def minmax(d):
    if not d:
        return {}
    v = list(d.values())
    lo, hi = min(v), max(v)
    if hi - lo < 1e-12:
        return {k: 1.0 for k in d}
    return {k: (x - lo) / (hi - lo) for k, x in d.items()}


class KAL:
    def __init__(self, path=DB):
        self.db = lancedb.connect(path)
        self.C = self.db.open_table("chunks")
        self.D = {r["doc_id"]: r for r in
                  self.db.open_table("documents").search().limit(999999).to_list()}
        self.c2d = {}
        self.has_kg = True
        try:
            self.E = self.db.open_table("lr_entities")
            self.R = self.db.open_table("lr_relations")
            self.ent_docs, self.ent_deg = {}, {}
            #  `.select()` fetches **only the columns read**.  Otherwise it drags all 8,179
            #  384-float vectors across, uses three fields and throws the rest away.
            #  Measured: 127.0ms → 29.4ms (4.3×).  The result dict is identical for all 8,179 rows.
            #  KAL() runs once per process, so this is 1% of a 9.4s CLI —— not a big win, but
            #  it is one line.  (r4-perf, 2026-08-21)
            for e in self.E.search().select(
                    ["entity_id", "doc_ids", "degree"]).limit(999999).to_list():
                self.ent_docs[e["entity_id"]] = list(e["doc_ids"])
                self.ent_deg[e["entity_id"]] = e["degree"]
        except Exception:
            self.has_kg = False
        self.meta = {r["key"]: r["value"] for r in
                     self.db.open_table("meta").search().limit(99).to_list()}

    def _doc_of(self, cid):
        if cid not in self.c2d:
            self.c2d[cid] = cid // 10000
        return self.c2d[cid]

    def bm25(self, q, k=120, where=None):
        try:
            s = self.C.search(q, query_type="fts").limit(k)
            if where:
                s = s.where(where)
            rows = s.to_list()
        except Exception:
            return {}
        out = {}
        for r in rows:
            d = r["doc_id"]
            out[d] = max(out.get(d, -1e9), float(r.get("_score", 0)))
        return out

    def chunk_vec(self, qv, k=120, where=None):
        s = self.C.search(qv).limit(k)
        if where:
            s = s.where(where)
        out = {}
        for r in s.to_list():
            sc = 1.0 - float(r.get("_distance", 0)) / 2.0
            d = r["doc_id"]
            out[d] = max(out.get(d, -1e9), sc)
        return out

    def entity_vec(self, qv, k=DEPTH["entity"], min_degree=1):
        if not self.has_kg:
            return {}
        out = collections.defaultdict(float)
        q = self.E.search(qv)
        if min_degree > 1:
            q = q.where(f"degree >= {min_degree}")     # ⚠ no index — this is a full scan
            # (degree is absent from schema_v3's INDEXES.  Confirmed by measurement)
        for rank, e in enumerate(q.limit(k).to_list(), 1):
            w = 1.0 / (60 + rank)
            for d in e["doc_ids"]:
                out[d] += w
        return dict(out)

    def relation_vec(self, qv, k=DEPTH["relation"], min_degree=1):
        if not self.has_kg:
            return {}
        out = collections.defaultdict(float)
        rows = self.R.search(qv).limit(k * 4 if min_degree > 1 else k).to_list()
        if min_degree > 1:
            g = self.ent_deg
            rows = [r for r in rows
                    if max(g.get(r["src_id"], 0), g.get(r["tgt_id"], 0)) >= min_degree][:k]
        for rank, r in enumerate(rows, 1):
            w = 1.0 / (60 + rank)
            for d in r["doc_ids"]:
                out[d] += w
            for eid in (r["src_id"], r["tgt_id"]):
                for d in self.ent_docs.get(eid, []):
                    out[d] += w * HOP
        return dict(out)

    def search(self, q, mode="default", top=ANSWER_N, origin=None, weights=None,
               min_degree=1):
        """min_degree — drop thinly connected entities and relations from the graph components.
        It exists to keep concepts mentioned once in passing out as noise.
        BM25 and chunk vectors are unaffected (they read the document body)."""
        w = weights or PRESETS.get(mode, PRESETS["default"])
        # origin is denormalised into chunks, so a BITMAP index filters it directly.
        # (It used to require listing hundreds of doc_ids in an IN clause)
        where = f"origin = '{origin}'" if origin else None
        qv = encode_query(q).tolist()
        parts = [self.bm25(q, k=DEPTH["bm25"], where=where),
                 self.chunk_vec(qv, k=DEPTH["chunk"], where=where),
                 self.entity_vec(qv, k=DEPTH["entity"], min_degree=min_degree),
                 self.relation_vec(qv, k=DEPTH["relation"], min_degree=min_degree)]
        #  ⚠ The filtering happens **before normalisation**.  It used to come after fuse, and
        #     `where` reaches bm25 and chunk but not entity and relation, so **a document about
        #     to be discarded set the min-max maximum** and then vanished.  Those two components
        #     are 0.77 of the default weights, so a document the user explicitly excluded was
        #     deciding the order of the ones that remained.
        #     Measured (deep review 2026-08-25, domain lens): under `--origin vault`, 5 of 7
        #     queries had a different top 10 and 2 had their first place flipped.
        if origin:
            parts = [{d: v for d, v in p.items()
                      if self.D.get(d, {}).get("origin", "vault") == origin}
                     for p in parts]
            #  ⚠ After filtering, a component **may have only 1–2 survivors.**  minmax then makes
            #     the lone survivor 1.0, and since the relation component carries 0.59, a document
            #     at 1.7% of the original maximum takes first place —— measured (2026-08-25, round 2):
            #     under `"cloudflare tunnel" --origin vault` an unrelated document led at 0.590.
            #     A component with fewer survivors than this **is not entitled to decide the order.**
            #
            #     ⚠ This guard applies **only when origin was filtered**.  Unfiltered, component
            #       size is set by DEPTH (300/300/30/40), and zeroing that on a small corpus kills
            #       the KG entirely on a freshly built vault.  The self-check also verifies
            #       "relations change the order" with a 2-element component.
            #  ⚠ Applied **to the KG components (entity, relation) only**.  bm25 and chunk are
            #     already the same population via `where` at the DB level, and emptying all four
            #     makes `--origin` return **zero results** on a vault of 2–3 documents ——
            #     the very first thing a user who just ran `just init` meets (measured, round 3).
            MIN_POP = 3
            parts = parts[:2] + [x if len(x) >= MIN_POP else {} for x in parts[2:]]
        sc = fuse([minmax(p) for p in parts], w)
        ranked = sorted(sc.items(), key=lambda t: -t[1])[:top]
        return [{"doc_id": d, "score": round(s, 4),
                 "path": self.D.get(d, {}).get("path", "?"),
                 "title": self.D.get(d, {}).get("title", "?"),
                 "origin": self.D.get(d, {}).get("origin", "vault"),
                 "abs_path": self.D.get(d, {}).get("abs_path", "")} for d, s in ranked], mode, w

    def snippets(self, q, doc_ids, n=2, width=340):
        """Evidence fragments for an answer — the chunk closest to the query, per document.

        What used to be one query per document is now **a single `IN`.**  Measured: 57.3ms →
        4.0ms for 20 documents (14×).  Those 20 held 112 rows between them, so the cost was
        not the data but the round trips themselves.  56% of a warm 103ms `kal_search` was here.

        A long `IN` clause breaks the query, so it is cut into batches of 500 —— the same idiom
        as `kal_mcp.py:554`.  (`search()`'s `where` avoids IN thanks to the denormalised
        `origin`, but this takes a list of doc_ids, which is a different problem.)
        """
        ids = [int(d) for d in doc_ids]
        if not ids:
            return {}
        qv = np.array(encode_query(q))            # copy the cached value —— the original is untouched
        by = {}
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            rows = self.C.search().where(
                f"doc_id IN ({','.join(str(x) for x in part)})"
            ).limit(400 * len(part)).to_list()
            for r in rows:
                by.setdefault(r["doc_id"], []).append(r)
        out = {}
        for d in ids:                              # preserve the order the caller gave
            rows = by.get(d)
            if not rows:
                continue
            V = np.array([r["vector"] for r in rows])
            idx = np.argsort(-(V @ qv))[:n]
            out[d] = [rows[i]["text"][:width] for i in idx]
        return out


def _selftest():
    # ── ① minmax —— the first stage of fusing every component ─────────
    assert minmax({}) == {}, "it blows up on empty input"
    assert minmax({"a": 5}) == {"a": 1.0}, "a single element must be 1.0"
    assert minmax({"a": 0, "b": 10}) == {"a": 0.0, "b": 1.0}
    flat = minmax({"a": 3, "b": 3})
    assert flat == {"a": 1.0, "b": 1.0}, f"all-equal must give 1.0: {flat}"

    # ── ② encode_query —— one search was embedding the same query twice ──
    calls = []
    class _M:
        def encode(s, texts, **k):
            calls.append(texts[0])
            return [[float(len(texts[0]))]]
    global model
    _orig_model = model
    globals()["model"] = lambda: _M()
    try:
        encode_query.cache_clear()
        a1 = encode_query("the same query")
        a2 = encode_query("the same query")
        assert len(calls) == 1, f"the same query is encoded {len(calls)} times (expected 1)"
        assert a1 == a2, "the cache returns a different value"
        encode_query("a different query")
        assert len(calls) == 2, "the cache swallows a different query"
        assert calls[0].startswith("query: "), "the e5 prefix is missing —— search quality degrades silently"
    finally:
        globals()["model"] = _orig_model
        encode_query.cache_clear()

    # ── ③ snippets —— IN-clause batching and grouping per doc ─────────
    # The 500 boundary is never reached by a real search (top 20).  Only here can it be.
    seen_wheres = []
    class _C:
        def search(s): return s
        def where(s, w): seen_wheres.append(w); return s
        def limit(s, n): return s
        def to_list(s):
            ids = [int(x) for x in seen_wheres[-1].split("(")[1].rstrip(")").split(",")]
            # ⚠ Returned **deliberately reversed**.  A real DB does not guarantee the order of
            #   an IN clause either.  A fake that returns them in order cannot test "does it
            #   preserve the caller's order" —— the first version did that, and a mutation that
            #   ignored the order passed.
            # Two chunks per document.  The vectors are 1-D, so the dot product is the value.
            return [{"doc_id": d, "vector": [float(i)], "text": f"d{d}c{i}"}
                    for d in reversed(ids) for i in (1, 2)]
    kal = KAL.__new__(KAL)
    kal.C = _C()
    globals()["model"] = lambda: _M()
    try:
        encode_query.cache_clear()
        out = kal.snippets("q", list(range(1200)), n=1)
        assert len(seen_wheres) == 3, f"1200 must split into 3 batches of 500, got {len(seen_wheres)}"
        assert len(out) == 1200, f"only {len(out)} documents came back (expected 1200)"
        # The **closest** chunk from each document —— a larger vector means a larger dot product
        assert out[7] == ["d7c2"], f"the per-document top pick is wrong: {out[7]}"
        # Even when the DB reverses them, the result must follow **the caller's order**
        assert list(out)[:3] == [0, 1, 2], f"the caller's order is not preserved: {list(out)[:3]}"
        assert kal.snippets("q", [], n=1) == {}, "it blows up on an empty list"
    finally:
        globals()["model"] = _orig_model
        encode_query.cache_clear()    # ── Score fusion —— the answer this system produces ──
    #    Only the recently changed pieces (minmax · the query cache · IN batching) were being
    #    checked.  Nobody was watching **the arithmetic that combines them**.  Throwing away
    #    77% of the weight with `zip(norm[:2], w[:2])` still yields 20 good-looking results (measured, r4-guard).
    _n = [{"a": 1.0, "b": 0.0}, {"a": 0.0, "b": 1.0},
          {"a": 0.5, "b": 0.5}, {"b": 1.0}]
    _w = [0.18, 0.05, 0.18, 0.59]
    _got = fuse(_n, _w)
    #  Must equal the hand-computed value —— one missing component changes it
    assert abs(_got["a"] - (0.18 * 1.0 + 0.05 * 0.0 + 0.18 * 0.5 + 0.59 * 0.0)) < 1e-12, _got
    assert abs(_got["b"] - (0.18 * 0.0 + 0.05 * 1.0 + 0.18 * 0.5 + 0.59 * 1.0)) < 1e-12, _got
    #  The relation component (59%) really does dominate the order —— remove it and it flips
    assert _got["b"] > _got["a"], "the relation weight is not reflected in the order"
    #  On a length mismatch it must die, **not truncate silently**
    #  A component count differing from PRESETS must be caught —— **even when both sides are sliced alike**
    for bad in ((_n[:2], _w), (_n, _w[:2]), (_n[:2], _w[:2])):
        try:
            fuse(*bad)
            raise SystemExit("mismatched component/weight lengths still pass —— zip truncates silently")
        except AssertionError:
            pass
    #  Does the real search path use this function (not a stand-in)
    #  ★ **Run `search()`'s body for real.**  Calling `fuse` directly cannot see a caller that
    #    slices both sides alike, as `parts[:2], w[:2]` (measured: that mutation escaped twice).
    #    The principle of using neither the model nor the DB still holds —— the component
    #    methods are replaced with fakes.
    _k = KAL.__new__(KAL)
    _k.D = {1: {"path": "a.md", "title": "A", "origin": "vault"},
            2: {"path": "b.md", "title": "B", "origin": "vault"}}
    _k.bm25 = lambda q, k=0, where=None: {1: 1.0, 2: 0.0}
    _k.chunk_vec = lambda qv, k=0, where=None: {1: 0.0, 2: 1.0}
    _k.entity_vec = lambda qv, k=0, min_degree=1: {1: 1.0, 2: 1.0}
    _k.relation_vec = lambda qv, k=0, min_degree=1: {2: 1.0}
    _enc = globals()["encode_query"]
    globals()["encode_query"] = lambda q: np.zeros(4)
    try:
        _hits, _mode, _wu = _k.search("q", top=2)
    finally:
        globals()["encode_query"] = _enc
    _by = {h["doc_id"]: h["score"] for h in _hits}
    #  after minmax: bm25{1:1,2:0} chunk{1:0,2:1} entity{1:1,2:1} relation{2:1}
    _w4 = PRESETS["default"]
    _e1 = _w4[0] * 1 + _w4[1] * 0 + _w4[2] * 1 + _w4[3] * 0
    _e2 = _w4[0] * 0 + _w4[1] * 1 + _w4[2] * 1 + _w4[3] * 1
    assert abs(_by[1] - round(_e1, 4)) < 1e-9 and abs(_by[2] - round(_e2, 4)) < 1e-9, \
        f"search()'s fusion differs from the hand calculation: {_by} vs ({_e1:.4f}, {_e2:.4f})"
    assert _hits[0]["doc_id"] == 2, "the relation component (59%) cannot change the order"
    #  ── Once origin is filtered, a KG component left alone must not decide the order ──
    #     ⚠ Checked **through `search()`**.  It first called `fuse` directly and reimplemented
    #        the guard, which means deleting the guard still passes —— the exact mistake this
    #        file warns about above (confirmed by mutation, 2026-08-25 round 3).
    _k3 = KAL.__new__(KAL)
    _k3.D = {1: {"path": "a.md", "title": "A", "origin": "vault"},
             2: {"path": "b.md", "title": "B", "origin": "session"},
             3: {"path": "c.md", "title": "C", "origin": "vault"},
             4: {"path": "d.md", "title": "D", "origin": "vault"}}
    _k3.bm25 = lambda q, k=0, where=None: {1: 0.9, 3: 0.5, 4: 0.4}
    _k3.chunk_vec = lambda qv, k=0, where=None: {1: 0.8, 3: 0.6, 4: 0.3}
    _k3.entity_vec = lambda qv, k=0, min_degree=1: {1: 0.2, 3: 0.1, 4: 0.05}
    #  The relation component holds **exactly one** vault document (the rest are session)
    _k3.relation_vec = lambda qv, k=0, min_degree=1: {2: 0.7, 4: 0.012}
    _enc3 = globals()["encode_query"]
    globals()["encode_query"] = lambda q: np.zeros(4)
    try:
        _h3, _, _ = _k3.search("q", top=4, origin="vault")
    finally:
        globals()["encode_query"] = _enc3
    assert _h3, "filtering by origin wiped out the results entirely"
    #  ⚠ Check **the filter itself**.  The first-place assertion below only exercises the
    #     MIN_POP guard, so deleting the whole `if origin:` block passed —— the guard was kept
    #     while what it wraps could vanish unnoticed (confirmed by mutation, 2026-08-25 round 4).
    #     Document 2 in `_k3` is a session document and tops the relation component at 0.7.
    assert all(h["doc_id"] != 2 for h in _h3), \
        f"a session document got into a --origin vault result: {[h['doc_id'] for h in _h3]}"
    assert {h["doc_id"] for h in _h3} <= {1, 3, 4}, \
        f"a non-vault document came back: {[h['doc_id'] for h in _h3]}"
    assert _h3[0]["doc_id"] == 1, \
        f"a lone relation component (0.012) took first place: {[(h['doc_id'], h['score']) for h in _h3]}"

    #  ── Check **the filter itself** ──────────────────────────────────
    #  `_k3` above cannot see it: what removes the session document (2) there is not the filter
    #  but **MIN_POP** (the relation component has only 2 entries and empties wholesale).  So
    #  deleting the `if origin:` block still passed `_k3` —— the guard was kept while what it
    #  wraps could vanish unnoticed (confirmed by mutation, 2026-08-25 round 4).
    #
    #  Here the entity component gets 4 documents so it clears MIN_POP (3).  One of them is a
    #  session document with **the highest score** —— without the filter it shows in the result.
    #  ⚠ The fake `bm25`/`chunk_vec` ignore `where`.  A real DB does filter with `where`, but
    #     that reaches only those two components and never the KG ones —— which is exactly why
    #     this Python filter exists, and why the situation is constructed here.
    _k5 = KAL.__new__(KAL)
    _k5.D = {1: {"path": "a.md", "title": "A", "origin": "vault"},
             2: {"path": "b.md", "title": "B", "origin": "vault"},
             3: {"path": "c.md", "title": "C", "origin": "vault"},
             9: {"path": "s.md", "title": "S", "origin": "session"}}
    _k5.bm25 = lambda q, k=0, where=None: {1: 0.9, 2: 0.5, 3: 0.4}
    _k5.chunk_vec = lambda qv, k=0, where=None: {1: 0.8, 2: 0.6, 3: 0.3}
    #  Entity component: 4 entries (clearing MIN_POP), with session document 9 scoring highest
    _k5.entity_vec = lambda qv, k=0, min_degree=1: {9: 0.99, 1: 0.2, 2: 0.1, 3: 0.05}
    _k5.relation_vec = lambda qv, k=0, min_degree=1: {9: 0.99, 1: 0.2, 2: 0.1, 3: 0.05}
    globals()["encode_query"] = lambda q: np.zeros(4)
    try:
        _h5, _, _ = _k5.search("q", top=4, origin="vault")
    finally:
        globals()["encode_query"] = _enc3
    _ids5 = [h["doc_id"] for h in _h5]
    assert 9 not in _ids5, \
        f"--origin vault returned a session document —— the filter is not running: {_ids5}"
    assert _ids5, "the filter killed the results entirely"

    #  ── Check MIN_POP from **above** as well ──────────────────────────
    #  `_k3` below only catches MIN_POP being too **low**.  Measured (2026-08-25 round 4):
    #  `MIN_POP=99` and `=2` both sailed through the self-check.  At 99 the KG components are
    #  **entirely** emptied on any vault, so entities and relations contribute nothing to the
    #  order —— no error is raised and search quality just dies quietly.
    #
    #  So the check is "a healthy component (4 documents ≥ MIN_POP) **must stay alive**".
    #  On bm25 and chunk alone, document 1 leads; 4 wins only if the entity component survives.
    _k6 = KAL.__new__(KAL)
    _k6.D = {i: {"path": f"{i}.md", "title": str(i), "origin": "vault"} for i in (1, 2, 3, 4)}
    _k6.bm25 = lambda q, k=0, where=None: {1: 0.9, 2: 0.8, 3: 0.7, 4: 0.6}
    _k6.chunk_vec = lambda qv, k=0, where=None: {1: 0.9, 2: 0.8, 3: 0.7, 4: 0.6}
    _k6.entity_vec = lambda qv, k=0, min_degree=1: {4: 0.99, 3: 0.02, 2: 0.01, 1: 0.0}
    _k6.relation_vec = lambda qv, k=0, min_degree=1: {4: 0.99, 3: 0.02, 2: 0.01, 1: 0.0}
    globals()["encode_query"] = lambda q: np.zeros(4)
    try:
        _h6, _, _ = _k6.search("q", top=4, origin="vault")
    finally:
        globals()["encode_query"] = _enc3
    assert _h6[0]["doc_id"] == 4, \
        ("a 4-element KG component (≥ MIN_POP) failed to contribute to the order —— MIN_POP is too high: "
         f"{[(h['doc_id'], round(h['score'], 3)) for h in _h6]}")
    #  ⚠ What these two checks cover (measured): MIN_POP=1 is caught by `_k3` below, 5 and 99
    #     by this one.  **2 and 4 are not caught** —— they are ±1 around 3, neither plainly
    #     wrong, and pinning that far would make the test a copy of the constant.  The two
    #     dangerous directions (a disarmed guard · a dead KG) are closed.

    #  A small vault must not end up with zero results —— a freshly built vault is exactly that
    _k4 = KAL.__new__(KAL)
    _k4.D = {1: {"path": "a.md", "title": "A", "origin": "vault"},
             2: {"path": "b.md", "title": "B", "origin": "vault"}}
    _k4.bm25 = lambda q, k=0, where=None: {1: 1.0, 2: 0.2}
    _k4.chunk_vec = lambda qv, k=0, where=None: {1: 0.3, 2: 0.9}
    _k4.entity_vec = lambda qv, k=0, min_degree=1: {1: 0.5}
    _k4.relation_vec = lambda qv, k=0, min_degree=1: {}
    globals()["encode_query"] = lambda q: np.zeros(4)
    try:
        _h4, _, _ = _k4.search("q", top=2, origin="vault")
    finally:
        globals()["encode_query"] = _enc3
    assert len(_h4) == 2, f"--origin killed the results on a 2-document vault: {_h4}"

    #  Every preset must have the same component count —— one short one truncates silently in that mode
    _lens = {len(v) for v in PRESETS.values()}
    assert len(_lens) == 1, f"the weight count differs between presets: " + str(
        {k: len(v) for k, v in PRESETS.items()})



    #  ⑳ **The shared embedding service.**  The path that asks over HTTP instead of holding the model here (hosting only).
    #     Measured (2026-08-29): loading in-process gives RSS 1,051MB and a first query of 8.0s; remote gives 124MB and 0.43s.
    #     What is checked here is not performance but **two safeguards**:
    #       · does a vector actually come back
    #       · does a **different model** from the index fail loudly —— degrade quietly and nobody finds the cause
    import json as _json
    import threading as _th
    from http.server import BaseHTTPRequestHandler as _BH, HTTPServer as _HS

    _want = _expected_model()
    _state = {"model": _want}

    class _Stub(_BH):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            _n = int(self.headers.get("Content-Length") or 0)
            _req = _json.loads(self.rfile.read(_n) or b"{}")
            _vecs = [] if _state.get("empty") else [{"values": [0.1] * 384} for _ in _req.get("queries", [])]
            _out = _json.dumps({"vectors": _vecs, "model": _state["model"], "dim": 384}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(_out)))
            self.end_headers()
            self.wfile.write(_out)

        def log_message(self, *a):
            pass

    _srv = _HS(("127.0.0.1", 0), _Stub)
    _t = _th.Thread(target=_srv.serve_forever, daemon=True)
    _t.start()
    try:
        _url = "http://127.0.0.1:%d" % _srv.server_address[1]
        _v = _remote_encode(_url, "a query")
        assert len(_v) == 384 and abs(float(_v[0]) - 0.1) < 1e-6, f"remote embedding returns no vector: {_v[:3]}"
        _state["empty"] = True
        try:
            _remote_encode(_url, "a query")
            raise AssertionError("an empty response passed —— searching with a 0 vector is meaningless")
        except RuntimeError as _e:
            assert "vector" in str(_e), f"the error message does not name the cause: {_e}"
        _state["empty"] = False
        _state["model"] = "somebody/else-model"
        try:
            _remote_encode(_url, "a query")
            raise AssertionError("a model differing from the index passed —— search degrades quietly")
        except RuntimeError as _e:
            assert "model" in str(_e), f"the error message does not name the cause: {_e}"
        #  ⚠ Check **the wiring** too.  The two checks above call `_remote_encode` directly, so
        #     deleting the KAL_EMBED_URL branch inside `encode_query` still passes —— and hosting
        #     then quietly reloads 1GB per user (confirmed by mutation, 2026-08-29).  Set only the env var and call encode_query.
        _state["model"] = _want
        #  Make **loading the model explode** —— reading `_M` would depend on order (an earlier test already fills it).
        _saved = os.environ.get("KAL_EMBED_URL")
        _saved_model = globals()["model"]

        def _boom():
            raise AssertionError("it loaded the model despite using the remote —— the per-user 1GB is still there")

        os.environ["KAL_EMBED_URL"] = _url
        globals()["model"] = _boom
        try:
            _v2 = encode_query("a wiring-check query %d" % _srv.server_address[1])
            assert abs(float(_v2[0]) - 0.1) < 1e-6, "encode_query does not use the remote service"
        finally:
            globals()["model"] = _saved_model
            if _saved is None:
                del os.environ["KAL_EMBED_URL"]
            else:
                os.environ["KAL_EMBED_URL"] = _saved
    finally:
        _srv.shutdown()
        _srv.server_close()

    """The two things changed today —— the query embedding cache and snippets' IN batching.

    Neither the model nor the DB is used.  `KAL.__new__` builds the instance and the tables are faked.
    """

    print("  ✅ kal_search self-check —— minmax · query cache · IN batching (1200→3) · shared embedding (vector · model mismatch)")


if __name__ == "__main__":
    #  ⚠ Blocked only here.  Put in `KAL.__init__`, `estimate.py:166` —— which wraps this in
    #     `except Exception` to mean "no DB, so everything is new" —— was pierced by
    #     `SystemExit` (a BaseException) and the self-check died.
    #     **A library constructor does not raise SystemExit** (2026-08-25).
    if "--selftest" not in sys.argv:
        import db_ready
        db_ready.require(DB, ["documents", "chunks"], "search")
    if "--selftest" in sys.argv:
        _selftest(); sys.exit(0)
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--mode", default="default", choices=list(PRESETS))
    ap.add_argument("--top", type=int, default=ANSWER_N)
    ap.add_argument("--origin", choices=["vault", "session"])
    ap.add_argument("--min-degree", type=int, default=1,
                    help="exclude entities/relations with fewer than N connections from the graph components (default 1 = all)")
    ap.add_argument("--snippets", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    kal = KAL()
    res, mode, w = kal.search(a.query, a.mode, a.top, a.origin,
                              min_degree=a.min_degree)
    if a.json:
        payload = {"query": a.query, "mode": mode,
                   "weights": {"bm25": w[0], "chunk": w[1], "entity": w[2], "relation": w[3]},
                   "results": res}
        if a.snippets:
            sn = kal.snippets(a.query, [r["doc_id"] for r in res[:5]])
            for r in payload["results"]:
                r["snippets"] = sn.get(r["doc_id"], [])
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        print(f'"{a.query}"   mode={mode}  '
              f'weights BM25 {w[0]} · chunk {w[1]} · entity {w[2]} · relation {w[3]}'
              + (f' · degree>={a.min_degree}' if a.min_degree > 1 else '') + '\n')
        for i, r in enumerate(res, 1):
            tag = "📓" if r["origin"] == "vault" else "💬"
            print(f'  {i:>2}. [{r["score"]:.3f}] {tag} {r["path"]}')
        if a.snippets:
            print()
            sn = kal.snippets(a.query, [r["doc_id"] for r in res[:3]])
            for r in res[:3]:
                for s in sn.get(r["doc_id"], []):
                    print(f'  › {r["path"][:44]}')
                    print(f'    {s[:300]}'.replace("\n", " "))
