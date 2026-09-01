#!/usr/bin/env python3
"""Decide the vault path **in one place**.

Why —— this value was hardcoded into sixteen files, and every default was
`~/github/HwangTaehyun/super-brain` (the author's path).  A stranger does not have it.
And **no error is raised**: glob returns 0, and a docker bind mount silently creates the
path.  The screen shows "0 documents" and the user assumes indexing failed.
`docs/PLUGIN-AND-DOCKER-PLAN.md:88` had already recorded this as a known item and it went
unfixed.

**It stops rather than guessing.**  The same discipline as `just setup` and `just _vault` (justfile).

Self-check:  python3 src/vault_path.py --selftest
"""
import os
import sys
import tempfile

ENV_NAMES = ("KAL_VAULT", "VAULT_DIR")

#  A temporary vault for self-checks —— one per process (see the vault() comment below)
_SELFTEST_VAULT = None

#  The `.env` `just vault` writes.  It has to be a constant so the self-check can isolate it ——
#  otherwise "does it die when nothing is set" passes by reading the real `.env`.
ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def vault():
    """The folder holding the notes.  When it cannot be decided, **fail loudly**.

    ⚠ Under `--selftest` it returns an empty temporary folder.  Self-checks do not use the
      vault, and dying merely on import would stop everything in CI.
      (That failure has already happened here —— a self-check opened the real DB and halted
      completely on a machine without `~/.kal`.)
    """
    for n in ENV_NAMES:
        v = os.environ.get(n)
        if v:
            return os.path.expanduser(v)

    #  ⚠ **Read where `just vault` wrote.**  Watching only the environment meant `just vault
    #     <path>` printed "the CLI reads the new path from now on" and the very next
    #     `just status` died with "I do not know where the notes are" —— the prescription the
    #     error message gave could not fix that error
    #     (2026-08-25, round 6).  Only `schema_v3` read the config and the other nine did not.
    try:
        import kal_config
        v = kal_config.path_override("vault")
        if v:
            return os.path.expanduser(v)
    except Exception:
        pass                            # an unreadable config still falls through below

    #  `.env` is for docker compose, but `just vault` writes here too.
    #  A host command that cannot see it makes the two look divergent.
    try:
        with open(ENV_FILE, encoding="utf-8") as fh:
            for ln in fh:
                if ln.startswith("VAULT_DIR="):
                    v = ln.split("=", 1)[1].strip()
                    if v:
                        return os.path.expanduser(v)
    except OSError:
        pass

    if "--selftest" in sys.argv:
        #  ⚠ It must be **the same value within one process**.  Handing out a fresh temporary
        #     folder each time makes `lr_extract` and `schema_v3` look at different vaults and
        #     breaks the "extraction scope == indexing scope" assertion (measured 2026-08-25:
        #     it broke at indexed 2 · extracted 1).
        global _SELFTEST_VAULT
        if _SELFTEST_VAULT is None:
            _SELFTEST_VAULT = tempfile.mkdtemp(prefix="kal-selftest-novault-")
            #  ⚠ **Say so when handing it over.**  Silently handing back an empty vault lets a
            #     test that really needs one "pass" while reading nothing, or die for an
            #     unrelated reason.  `kal_mcp` really did die with `{"error":"unreadable"}` and
            #     the cause took a while to find (2026-08-25).
            print("  ⓘ no vault set, running against an empty temporary folder —— pass KAL_VAULT"
                  " if this test needs a vault", file=sys.stderr)
        return _SELFTEST_VAULT
    raise SystemExit(
        "  ❌ I do not know where the notes are —— I will not guess.\n"
        "     Set it in one of two ways:\n"
        "       just vault ~/my-notes         # writes to both .env and ~/.kal/config.json\n"
        "       KAL_VAULT=~/my-notes <cmd>    # this run only")


def check(path):
    """May this path be used as the vault.  → (a list of problems).  An empty list means fine.

    `just setup` and `just _vault` (justfile) **both** call this.  The check used to live in
    `setup` alone, so `VAULT_DIR=$HOME just _vault` returned the home directory as it was ——
    and that value went into `sh`, `mcp-plugin-test` and `codex-config`
    (2026-08-25 deep-review Round 5).
    """
    bad = []
    if not path:
        return ["the path is empty"]
    p = os.path.expanduser(path)
    if not os.path.isdir(p):
        return [f"no such folder: {p}"]
    real = os.path.realpath(p)
    home = os.path.realpath(os.path.expanduser("~"))

    #  ⚠ **Compared by inode, not by string.**  Several spellings point at one folder:
    #       /Users/taehyun                      the original
    #       /users/taehyun                      a case-insensitive filesystem (the APFS default)
    #       /System/Volumes/Data/Users/taehyun  macOS firmlink
    #     All three are dev=16777234 ino=1124871, **the same folder**, while `==` says they
    #     differ.  Measured, the latter two passed the check (2026-08-25, round 6).
    def _same(a, b):
        try:
            return os.path.samefile(a, b)
        except OSError:
            return False

    #  Is it the home directory itself, or **an ancestor containing home**.  Home's ancestors are walked upward.
    anc, seen = home, set()
    while anc not in seen:
        seen.add(anc)
        if _same(real, anc):
            what = "the home directory" if anc == home else f"an ancestor containing home ({anc})"
            bad.append(f"{what} cannot be used as a vault: {real}")
            break
        parent = os.path.dirname(anc)
        if parent == anc:
            break
        anc = parent
    if _same(real, os.sep) and not bad:
        bad.append("the root (/) cannot be used as a vault")
    return bad


def _check_selftest():
    import tempfile
    home = os.path.realpath(os.path.expanduser("~"))
    assert check(home), "home itself was not blocked"
    assert check(home + os.sep), "a trailing slash gets around it"
    assert check(os.path.dirname(home)), "home's **parent** (/Users) was not blocked"
    assert check("/"), "the root was not blocked"
    assert check("/nope/zz/definitely"), "a nonexistent folder was not blocked"
    #  ⚠ **A different spelling of the same folder** must be blocked too (round 6 passed both).
    for alt in (home.replace("/Users/", "/users/"),
                "/System/Volumes/Data" + home):
        if os.path.isdir(alt):
            assert check(alt), f"a different spelling of the same folder passes: {alt}"
    assert check(""), "an empty path was not blocked"
    with tempfile.TemporaryDirectory() as d:
        assert not check(d), f"a healthy folder was blocked: {check(d)}"
        #  A symlink pointing at home is blocked too
        ln = os.path.join(d, "link")
        os.symlink(home, ln)
        assert check(ln), "a symlink pointing at home passes"
    print("  ✅ vault_path.check —— home, parent, root, symlink, missing folder and empty all refused")


def _selftest():
    keep = {n: os.environ.pop(n, None) for n in ENV_NAMES}
    try:
        #  ① the environment wins if set —— `~` is expanded too
        os.environ["KAL_VAULT"] = "~/zzz-nope"
        assert vault() == os.path.expanduser("~/zzz-nope"), vault()
        #  ② the first name wins
        os.environ["VAULT_DIR"] = "/tmp/second"
        assert vault() == os.path.expanduser("~/zzz-nope"), "KAL_VAULT must come first"
        #  ③ without KAL_VAULT, VAULT_DIR
        del os.environ["KAL_VAULT"]
        assert vault() == "/tmp/second", vault()
        #  ④ with neither, and not --selftest, it **dies**
        del os.environ["VAULT_DIR"]
        argv, envf = sys.argv, globals()["ENV_FILE"]
        globals()["ENV_FILE"] = "/nonexistent/.env"     # so the real .env is not read
        os.environ["KAL_CONFIG"] = "/nonexistent/config.json"
        try:
            sys.argv = ["x"]
            try:
                vault()
            except SystemExit as e:
                assert "I will not guess" in str(e), str(e)
            else:
                raise AssertionError("nothing was set and it did not die —— it runs quietly on the author's path")
        finally:
            sys.argv = argv
        #  ③-b **Where `just vault` wrote** is read too —— watching only the environment blocked
        #     onboarding entirely (round 6).  config.json, then .env.
        import json
        import tempfile as _tf
        _d = _tf.mkdtemp(prefix="kal-vp-")
        _cfgkeep = os.environ.get("KAL_CONFIG")
        try:
            os.environ["KAL_CONFIG"] = os.path.join(_d, "config.json")
            #  ⚠ The shape is exactly what `just vault` writes —— a flat {"vault": …}.
            #     Building it wrongly as `{"paths": {...}}` made the test fail, and that was
            #     the fixture being wrong rather than the code.
            json.dump({"vault": "/tmp/from-config"}, open(os.environ["KAL_CONFIG"], "w"))
            import kal_config as _kc
            import importlib
            importlib.reload(_kc)
            assert vault() == "/tmp/from-config", \
                f"config.json's vault goes unread —— `just vault` becomes meaningless: {vault()}"
        finally:
            if _cfgkeep is None:
                os.environ.pop("KAL_CONFIG", None)
            else:
                os.environ["KAL_CONFIG"] = _cfgkeep
            import kal_config as _kc2, importlib as _il
            _il.reload(_kc2)
            import shutil as _sh
            _sh.rmtree(_d, ignore_errors=True)

        #  ④-b In self-check mode it must be **the same value**
        argv = sys.argv
        try:
            sys.argv = ["x", "--selftest"]
            a, b = vault(), vault()
            assert a == b, f"the self-check vault differs between calls: {a} vs {b}"
        finally:
            sys.argv = argv
            globals()["ENV_FILE"] = envf
            os.environ.pop("KAL_CONFIG", None)

        #  ⑤ **The author's path must appear nowhere in the code as a default**
        import glob
        import re
        here = os.path.dirname(os.path.abspath(__file__))
        rx = re.compile(r"super-brain")
        bad = []
        for f in glob.glob(os.path.join(here, "*.py")) + glob.glob(os.path.join(here, "*.sh")):
            if os.path.basename(f) in ("vault_path.py",):
                continue                       # this file quotes it in its explanation
            for i, ln in enumerate(open(f, encoding="utf-8"), 1):
                if rx.search(ln) and not ln.lstrip().startswith("#"):
                    bad.append(f"{os.path.basename(f)}:{i}")
        assert not bad, ("the author's vault path remains in the code (on a non-comment line): "
                         + " · ".join(bad))
        _check_selftest()
        print("  ✅ vault_path self-check —— priority · ~ expansion · dies when unset · "
              "self-check consistency · 0 author paths")
    finally:
        for n, v in keep.items():
            os.environ.pop(n, None)
            if v is not None:
                os.environ[n] = v


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest(); sys.exit(0)
    if "--check" in sys.argv:
        target = sys.argv[sys.argv.index("--check") + 1]
        problems = check(target)
        for m in problems:
            print(f"  ❌ {m}", file=sys.stderr)
        sys.exit(1 if problems else 0)
    print(vault())
