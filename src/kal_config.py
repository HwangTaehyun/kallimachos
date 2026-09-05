#!/usr/bin/env python3
"""Settings in one place —— defaults, `config.json` and the environment merged **in a fixed order**.

    environment  >  ~/.kal/config.json  >  code defaults

Why the environment wins
  Because it is a deployment-level decision (12-factor).  If a screen could override what the
  person who started the container set, there would be no way to trace why the same image
  behaves differently in different environments.

  **In exchange the screen has to say so.**  A key the environment won is locked in the UI and
  marked `source: "env"`.  Otherwise it becomes "an input that changes nothing when you edit
  it", and that is worse than not having it.

Why paths cannot be changed here
  `KAL_VAULT` and `KAL_PATH` are **bind mounts** in the container (`/vault` and `/data/kal`).
  Changing the value inside the container achieves nothing, because that path does not exist ——
  the mount is decided when the container starts.  So paths show as **read-only** and the
  screen explains how to change them (edit `.env` and `docker compose up -d`).

Values that require a re-index
  Chunk size, overlap, tokeniser and embedding model are **already baked into the DB.**  Change
  one and it disagrees with the existing chunks and vectors.  They are marked `reindex: true`,
  and status compares them against what `schema_v3` recorded in `meta` at build time.
"""
import json
import os
import sys

KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
CONFIG_PATH = os.environ.get("KAL_CONFIG", os.path.join(KAL_HOME, "config.json"))

#  key: (default, type, environment variable, needs re-index, min, max, description)
#  min/max of None means no range check (strings and the like).
SPEC = {
    "chunk_chars": {
        "default": 500, "type": "int", "env": "KAL_CHUNK_CHARS", "reindex": True,
        "min": 100, "max": 4000,
        "label": "Chunk size (characters)",
        "help": "How many characters each piece of a document holds.  Small is precise and slow; large is broader in context but blurrier.",
    },
    "chunk_overlap": {
        "default": 50, "type": "int", "env": "KAL_CHUNK_OVERLAP", "reindex": True,
        "min": 0, "max": 1000,
        "label": "Chunk overlap (characters)",
        "help": "How much chunks overlap so sentences on a boundary are not cut.  Must be smaller than the chunk size.",
    },
    "ngram_min": {
        "default": 2, "type": "int", "env": "KAL_NGRAM_MIN", "reindex": True,
        "min": 1, "max": 6,
        "label": "FTS n-gram minimum",
        "help": "Korean cannot be split on spaces, so n-grams are used.",
    },
    "ngram_max": {
        "default": 3, "type": "int", "env": "KAL_NGRAM_MAX", "reindex": True,
        "min": 1, "max": 8,
        "label": "FTS n-gram maximum",
        "help": "A large value makes the index large.  Must be greater than or equal to the minimum.",
    },
    "bm25_k1": {
        "default": 1.2, "type": "float", "env": "KAL_BM25_K1", "reindex": True,
        "min": 0.1, "max": 3.0,
        "label": "BM25 k1",
        "help": "Term-frequency saturation.  Raise it to reward repeated occurrences more.",
    },
    "bm25_b": {
        "default": 0.75, "type": "float", "env": "KAL_BM25_B", "reindex": True,
        "min": 0.0, "max": 1.0,
        "label": "BM25 b",
        "help": "Document-length normalisation.  At 0, length is ignored.",
    },
    "embedding_model": {
        "default": "intfloat/multilingual-e5-small", "type": "str",
        "env": "KAL_EMBEDDING_MODEL", "reindex": True, "min": None, "max": None,
        "label": "Embedding model",
        "help": "Changing this changes the vector dimension, so the whole index must be rebuilt.  A model not baked into the image is downloaded.",
    },
    #  ⚠ This value is **a transmission boundary**.  It is a different kind of setting from the
    #     rest —— a wrong chunk size only makes search worse, while a wrong value here sends
    #     **private notes off the machine through `claude -p`.**  So it belongs on screen.
    #     (deep review 2026-08-23, security lens, debt #5)
    "skip_extra": {
        "default": "", "type": "str", "env": "KAL_SKIP", "reindex": True,
        "min": None, "max": None,
        "label": "Folders excluded from indexing and transmission",
        "help": ("Vault-relative paths.  Several, colon-separated (for example imported/slack-dm:private).  "
                 "A folder listed here is neither indexed nor sent to an LLM.  "
                 ".git, .obsidian, node_modules and the like are excluded automatically already."),
    },
    #  ⚠ **The same kind of transmission boundary** as `skip_extra`, and it was missing from the
    #     registry.  `lr_extract.py` read only `os.environ`, and the host CLI does not read
    #     `.env`, so it **had no effect at all** —— a user believes a folder is excluded while
    #     those notes go out through `claude -p`.  No error is raised.
    #     (deep review 2026-08-25, security lens)
    "no_llm_paths": {
        #  reindex: the gate now lives in documents.no_llm, so a changed path list needs a sync or rebuild
        #  (deep-review 2026-09-05 R4) —— the panel lists it under "needs re-index"; `just sync` is enough.
        "default": "", "type": "str", "env": "KAL_NO_LLM", "reindex": True,
        "min": None, "max": None,
        "label": "Excluded from LLM transmission only (still indexed)",
        "help": ("Vault-relative paths.  Several, colon-separated.  Unlike \"Folders excluded from "
                 "indexing and transmission\" these are still indexed —— they appear in search and "
                 "only stay out of the LLM.  To block a single document, put no_llm: true in its frontmatter."),
    },
    "search_preset": {
        "default": "default", "type": "str", "env": "KAL_SEARCH_PRESET", "reindex": False,
        "min": None, "max": None,
        "label": "Search weight preset",
        "help": "Used at query time only —— the knowledge DB need not be rebuilt.  In tune_alpha.py's measurements, default was best.",
    },
}


def _cast(key, raw):
    t = SPEC[key]["type"]
    if t == "int":
        return int(raw)
    if t == "float":
        return float(raw)
    return str(raw)


def read_file(path=None):
    """Read `config.json`.  Missing or broken gives an empty dict —— the same as having no settings.

    ⚠ **A broken file must not be ignored quietly.**  When a user editing by hand breaks the
    JSON and the screen shows defaults, there is no way to tell why what they wrote is ignored.
    The error is handed back as the second return value.
    """
    p = path or CONFIG_PATH
    if not os.path.exists(p):
        return {}, ""
    try:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        if not isinstance(d, dict):
            return {}, f"{p} is not a JSON object"
        return d, ""
    except Exception as e:
        return {}, f"{p} could not be read: {type(e).__name__}: {e}"


def resolve(path=None, env=None):
    """→ {key: {"value", "source", "default", "reindex", ...}}  · plus a file-error string.

    source is one of three: `env` · `file` · `default`.
    """
    env = os.environ if env is None else env
    filed, err = read_file(path)
    out = {}
    for k, spec in SPEC.items():
        value, source = spec["default"], "default"
        if k in filed:
            try:
                value, source = _cast(k, filed[k]), "file"
            except (TypeError, ValueError):
                err = err or f"the `{k}` value in config.json could not be read: {filed[k]!r}"
        ev = env.get(spec["env"])
        if ev not in (None, ""):
            try:
                value, source = _cast(k, ev), "env"
            except ValueError:
                err = err or f"the value of environment variable {spec['env']} could not be read: {ev!r}"
        out[k] = dict(spec, value=value, source=source)
    return out, err


def values(path=None, env=None):
    """The computed values only.  This is what the pipeline code uses."""
    r, _ = resolve(path, env)
    return {k: v["value"] for k, v in r.items()}


def validate(vals):
    """→ {key: reason}.  Empty means it passes.

    It checks **combinations**, not only ranges.  Measured (a 1,200-character document):
        CHUNK=500 OVERLAP=500  → ValueError: range() arg 3 must not be zero
        CHUNK=500 OVERLAP=600  → 0 chunks —— the document vanishes from search entirely
        CHUNK=100 OVERLAP=99   → 1,200 chunks —— the index explodes
    These are exposed through the UI, so a user can enter them unless they are blocked here.
    """
    bad = {}
    for k, spec in SPEC.items():
        if k not in vals:
            continue
        v = vals[k]
        if spec["min"] is not None and not (spec["min"] <= v <= spec["max"]):
            bad[k] = f"must be between {spec['min']} and {spec['max']} (got {v})"
    if "chunk_chars" in vals and "chunk_overlap" in vals and "chunk_overlap" not in bad:
        c, o = vals["chunk_chars"], vals["chunk_overlap"]
        if o >= c:
            bad["chunk_overlap"] = (
                f"must be smaller than the chunk size ({c}) —— equal kills the index, and "
                f"larger produces 0 chunks so the document vanishes from search")
        elif c - o < c * 0.2:
            bad["chunk_overlap"] = (
                f"the overlap is too large (a stride of {c - o} characters).  Keep it under 80% of "
                f"the chunk size —— the index grows {c / (c - o):.0f}\u00d7")
    if "ngram_min" in vals and "ngram_max" in vals and "ngram_max" not in bad:
        if vals["ngram_max"] < vals["ngram_min"]:
            bad["ngram_max"] = f"must be greater than or equal to the minimum ({vals['ngram_min']})"
    return bad


def save(patch, path=None):
    """Merge **only the keys being overridden** into `config.json` and save.  → the saved dict.

    A key that matches the default is **deleted** —— leaving it in the file pins this deployment
    to the old value when the default later changes.  Only "what was set on purpose" belongs in the file.
    """
    p = path or CONFIG_PATH
    cur, err = read_file(p)
    if err:
        raise ValueError(err)
    merged = dict(cur)
    for k, v in patch.items():
        if k not in SPEC:
            raise ValueError(f"unknown setting key: {k}")
        v = _cast(k, v)
        if v == SPEC[k]["default"]:
            merged.pop(k, None)
        else:
            merged[k] = v
    #  Validation runs on **the merged result**.  Sending one key alone can still break a
    #  combination (sending only overlap=600 disagrees with a chunk size of 500).
    eff = {k: merged.get(k, SPEC[k]["default"]) for k in SPEC}
    bad = validate(eff)
    if bad:
        raise ValueError("; ".join(f"{k}: {r}" for k, r in bad.items()))
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, p)          # atomic —— a crash mid-write leaves no half file
    return merged


def drift(meta):
    """Compare **what settings the DB was built with** against the current ones.  → [(key, now, DB)].

    It uses what `schema_v3` wrote into `meta` at the end of the build —— nothing else to record.
    Anything caught here means that setting lives **only on screen, not in the DB**, until a re-index.
    """
    v = values()
    m = {r["key"]: r["value"] for r in meta} if isinstance(meta, list) else dict(meta or {})
    pairs = [
        ("chunk_chars", str(v["chunk_chars"]), m.get("chunk_chars")),
        ("chunk_overlap", str(v["chunk_overlap"]), m.get("chunk_overlap")),
        ("bm25_k1", str(v["bm25_k1"]), m.get("bm25_k1")),
        ("bm25_b", str(v["bm25_b"]), m.get("bm25_b")),
        ("embedding_model", v["embedding_model"], m.get("embedding_model")),
        ("fts_tokenizer", f"ngram({v['ngram_min']},{v['ngram_max']})", m.get("fts_tokenizer")),
    ]
    return [(k, now, was) for k, now, was in pairs if was is not None and str(was) != now]


#  Paths are not in SPEC —— they are not tuning values, and they must not sit beside numbers on
#  the web's "Chunking and search" screen.  They live as separate keys in the same file (config.json).
PATH_KEYS = ("vault", "home", "db")


def path_override(key, path=None):
    """The path written in config.json.  None when absent.

    Why it is needed —— `export KAL_VAULT=…` lives **only inside that shell**.  A new terminal
    loses it, and a process started elsewhere, such as the MCP server, never sees it at all.
    So "set it once and it stays" requires a file.
    The priority is the same as every other setting: environment > config.json > default.
    """
    if key not in PATH_KEYS:
        raise KeyError(key)
    filed, _ = read_file(path)
    v = (filed or {}).get(key)
    return str(v) if v else None


def save_path(key, value, path=None):
    """Write the path into config.json.  An empty value deletes it (= back to the default or the environment).

    ⚠ **The container does not follow this.**  `/vault` is a bind mount and mounts are decided
      when the container starts —— measured (2026-08-24): inside the container the host's
      `/tmp/other-notes` is `No such file or directory`.  That is not a wall software can climb.
      Moving the container too means editing VAULT_DIR in `.env` and restarting ——
      `just vault` does both together.
    """
    if key not in PATH_KEYS:
        raise KeyError(key)
    p = path or CONFIG_PATH
    cur, err = read_file(p)
    if err:
        raise ValueError(err)
    merged = dict(cur)
    if value:
        merged[key] = os.path.abspath(os.path.expanduser(str(value)))
    else:
        merged.pop(key, None)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return merged


def paths():
    """Paths are **read-only**.  The reason comes with them —— the screen shows it verbatim.

    In a container `/vault` is a bind mount, so changing the value achieves nothing: that path
    does not exist.  The mount is decided when the container starts.  So pretending it is
    editable here is wrong —— it becomes an input that does nothing when pressed.
    """
    import schema_v3 as _s
    in_container = os.path.exists("/.dockerenv")
    how = ("Edit VAULT_DIR and KAL_DIR in `.env` and restart with `docker compose up -d`"
           if in_container else
           "Set them in your shell with `export KAL_VAULT=…` and `export KAL_HOME=…` "
           "(`.env` is read by docker compose only)")
    out = []
    for key, label, val, envname, dflt in (
        #  ⚠ There is **no default.**  It used to be the author's path, which a stranger does
        #     not have, so it became "0 documents" with no error (2026-08-25).
        ("vault", "The notes root to index", _s.VAULT, "KAL_VAULT",
         "(no default —— you must set this)"),
        ("db", "Where LanceDB lives", _s.DB, "KAL_PATH", os.path.join(KAL_HOME, "db")),
        ("home", "The root for the DB, caches and artifacts", KAL_HOME, "KAL_HOME", os.path.expanduser("~/.kal")),
    ):
        out.append({
            "key": key, "label": label, "value": val,
            "source": ("env" if os.environ.get(envname)
                       else "file" if path_override(key) else "default"),
            "env": envname, "default": dflt,
            "exists": os.path.isdir(val),
            "editable": False, "how": how,
        })
    return out


def as_json():
    r, err = resolve()
    meta = []
    try:
        import lancedb
        import schema_v3 as _s
        db = lancedb.connect(_s.DB)
        if "meta" in db.list_tables().tables:
            meta = db.open_table("meta").to_arrow().to_pylist()
    except Exception:
        #  An unreadable DB is not a failure of the settings screen —— it may simply be
        #  un-indexed.  Only the drift goes unshown; the settings are still displayed.
        meta = []
    return {
        "config_path": CONFIG_PATH,
        "in_container": os.path.exists("/.dockerenv"),
        "paths": paths(),
        "settings": [
            {"key": k, "value": v["value"], "source": v["source"], "default": v["default"],
             "type": v["type"], "env": v["env"], "reindex": v["reindex"],
             "min": v["min"], "max": v["max"], "label": v["label"], "help": v["help"]}
            for k, v in r.items()
        ],
        "drift": [{"key": k, "now": now, "db": was} for k, now, was in drift(meta)],
        "error": err,
    }


def _selftest():
    import tempfile
    #  help and label render as **plain text** on screen.  Markdown there leaves the asterisks
    #  visible (measured: "a **full re-index** is required").  Blocked here.
    for _k, _sp in SPEC.items():
        for _f in ("label", "help"):
            assert "**" not in _sp[_f], (
                f"SPEC[{_k}][{_f}] holds markdown emphasis —— the screen renders plain text, "
                f"so the asterisks show through: {_sp[_f]!r}")
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "config.json")

    # ── Priority ──
    r, err = resolve(p, env={})
    assert not err and r["chunk_chars"]["value"] == 500 and r["chunk_chars"]["source"] == "default"
    save({"chunk_chars": 800}, p)
    r, _ = resolve(p, env={})
    assert r["chunk_chars"]["value"] == 800 and r["chunk_chars"]["source"] == "file", r["chunk_chars"]
    r, _ = resolve(p, env={"KAL_CHUNK_CHARS": "1200"})
    assert r["chunk_chars"]["value"] == 1200 and r["chunk_chars"]["source"] == "env", \
        "the environment does not beat the file —— a deployment is overridden from a screen"
    #  An empty environment variable means "not set".  A `.env` holding only `KAL_CHUNK_CHARS=` is common.
    r, _ = resolve(p, env={"KAL_CHUNK_CHARS": ""})
    assert r["chunk_chars"]["source"] == "file", "an empty environment variable overrode the file"

    # ── A key matching the default is removed from the file ──
    save({"chunk_chars": 500}, p)
    assert "chunk_chars" not in read_file(p)[0], \
        "leaving a default in the file pins this deployment to the old value when the default changes"

    # ── Combination checks —— the three measured dangers ──
    for over, why in ((500, "equal kills the index"), (600, "larger gives 0 chunks")):
        try:
            save({"chunk_chars": 500, "chunk_overlap": over}, p)
            raise AssertionError(f"an overlap of {over} passed —— {why}")
        except ValueError as e:
            assert "chunk size" in str(e), e
    try:
        save({"chunk_chars": 100, "chunk_overlap": 99}, p)
        raise AssertionError("an overlap of 99/100 passed —— the index grows 100×")
    except ValueError as e:
        assert "the overlap is too large" in str(e), e
    #  Sending one key alone must still validate **the merged result**
    save({"chunk_chars": 500}, p)
    try:
        save({"chunk_overlap": 600}, p)
        raise AssertionError("sending only the overlap skipped the combination check")
    except ValueError:
        pass
    try:
        save({"ngram_min": 4, "ngram_max": 2}, p)
        raise AssertionError("an n-gram minimum > maximum passed")
    except ValueError as e:
        assert "greater than or equal" in str(e), e
    try:
        save({"chunk_chars": 10}, p)
        raise AssertionError("an out-of-range value passed")
    except ValueError as e:
        assert "must be between" in str(e), e
    try:
        save({"nope": 1}, p)
        raise AssertionError("an unknown key passed")
    except ValueError as e:
        assert "unknown setting key" in str(e), e

    # ── A broken file **is not passed over quietly** ──
    open(p, "w", encoding="utf-8").write("{ not json")
    _, err = resolve(p, env={})
    assert err and "could not be read" in err, "a broken config.json is ignored quietly"
    open(p, "w", encoding="utf-8").write('["an array"]')
    _, err = resolve(p, env={})
    assert "is not a JSON object" in err, err

    # ── Drift against the DB ──
    os.remove(p)
    meta = [{"key": "chunk_chars", "value": "500"}, {"key": "chunk_overlap", "value": "50"},
            {"key": "bm25_k1", "value": "1.2"}, {"key": "bm25_b", "value": "0.75"},
            {"key": "embedding_model", "value": "intfloat/multilingual-e5-small"},
            {"key": "fts_tokenizer", "value": "ngram(2,3)"}]
    assert not drift(meta), drift(meta)
    meta2 = [m if m["key"] != "chunk_chars" else {"key": "chunk_chars", "value": "800"} for m in meta]
    d = drift(meta2)
    assert d and d[0][0] == "chunk_chars" and d[0][1] == "500" and d[0][2] == "800", d
    #  A key absent from meta is not drift (an older DB never recorded it)
    assert not drift([{"key": "chunk_chars", "value": "500"}]), "an older DB is reported as drift"

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print("  ✅ kal_config self-check — 4 priority cases · 6 combination checks · 2 broken files · 3 drift cases")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    if "--selftest" in sys.argv:
        _selftest()
    elif "--json" in sys.argv:
        json.dump(as_json(), sys.stdout, ensure_ascii=False)
    elif "--save" in sys.argv:
        #  A patch arrives on stdin.  Failure goes through the exit code —— the caller (Go) passes the reason on.
        try:
            save(json.load(sys.stdin))
        except Exception as e:
            sys.stderr.write(str(e))
            raise SystemExit(2)
        json.dump(as_json(), sys.stdout, ensure_ascii=False)
    else:
        r, err = resolve()
        if err:
            print(f"  ⚠ {err}")
        print(f"  {CONFIG_PATH}")
        for k, v in r.items():
            mark = {"env": "env ", "file": "file", "default": "dflt"}[v["source"]]
            print(f"    [{mark}] {k:<18} {v['value']}")
