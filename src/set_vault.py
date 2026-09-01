#!/usr/bin/env python3
"""Set the vault **in one go** —— so the CLI and the web look at the same place.

Why one place is not enough
  The two consumers read different things.

    host CLI ·  MCP       →  environment > ~/.kal/config.json > default
    container (web API)   →  whatever compose **bind-mounted** as `/vault`

  So `export KAL_VAULT=…` lives only in that shell, and writing config.json does not bring the
  container along.  The mount is decided when the container starts ——
  measured (2026-08-24): inside the container the host's `/tmp/other-notes` is
  `No such file or directory`.  **That is not a wall software can climb.**

  So both places have to be written.  Doing it twice by hand always misses one, and missing
  one raises no error —— the screen and the DB simply point at different vaults.
  (That divergence is caught by "the vault this DB was built from" on the «Paths» screen.)

Usage
  python src/set_vault.py /Users/me/notes
  python src/set_vault.py --show
  python src/set_vault.py --clear        # back to the default
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.path.join(REPO, ".env")


def env_set(key, value, path=ENV):
    """Replace one line in `.env`.  Append it when absent.  Every other line is **left alone**.

    Rewriting the whole file would destroy comments and things like KAL_RELAY_TOKEN.
    """
    lines, hit = [], False
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            hit = True
            break
    if not hit:
        lines.append(f"{key}={value}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return hit


def check_data_dir():
    """Does `KAL_DIR` in `.env` point at **where the knowledge DB actually is**.

    Why it is needed —— a docker bind mount **silently creates** a missing path.  So when
    `.env` points at an old path, an empty folder appears with no error and the web UI shows
    "0 documents".  The user assumes the index is gone.  Measured (2026-08-25): after moving
    the data, only `.env` went unfixed and exactly that happened.
    → returns (a list of problems).  An empty list means fine.
    """
    bad = []
    kd = None
    if os.path.exists(ENV):
        for ln in open(ENV, encoding="utf-8"):
            if ln.startswith("KAL_DIR="):
                kd = os.path.expanduser(ln.split("=", 1)[1].strip())
    if not kd:
        return bad                      # unset means compose's default is used
    if not os.path.isdir(kd):
        bad.append(f"KAL_DIR in .env points at a folder that does not exist: {kd}")
    elif not os.path.isdir(os.path.join(kd, "db")):
        bad.append(f"KAL_DIR in .env has no db/: {kd}  "
                   f"(is it pointing at an old path?)")
    return bad


def show():
    import kal_config
    envv = os.environ.get("KAL_VAULT")
    filed = kal_config.path_override("vault")
    envfile = None
    if os.path.exists(ENV):
        for ln in open(ENV, encoding="utf-8"):
            if ln.startswith("VAULT_DIR="):
                envfile = ln.split("=", 1)[1].strip()
    print(f"  shell env    KAL_VAULT   {envv or '(unset)'}")
    print(f"  file         config.json {filed or '(unset)'}")
    print(f"  container    .env VAULT_DIR {envfile or '(unset)'}")
    import schema_v3
    print(f"\n  → where the CLI reads right now: {schema_v3.VAULT}")
    for msg in check_data_dir():
        print(f"  ⚠ {msg}")
    if envfile and filed and os.path.abspath(envfile) != os.path.abspath(filed):
        print("  ⚠ the CLI and the container look at **different places**.")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    import kal_config
    if "--show" in sys.argv or (not args and "--clear" not in sys.argv):
        show(); return 0
    target = "" if "--clear" in sys.argv else os.path.abspath(os.path.expanduser(args[0]))
    if target and not os.path.isdir(target):
        print(f"  ❌ no such folder: {target}", file=sys.stderr)
        return 2
    kal_config.save_path("vault", target)
    #  ⚠ Clearing does not fall back to the author's path.  Cleared means **nothing is set**,
    #     and `vault_path.vault()` reports that state by failing loudly.
    if target:
        #  ⚠ The path is **validated** before being written.  It used to check `isdir` alone, so
        #     `$HOME` passed, and then docker mounts the whole home as `/vault` (2026-08-25, round 6).
        import vault_path
        problems = vault_path.check(target)
        if problems:
            for m in problems:
                print(f"  ❌ {m}")
            return 2
        env_set("VAULT_DIR", target)
        #  ⚠ `VAULT_NAME` is written **as well**.  Without it, compose's
        #     `${VAULT_NAME:?…}` says "just vault <path> writes it for you" while that command
        #     does not —— the prescription the error gives cannot fix the error
        #     (round 6).  It is used in the obsidian://open deep link.
        env_set("VAULT_NAME", os.path.basename(os.path.normpath(target)))
    else:
        env_set("VAULT_DIR", "")
        env_set("VAULT_NAME", "")
    print(f"  ✓ config.json  vault      = {target or '(cleared — the default)'}")
    print(f"  ✓ .env         VAULT_DIR  = {target or '(the default)'}")
    if os.environ.get("KAL_VAULT"):
        print(f"\n  ⚠ KAL_VAULT={os.environ['KAL_VAULT']} remains in your shell —— "
              f"the environment wins.\n    Run `unset KAL_VAULT`.")
    print("\n  The CLI reads the new path from now on.")
    print("  The web (container) follows only once **the mount is retaken**:  docker compose up -d")
    print("  Until then the «Paths» screen's 'the vault this DB was built from' shows the divergence.")
    return 0


def _selftest():
    import tempfile
    ok = 0
    #  ① it does not disturb the other lines in .env —— a token must not be destroyed
    d = tempfile.mkdtemp(); f = os.path.join(d, ".env")
    open(f, "w").write("# comment\nKAL_RELAY_TOKEN=secret\nVAULT_DIR=/old\nUID=501\n")  # a temporary .env fixture —— the assertion below checks this value survives (oh-my-airs:allow)
    env_set("VAULT_DIR", "/new", f)
    body = open(f).read()
    assert "KAL_RELAY_TOKEN=secret" in body, body
    assert "# comment" in body and "UID=501" in body, body
    assert "VAULT_DIR=/new" in body and "/old" not in body, body
    ok += 4
    #  ② when absent it is appended (not overwritten)
    open(f, "w").write("UID=501\n")
    assert env_set("VAULT_DIR", "/x", f) is False
    assert "UID=501" in open(f).read() and "VAULT_DIR=/x" in open(f).read()
    ok += 2
    #  ③ saving and clearing a path round-trips
    import kal_config, json
    cfg = os.path.join(d, "config.json")
    kal_config.save_path("vault", "~/", cfg)
    assert json.load(open(cfg))["vault"] == os.path.expanduser("~"), "the tilde was not expanded"
    kal_config.save_path("vault", "", cfg)
    assert "vault" not in json.load(open(cfg)), "it was not cleared"
    ok += 2
    #  ④ a key outside SPEC is refused —— a typo must not put an arbitrary key in the file
    try:
        kal_config.save_path("vaultt", "/x", cfg); assert False, "a typo'd key was accepted"
    except KeyError:
        ok += 1
    #  ⑦ a KAL_DIR in `.env` pointing at an empty place is caught —— docker creates it silently
    d2 = tempfile.mkdtemp(); e2 = os.path.join(d2, ".env")
    keep_env = globals()["ENV"]
    try:
        globals()["ENV"] = e2
        open(e2, "w").write(f"KAL_DIR={d2}/nowhere\n")
        assert check_data_dir(), "a nonexistent path was not caught"
        os.makedirs(f"{d2}/empty")
        open(e2, "w").write(f"KAL_DIR={d2}/empty\n")
        assert check_data_dir(), "a folder with no db/ was not caught"
        os.makedirs(f"{d2}/empty/db")
        assert not check_data_dir(), "a healthy one was caught"
    finally:
        globals()["ENV"] = keep_env
    ok += 3

    print(f"  ✅ set_vault self-check —— {ok} case(s) (.env preserved · appended · round trip · typo refused)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest(); raise SystemExit(0)
    raise SystemExit(main())
