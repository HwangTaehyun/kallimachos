#!/usr/bin/env python3
"""`just migrate` —— coming up from the old name (`kdb`).

What changed
    data          ~/.kdb        →  ~/.kal
    environment   KDB_*         →  KAL_*        (no compatibility)
    image         …/kdb:0.1.0   →  …/kal:0.1.0

Why this has to exist
  Both break **quietly**.  With the old data still in place and the code looking at the new
  path, it decides "first build" and regenerates everything —— a measured **905 LLM calls**.
  No error is raised, so the user assumes that is normal.

What it does not do
  · It **does not delete** the old directory.  This is a copy, not a move —— it has to be undoable.
  · It does not edit shell rc files.  Touching someone's dotfiles silently is not its place.
    Instead it **prints** what needs changing.
  · It does not overwrite data already at the new location.

Usage
  python src/kal_migrate.py            show what it would do (the default)
  python src/kal_migrate.py --apply    actually copy
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
OLD = os.path.expanduser("~/.kdb")
NEW = os.path.expanduser("~/.kal")
#  venv is not moved —— uv now builds `.venv` inside the repository.  A 1.4GB copy at the new
#  location would go unused by everyone.
SKIP = {"venv", ".write.lock"}


def survey():
    """What exists, and what collides."""
    out = {"old": OLD, "new": NEW, "old_exists": os.path.isdir(OLD),
           "new_exists": os.path.isdir(NEW), "items": [], "conflicts": [],
           "env": sorted(k for k in os.environ if k.startswith("KDB_"))}
    if not out["old_exists"]:
        return out
    for name in sorted(os.listdir(OLD)):
        if name in SKIP:
            continue
        src = os.path.join(OLD, name)
        size = 0
        try:
            if os.path.isdir(src):
                size = sum(os.path.getsize(os.path.join(r, f))
                           for r, _, fs in os.walk(src) for f in fs
                           if os.path.exists(os.path.join(r, f)))
            else:
                size = os.path.getsize(src)
        except OSError:
            pass
        out["items"].append((name, size))
        if os.path.exists(os.path.join(NEW, name)):
            out["conflicts"].append(name)
    return out


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f}{u}" if u == "B" else f"{n/1:.0f}{u}" if False else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}GB"


def main(apply=False):
    s = survey()
    if not s["old_exists"]:
        print(f"  No old data ({OLD}) —— nothing to move.")
        if s["env"]:
            print(f"  ⚠ Old environment variables remain in your shell: {' '.join(s['env'])}")
            print("     Change them to KAL_ —— the old names have no effect.")
        return 0

    print(f"\n  old location  {OLD}")
    print(f"  new location  {NEW}  {'(already exists)' if s['new_exists'] else '(to be created)'}\n")
    for name, size in s["items"]:
        mark = "⚠ already there" if name in s["conflicts"] else ""
        print(f"    {name:24} {human(size):>9}  {mark}")
    print(f"\n    (venv is not moved —— uv now builds `.venv` inside the repository)")

    if s["conflicts"]:
        print(f"\n  ❌ {len(s['conflicts'])} name(s) already exist at the new location. "
              f"Nothing is overwritten.")
        print(f"     If you already migrated, there is nothing to do.  To redo it, clear that side first.")
        return 1

    if s["env"]:
        print(f"\n  ⚠ Old environment variables remain in your shell —— these I cannot fix:")
        for k in s["env"]:
            print(f"       {k}  →  KAL_{k[4:]}")

    if not apply:
        print(f"\n  To actually move it:  just migrate --apply")
        return 0

    os.makedirs(NEW, exist_ok=True)
    for name, _ in s["items"]:
        src, dst = os.path.join(OLD, name), os.path.join(NEW, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        print(f"  ✓ {name}")
    os.chmod(NEW, 0o700)
    print(f"\n  The old location is **left as it is** ({OLD}) —— delete it yourself once you "
          f"have confirmed the new one works.")
    print(f"  Check with:  just status")
    return 0


def _selftest():
    import tempfile
    ok = 0
    global OLD, NEW
    keep = (OLD, NEW)
    try:
        base = tempfile.mkdtemp()
        OLD, NEW = os.path.join(base, "old"), os.path.join(base, "new")
        os.makedirs(os.path.join(OLD, "db"))
        os.makedirs(os.path.join(OLD, "venv"))          # must not be moved
        open(os.path.join(OLD, "db", "x.lance"), "w").write("x")
        open(os.path.join(OLD, "lr_cache.jsonl"), "w").write("{}\n")
        open(os.path.join(OLD, "venv", "big"), "w").write("y" * 100)

        s = survey()
        names = [n for n, _ in s["items"]]
        assert "venv" not in names, "it tries to move venv"
        assert set(names) == {"db", "lr_cache.jsonl"}, names
        ok += 2

        #  ① without --apply it creates nothing
        main(apply=False)
        assert not os.path.exists(NEW), "the preview created files"
        ok += 1

        #  ② after moving, the contents are intact
        main(apply=True)
        assert os.path.exists(os.path.join(NEW, "db", "x.lance"))
        assert not os.path.exists(os.path.join(NEW, "venv")), "venv came along"
        assert os.path.exists(os.path.join(OLD, "db")), "the original was deleted"
        ok += 3

        #  ③ on a collision it **refuses rather than overwriting**
        assert main(apply=True) == 1, "it overwrote on a collision"
        ok += 1

        #  ④ with no old location it ends quietly (not an error)
        OLD = os.path.join(base, "nope")
        assert main(apply=True) == 0
        ok += 1
    finally:
        OLD, NEW = keep
    print(f"  ✅ kal_migrate self-check —— {ok} case(s) "
          f"(venv excluded · preview is a no-op · original preserved · collision refused · nothing there)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        raise SystemExit(0)
    raise SystemExit(main(apply="--apply" in sys.argv))
