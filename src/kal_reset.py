#!/usr/bin/env python3
"""`just reset` —— when things get tangled, choose **how far back** to go and go there.

Why it is needed
  Doing "it looks odd, I will just rebuild it" used to require knowing what to delete.  And
  there are four kinds of thing to delete, **whose costs differ by 1,000×**:

      old LanceDB versions   reclaims disk only.  The index is untouched      ~1s
      the index              the tables are rebuilt.  The extraction cache survives, so 0 LLM calls  ~20s
      the extraction cache   every LLM call is made again                     905 of them today
      the settings           config.json and .env back to their defaults      instant

  Not knowing, people mostly pick the strongest —— `rm -rf ~/.kal`, and then 905 LLM calls
  again.  What was actually needed is usually the second line.

What it does **not** do
  · It never touches the vault's notes.  They are the read-only original.
  · It never touches an older data directory (a previous name such as `~/.kdb`).
  · Before undoing anything it **counts what will disappear and shows it**, then asks.
    Called non-interactively without `--yes` it **refuses** —— better to stop than to let a
    script silently destroy the index.

Usage
  python src/kal_reset.py                 what can be done (does nothing)
  python src/kal_reset.py vacuum          old versions only
  python src/kal_reset.py index           rebuild the index (the extraction cache survives)
  python src/kal_reset.py extract         the extraction cache as well
  python src/kal_reset.py all             the settings back to defaults too
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

#  The levels are **cumulative**: choosing extract also does what index does.
#  The order is the destructive power.
LEVELS = ("vacuum", "index", "extract", "all")


def _home():
    import schema_v3
    return schema_v3.KAL_HOME


def _db_path():
    import schema_v3
    return schema_v3.DB


def survey():
    """What exists, and what each level removes.  Returned **as counts**."""
    home, db = _home(), _db_path()
    #  ⚠ `repo` is **passed in here.**  While apply() read the module constant REPO directly,
    #    the self-check handed it a temporary folder and it still **deleted the real
    #    repository's `.env`** (measured 2026-08-24 —— a file holding a token vanished).
    #    Letting it see only the path a test passed makes that accident structurally impossible.
    out = {"home": home, "db": db, "repo": REPO, "tables": {}, "old_versions": 0,
           "cache_lines": 0, "cache_bytes": 0, "config": {}, "env": False}
    #  ⚠ `list_tables()` is **a response object, not a list of names** (LanceDB 0.37).
    #    Iterated directly it yields tuples like `('tables', [...])`, and handing one to
    #    `open_table` raises.  A broad `except` used to swallow that exception, and the screen
    #    quietly said "0 tables" —— which a user reads as "there is nothing to delete".
    #    Measured 2026-08-24: there were really 9 tables and 24,000 rows.
    try:
        import lancedb
        conn = lancedb.connect(db)
        lt = conn.list_tables()
        names = list(getattr(lt, "tables", None) or lt)
        for t in sorted(names):
            out["tables"][t] = conn.open_table(t).count_rows()
    except Exception as e:
        out["tables_error"] = str(e)          # not swallowed —— shown below
    #  Old versions —— the manifest count inside `.lance/_versions` minus the current one
    try:
        for d in os.listdir(db):
            v = os.path.join(db, d, "_versions")
            if os.path.isdir(v):
                out["old_versions"] += max(0, len(os.listdir(v)) - 1)
    except Exception:
        pass
    c = os.path.join(home, "lr_cache.jsonl")
    if os.path.exists(c):
        out["cache_bytes"] = os.path.getsize(c)
        with open(c, encoding="utf-8", errors="ignore") as fh:
            out["cache_lines"] = sum(1 for _ in fh)
    cfg = os.path.join(home, "config.json")
    if os.path.exists(cfg):
        try:
            out["config"] = json.load(open(cfg))
        except Exception:
            out["config"] = {"(unreadable)": ""}
    out["env"] = os.path.exists(os.path.join(REPO, ".env"))
    return out


def describe(level, s):
    """What this level removes, in plain words.  **It states counts** —— "delete the cache" is
    less decidable than "delete 902 LLM calls' worth"."""
    rows = []
    if level in ("vacuum", "index", "extract", "all"):
        rows.append(f"{s['old_versions']} old LanceDB version(s) — reclaims disk only, the index is untouched")
    if level in ("index", "extract", "all"):
        n = s["tables"]
        if s.get("tables_error"):
            #  If it could not be counted, **say so**.  Showing "0" reads as "nothing to delete".
            rows.append(f"the knowledge DB — the count could not be read ({s['tables_error'][:50]}). "
                        f"{s['db']} is deleted wholesale anyway")
        else:
            tot = sum(n.values())
            rows.append(f"{len(n)} knowledge DB table(s) · {tot:,} rows — has to be rebuilt "
                        f"({' · '.join(f'{k} {v:,}' for k, v in list(n.items())[:4])}…)")
    if level in ("extract", "all"):
        mb = s["cache_bytes"] / 1e6
        rows.append(f"{s['cache_lines']:,} extraction cache line(s) ({mb:.0f}MB) — "
                    f"**every LLM call is made again**")
    if level == "all":
        keys = ", ".join(s["config"]) or "(empty)"
        rows.append(f"{len(s['config'])} setting(s) in config.json [{keys}]"
                    + (" · .env" if s["env"] else ""))
    return rows


def apply(level, s):
    """Actually delete.  Returns what was deleted as a list."""
    done = []
    home, db = s["home"], s["db"]
    if level in LEVELS and os.path.isdir(db):
        #  ⚠ **Always through a subprocess.**  Doing `import vacuum; vacuum.main()` let its
        #    `SystemExit` propagate and killed the caller (the self-check) entirely ——
        #    exit=2 with no failure message, so the cause was invisible.
        import subprocess
        r = subprocess.run([sys.executable, os.path.join(HERE, "vacuum.py")],
                           capture_output=True, text=True)
        done.append("old versions cleaned" + ("" if r.returncode == 0 else " (failed — skipped)"))
    if level in ("index", "extract", "all"):
        #  ⚠ The `db` directory goes wholesale.  Dropping only the tables leaves `.lance`
        #    remnants, and a later build once picked up the old schema through them.
        if os.path.isdir(db):
            shutil.rmtree(db)
            done.append(f"knowledge DB deleted ({db})")
    if level in ("extract", "all"):
        for f in ("lr_cache.jsonl", "lr_summary_cache.jsonl", "lr_kg.json",
                  "comm_label_cache.jsonl"):
            p = os.path.join(home, f)
            if os.path.exists(p):
                os.remove(p)
                done.append(f"{f} deleted")
    if level == "all":
        cfg = os.path.join(home, "config.json")
        if os.path.exists(cfg):
            #  It is **emptied**, not deleted —— with the file gone, "settings unreadable" and
            #  "settings empty" become indistinguishable.
            open(cfg, "w").write("{}\n")
            done.append("config.json emptied")
        env = os.path.join(s.get("repo", REPO), ".env")
        if os.path.exists(env):
            bak = env + ".bak"
            shutil.copy2(env, bak)
            os.remove(env)
            done.append(f".env removed (a copy: {os.path.basename(bak)}) — `just setup` recreates it")
    return done


def main(argv):
    args = [a for a in argv if not a.startswith("-")]
    yes = "--yes" in argv or "-y" in argv
    s = survey()

    if not args:
        print(f"\n  knowledge DB at  {s['db']}")
        print(f"  settings         {os.path.join(s['home'], 'config.json')}\n")
        print("  Choose how far back to go —— further down is more expensive.\n")
        for lv in LEVELS:
            print(f"    just reset {lv}")
            for r in describe(lv, s):
                print(f"        · {r}")
            print()
        print("  The vault's notes are untouched in every case.")
        return 0

    level = args[0]
    if level not in LEVELS:
        print(f"  unknown level: {level}  (available: {' · '.join(LEVELS)})", file=sys.stderr)
        return 2

    print(f"\n  What `reset {level}` removes:\n")
    for r in describe(level, s):
        print(f"    · {r}")
    print()

    if not yes:
        #  Non-interactive, it **refuses.**  Better to stop than to let a script silently
        #  destroy the index —— the same mistake was already made once in run.py.
        if not sys.stdin.isatty():
            print(f"  Confirmation is required.  From a script:  just reset {level} -- --yes",
                  file=sys.stderr)
            return 2
        if input(f"  Really undo this?  It cannot be undone [y/N] ").strip().lower() \
                not in ("y", "yes"):
            print("  Cancelled.")
            return 130

    for d in apply(level, s):
        print(f"  ✓ {d}")
    #  **What to do next differs per level.**  vacuum does not delete the index, so suggesting
    #  "rebuild the index" costs the user 20 seconds they did not need ——
    #  and worse, it reads as "did I destroy my index".
    print("\n  Next:")
    if level == "vacuum":
        print("    (nothing — the index is untouched.  Only disk was reclaimed)")
        return 0
    print("    just run index      rebuild the index")
    if level in ("extract", "all"):
        print("    just run extract    re-extract the knowledge graph (LLM)")
    if level == "all":
        print("    just setup          recreate .env")
        print("    just init           start from choosing folders (TUI)")
    return 0


def _selftest():
    import tempfile
    ok = 0

    #  ① The levels are **cumulative** —— a lower level must contain the ones above.
    #     Otherwise you get half-states like "extract was deleted and the tables remain".
    fake = {"home": "/x", "db": "/x/db", "tables": {"a": 1}, "old_versions": 2,
            "cache_lines": 5, "cache_bytes": 10, "config": {"k": 1}, "env": True}
    n = [len(describe(lv, fake)) for lv in LEVELS]
    assert n == sorted(n) and n[0] < n[-1], n
    assert n == [1, 2, 3, 4], n
    ok += 2

    #  ② It states counts —— "delete the cache" alone is not decidable
    txt = " ".join(describe("extract", fake))
    assert "5" in txt and "LLM" in txt, txt
    ok += 1

    #  ③ An unknown level is refused (a typo must not become `all`)
    assert main(["indx"]) == 2
    ok += 1

    #  ④ With no argument it **does nothing** —— it only shows the list
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        assert main([]) == 0
    ok += 1

    #  ④-b After vacuum it **must not suggest** "rebuild the index" —— nothing was deleted.
    #      Suggesting it costs the user 20 unnecessary seconds, or reads as "did I destroy it".
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main(["vacuum", "--yes"])
    txt = buf.getvalue()
    assert "the index is untouched" in txt, txt[-200:]
    assert "just run index" not in txt.split("Next:")[-1], txt[-200:]
    ok += 2

    #  ⑤ Does the deletion really run —— a fake home is built and each level checked
    for level, gone, kept in (
            ("index",   ["db"],                       ["lr_cache.jsonl", "config.json"]),
            ("extract", ["db", "lr_cache.jsonl"],     ["config.json"]),
    ):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "db", "t.lance", "_versions"))
        open(os.path.join(d, "db", "t.lance", "_versions", "1.manifest"), "w").close()
        open(os.path.join(d, "lr_cache.jsonl"), "w").write("{}\n")
        open(os.path.join(d, "config.json"), "w").write('{"k":1}\n')
        s = {"home": d, "db": os.path.join(d, "db"), "tables": {}, "old_versions": 0,
             "cache_lines": 1, "cache_bytes": 3, "config": {}, "env": False}
        apply(level, s)
        for g in gone:
            assert not os.path.exists(os.path.join(d, g)), f"{level}: {g} survived"
        for k in kept:
            assert os.path.exists(os.path.join(d, k)), f"{level}: {k} was deleted"
        ok += len(gone) + len(kept)

    #  ⑤-b `apply` **never touches anything outside the path it was given.**
    #      Without this, the self-check deleted the real repository's `.env` (token included).
    d = tempfile.mkdtemp()
    open(os.path.join(d, ".env"), "w").write("KAL_RELAY_TOKEN=x\n")
    real_env = os.path.join(REPO, ".env")
    real_before = os.path.exists(real_env)
    s2 = {"home": d, "db": os.path.join(d, "db"), "repo": d, "tables": {},
          "old_versions": 0, "cache_lines": 0, "cache_bytes": 0,
          "config": {}, "env": True}
    apply("all", s2)
    assert not os.path.exists(os.path.join(d, ".env")), "the .env that was passed in was not deleted"
    assert os.path.exists(os.path.join(d, ".env.bak")), "no copy was left"
    assert os.path.exists(real_env) == real_before, \
        "it touched the real repository's .env —— outside the path it was given"
    ok += 3

    #  ⑥ `all` **empties** config (it does not delete it) —— a missing file and empty settings
    #     have to stay distinguishable
    d = tempfile.mkdtemp()
    open(os.path.join(d, "config.json"), "w").write('{"k":1}\n')
    s = {"home": d, "db": os.path.join(d, "db"), "repo": d, "tables": {},
         "old_versions": 0, "cache_lines": 0, "cache_bytes": 0,
         "config": {}, "env": False}
    apply("all", s)
    p = os.path.join(d, "config.json")
    assert os.path.exists(p), "config.json was deleted —— it must be emptied"
    assert json.load(open(p)) == {}, open(p).read()
    ok += 2

    print(f"  ✅ kal_reset self-check —— {ok} case(s) "
          f"(cumulative levels · counts shown · typo refused · no-arg no-op · real deletion · config emptied)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        raise SystemExit(0)
    raise SystemExit(main(sys.argv[1:]))
