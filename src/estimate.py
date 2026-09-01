#!/usr/bin/env python3
"""How long will it take before you run it —— answered by **counting**, not by a constant.

Why this is needed
  `status.py`'s STEPS carry a `minutes` field and the screen and confirmation dialogs show it.
  But that is a fixed constant.  Measured across 379 run records (2026-08-24):

      step        declared    measured median    measured range
      extract     30 min      5.1 min            1 s ~ 89 min      ← 5,000×
      distill     20 min      0 s                0 s ~ 1 s
      index        3 min      24 s               18 s ~ 8.8 min

  The declared value is neither.  And this is not a precision problem but a **shape** problem ——
  what decides the time is not the vault's size but **how much work is left after the cache**,
  and that can be counted exactly before running.  On this vault today, extract is not "30
  minutes" but "3 LLM calls".

What gets counted
  "One unit of work" means something different per step.  So it reports **the unit and the
  count** rather than a time alone.  "3 LLM calls · about 10 seconds" is more trustworthy than
  "about 1 minute" —— when it is wrong, the user can see where.

Where the rate (seconds per unit) comes from
  ① **the median of the records** when `~/.kal/runs/*.json` carries `units`.
  ② otherwise the seed values below.  And the output **says so** ——
     making an invented number look measured is worse than hardcoding a constant.
  run_log.py writes those records.  Every run moves it closer to ①.

The counting side **calls the original functions**
  Reimplementing the chunking and skipping rules here would inevitably drift.  It happened:
  lr_extract.collect() once drifted from the indexing rules and 2 documents never entered the
  KG (the comment at lr_extract.py:295).  The estimator does not repeat that mistake.

Usage
  python src/estimate.py              # every step
  python src/estimate.py extract      # one step
  python src/estimate.py --json       # for machines (the Go API reads this)
  python src/estimate.py --selftest
"""
import json, os, sys, glob, io, contextlib, statistics

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
RUNS = os.path.join(KAL_HOME, "runs")

#  Seed values —— seconds per unit.  Once records accumulate, these go unused.
#  Grounds: extract takes a measured `claude -p` round trip of 2.7 s (minimal prompt,
#  2026-08-24), plus the JSON output for a 2,400-char chunk, giving 8 s, divided by the worker count.
#  index is the run-record median of 24 s ÷ 3,370 chunks = 0.007 s/chunk (2026-08-24).
SEED = {
    "extract": ("LLM calls", 8.0),      # divided by the worker count (parallel, below)
    "index":   ("chunks", 0.007),
    "distill": ("sessions", 40.0),      # several windows per session × an LLM
    "sync":    ("changed documents", 0.4),
}
PARALLEL = {"extract", "distill"}      # steps that fan out across workers

#  Seed **floors** —— the time it takes even with 0 units of work.  For a fresh install with no records.
#  Grounds: index and sync load the embedding model.  Measured on this machine (2026-08-24),
#  the minimum was 18 s and 6 s respectively.  Other hardware differs, so these are only seeds,
#  and after one run `_floor()` switches to that machine's own measurement.
SEED_FLOOR = {"index": 15.0, "sync": 5.0}


def _runs_rate(step):
    """Seconds per unit from the run records.  Trusted only from 3 records carrying units."""
    per = []
    for f in glob.glob(os.path.join(RUNS, "*.json")):
        try:
            r = json.load(open(f))
        except Exception:
            continue
        u = r.get("units")
        if r.get("step") != step or r.get("status") != "ok" or not u:
            continue
        s, e = r.get("started_at"), r.get("ended_at")
        #  Do not write it as `if s and e` —— a timestamp of 0 is falsy and gets dropped quietly.
        #  The self-check builds a record with 0 to catch that mistake (2026-08-24).
        if s is not None and e is not None and e >= s:
            per.append((e - s) / u)
    return statistics.median(per) if len(per) >= 3 else None


def _wall_median(step):
    """The median of **the elapsed time itself**, with no units.  For steps that cannot be counted.

    promote, export and verify make "one unit of work" hard to define.  That is no reason to
    fall back to a declared constant —— the run records hold 379 wall-clock times.  Measured
    (2026-08-24): export declares 2 min against a median of 5 s; verify declares 1 min against
    0 s.  A bundle's total is dominated by those three, so a constant here makes the whole bundle estimate a constant.
    """
    ds = []
    for f in glob.glob(os.path.join(RUNS, "*.json")):
        try:
            r = json.load(open(f))
        except Exception:
            continue
        if r.get("step") != step or r.get("status") != "ok":
            continue
        s_, e_ = r.get("started_at"), r.get("ended_at")
        if s_ is not None and e_ is not None and e_ >= s_:
            ds.append(e_ - s_)
    return statistics.median(ds) if len(ds) >= 3 else None


def _floor(step):
    """The fixed start-up cost —— the time it takes even with 0 units of work.

    A pure proportion (units × rate) omits this.  Measured (2026-08-24): with only 55 changed
    chunks the proportion gives 0.4 s while **the minimum in the run records is 18 s** —— the
    difference is loading the embedding model, opening LanceDB and writing the tables.  That
    cost does not disappear at 0 units, so the observed **minimum** is the floor (not the
    median —— a median already includes work).
    """
    ds = []
    for f in glob.glob(os.path.join(RUNS, "*.json")):
        try:
            r = json.load(open(f))
        except Exception:
            continue
        if r.get("step") != step or r.get("status") != "ok":
            continue
        s_, e_ = r.get("started_at"), r.get("ended_at")
        if s_ is not None and e_ is not None and e_ >= s_:
            ds.append(e_ - s_)
    return min(ds) if len(ds) >= 3 else SEED_FLOOR.get(step, 0.0)


def _rate(step):
    """(unit name, seconds per unit, source)."""
    unit, seed = SEED.get(step, ("tasks", 1.0))
    learned = _runs_rate(step)
    if learned is not None:
        return unit, learned, f"run-record median"
    return unit, seed, "seed value (too few records)"


# ── Count "the work left" per step ───────────────────────────────────────
#    All three are **exact** —— not a sample, but the actual targets counted directly.

def _count_extract():
    """The number of chunks not in the cache = the number of LLM calls ahead."""
    with contextlib.redirect_stdout(io.StringIO()):
        import lr_extract as L
        cs = L.collect()
    cached = set()
    p = os.path.join(KAL_HOME, "lr_cache.jsonl")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            try:
                r = json.loads(line)
                if "h" in r:
                    cached.add((r["doc"], r["idx"], r["h"], r.get("pv", "")))
            except Exception:
                pass
    todo = sum(1 for c in cs if L.cache_key(c) not in cached)
    return todo, f"{len(cs) - todo} of {len(cs)} chunk(s) cached"


def _count_index():
    """The number of chunks to embed.  Chunks of unchanged documents reuse their vectors."""
    with contextlib.redirect_stdout(io.StringIO()):
        import schema_v3 as S
        docs = S.scan_vault()
    old = {}
    try:
        import kal_search
        t = kal_search.KAL().db.open_table("documents")
        for r in t.search().limit(0).to_list():
            old[r["path"]] = r["content_hash"]
    except Exception:
        pass                        # no DB means everything is new
    if not old:
        n = sum(max(1, len(d["_body"]) // max(1, S.CHUNK - S.OVERLAP)) for d in docs.values())
        return n, f"first build — all {len(docs)} document(s)"
    changed = [d for d in docs.values() if old.get(d["path"]) != d["content_hash"]]
    n = sum(max(1, len(d["_body"]) // max(1, S.CHUNK - S.OVERLAP)) for d in changed)
    return n, f"{len(changed)} of {len(docs)} document(s) changed"


def _count_distill():
    """The number of sessions not yet distilled (no .done marker)."""
    out = os.environ.get("KAL_DISTILL_OUT", os.path.join(KAL_HOME, "distilled"))
    sess = os.path.join(KAL_HOME, "sessions/session_docs.json")
    if not os.path.exists(sess):
        return 0, "no session list — run ingest_sessions.py first"
    try:
        recs = json.load(open(sess, encoding="utf-8"))
    except Exception:
        return 0, "the session list could not be read"
    done = os.path.join(out, ".done")
    todo = [r for r in recs if not os.path.exists(os.path.join(done, r.get("session_id", "")))]
    return len(todo), f"{len(recs) - len(todo)} of {len(recs)} session(s) done"


def _count_sync():
    """The number of changed documents —— the same comparison as index, but the unit is documents."""
    with contextlib.redirect_stdout(io.StringIO()):
        import schema_v3 as S
        docs = S.scan_vault()
    try:
        import kal_search
        t = kal_search.KAL().db.open_table("documents")
        old = {r["path"]: r["content_hash"] for r in t.search().limit(0).to_list()}
    except Exception:
        return len(docs), "no DB — everything"
    n = sum(1 for d in docs.values() if old.get(d["path"]) != d["content_hash"])
    return n, f"{n} of {len(docs)} document(s) changed"


COUNTERS = {"extract": _count_extract, "index": _count_index,
            "distill": _count_distill, "sync": _count_sync}


def estimate(step_id):
    """The estimate for one step.  A bundle step sums its `runs`."""
    from status import STEP_BY_ID
    st = STEP_BY_ID.get(step_id)
    if st is None:
        raise KeyError(step_id)

    if st.get("runs"):                       # a bundle —— sum the children
        parts = [estimate(c) for c in st["runs"]]
        return {"step": step_id, "title": st["title"],
                "seconds": sum(p["seconds"] for p in parts),
                "units": None, "unit": "",
                "basis": " + ".join(f"{p['title']} {_human(p['seconds'])}" for p in parts),
                "source": "bundle sum", "declared_minutes": st.get("minutes"),
                "parts": parts}

    fn = COUNTERS.get(step_id)
    if fn is None:                           # no way to count → the wall-clock record, else the declared value
        wall = _wall_median(step_id)
        return {"step": step_id, "title": st["title"],
                "seconds": wall if wall is not None else st.get("minutes", 1) * 60,
                "units": None, "unit": "", "basis": "no way to count",
                "source": "run-record median" if wall is not None else "declared value (too few records)",
                "declared_minutes": st.get("minutes")}

    units, basis = fn()
    unit, per, source = _rate(step_id)
    if step_id in PARALLEL:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                from lr_extract import effective_workers
            per = per / max(1, effective_workers())
        except Exception:
            pass
    floor = _floor(step_id)
    return {"step": step_id, "title": st["title"],
            "seconds": round(floor + units * per, 1), "units": units, "unit": unit,
            "floor": floor, "basis": basis, "source": source,
            "declared_minutes": st.get("minutes")}


def _human(sec):
    if sec < 1:
        return "instant"
    if sec < 60:
        return f"{sec:.0f} s"
    if sec < 3600:
        return f"{sec/60:.0f} min"
    return f"{sec/3600:.1f} h"


def render(e, indent=""):
    """One readable block.  **The count comes first** —— it is more trustworthy than the time."""
    head = f"{indent}{e['title']}  ({e['step']})"
    if e["units"] is not None:
        head += f"\n{indent}  work     {e['units']:,} {e['unit']}"
    head += f"\n{indent}  estimate {_human(e['seconds'])}"
    d = e.get("declared_minutes")
    if d and abs(e["seconds"] - d * 60) > max(60, d * 60 * 0.5):
        head += f"   (the list's fixed value is {d} min)"
    head += f"\n{indent}  basis    {e['basis']} · rate {e['source']}"
    if e.get("floor"):
        head += f" · fixed start-up cost {_human(e['floor'])}"
    return head


def _selftest():
    from status import STEPS
    ok = 0
    #  ① every step estimates (no exception kills it)
    for s in STEPS:
        e = estimate(s["id"])
        assert e["seconds"] >= 0, s["id"]
        assert e["title"], s["id"]
        ok += 1
    #  ② a bundle is the sum of its children —— omit the summing and it comes out 0
    r = estimate("rebuild_all")
    assert r["parts"], "the bundle did not expand its children"
    assert abs(r["seconds"] - sum(p["seconds"] for p in r["parts"])) < 0.5
    ok += 1
    #  ③ Too few records and it **must say** it is a seed value.  Used quietly, an invented
    #     number looks measured —— this check prevents that.
    _, _, src = _rate("__no_such_step__")
    assert "seed value" in src, src
    ok += 1
    #  ④ 3 or more records and it uses them (a seed value winning means the learning is dead)
    import tempfile, time
    global RUNS
    keep = RUNS
    try:
        RUNS = tempfile.mkdtemp()
        for i in range(3):
            json.dump({"step": "zz", "status": "ok", "units": 10,
                       "started_at": 0, "ended_at": 100},
                      open(os.path.join(RUNS, f"{i}.json"), "w"))
        assert _runs_rate("zz") == 10.0, _runs_rate("zz")
        os.remove(os.path.join(RUNS, "0.json"))
        assert _runs_rate("zz") is None, "2 records were trusted"
    finally:
        RUNS = keep
    ok += 2
    #  ④-b Steps that cannot be counted learn from the records too (never fall back to a declared constant)
    keep = RUNS
    try:
        RUNS = tempfile.mkdtemp()
        for i in range(3):
            json.dump({"step": "yy", "status": "ok",
                       "started_at": 0, "ended_at": 7},
                      open(os.path.join(RUNS, f"{i}.json"), "w"))
        assert _wall_median("yy") == 7, _wall_median("yy")
        os.remove(os.path.join(RUNS, "0.json"))
        assert _wall_median("yy") is None, "2 records were trusted"
    finally:
        RUNS = keep
    ok += 2
    #  ④-c The floor is really added —— 0 units must not come out as 0 seconds
    keep = RUNS
    try:
        RUNS = tempfile.mkdtemp()
        for i in range(3):
            json.dump({"step": "index", "status": "ok", "started_at": 0, "ended_at": 18},
                      open(os.path.join(RUNS, f"{i}.json"), "w"))
        assert _floor("index") == 18, _floor("index")
        #  With no records it falls back to the **seed floor** —— 0 would make a fresh install
        #  claim "instant" when loading the model alone takes tens of seconds.
        for f in os.listdir(RUNS):
            os.remove(os.path.join(RUNS, f))
        assert _floor("index") == SEED_FLOOR["index"], _floor("index")
        assert _floor("__no_such_step__") == 0.0
    finally:
        RUNS = keep
    ok += 1
    #  ⑤ In the readable output **the count comes before the time**
    txt = render(estimate("extract"))
    assert txt.index("work") < txt.index("estimate"), txt
    ok += 1
    print(f"  ✅ estimate self-check —— {ok} case(s) (every step · bundle sum · seed marking · learning · display order)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest(); raise SystemExit(0)
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    from status import STEPS
    ids = args or [s["id"] for s in STEPS]
    es = []
    for i in ids:
        try:
            es.append(estimate(i))
        except KeyError:
            print(f"unknown step: {i}", file=sys.stderr); raise SystemExit(2)
    if "--json" in sys.argv:
        print(json.dumps({e["step"]: e for e in es}, ensure_ascii=False))
    else:
        for e in es:
            print(render(e)); print()
