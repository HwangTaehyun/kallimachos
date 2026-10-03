"""Local, opt-in count of how many characters of note text left this machine through kal_search.

Why this exists —— the benchmark (docs/BENCH-KG-VS-SEARCH.md §7-3) measured that one question sends
about 62 KB of note fragments to the model provider through `kal_search` alone.  A user who wants to
see that number should be able to; nobody else should.  So:

  · **Off by default.**  `KAL_USAGE=1` turns it on.  A hosted process must never write it ——
    `KAL_USAGE_HOSTED=1` (set by the cloud MCP) forces it off whatever else says, because the hosted
    child has a writable HOME and a shared counter there would be server-side storage of per-user
    activity (adversarial review, 2026-10-03).
  · **Monthly integer totals only**: `{"2026-10": 123456}`.  No query, no doc_id, no timestamp finer
    than the month —— the file must not become a log of what was asked.
  · **A directory of its own**, `~/.kal/usage/` mode 0700: a 0600 file in a browsable directory still
    leaks call times through mtime and size.
  · **Atomic write** (temp file + os.replace) under a **persistent sidecar lock** `.lock` ——
    `flock` on the file being replaced is lost with the old inode, so three MCP servers writing
    at once would drop increments (same review).

`record(chars)` is the whole API.  It never raises: a failure to count is not a failure to answer.
"""
import datetime
import fcntl
import json
import os
import tempfile

ENABLE_VAR = "KAL_USAGE"
HOSTED_VAR = "KAL_USAGE_HOSTED"


def enabled(env=os.environ):
    return env.get(ENABLE_VAR) == "1" and env.get(HOSTED_VAR) != "1"


def usage_dir(home=None):
    return os.path.join(home or os.environ.get("KAL_HOME", os.path.expanduser("~/.kal")), "usage")


def record(chars, home=None, month=None, env=os.environ):
    """Add `chars` to this month's total.  Returns the new total, or None when off / failed."""
    if not enabled(env) or not isinstance(chars, int) or chars <= 0:
        return None
    try:
        d = usage_dir(home)
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
        path = os.path.join(d, "usage.json")
        key = month or datetime.date.today().strftime("%Y-%m")
        lock_fd = os.open(os.path.join(d, ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            totals = {}
            try:
                with open(path, encoding="utf-8") as fh:
                    loaded = json.load(fh)
                totals = {k: int(v) for k, v in loaded.items()
                          if isinstance(k, str) and len(k) == 7 and isinstance(v, int)}
            except (OSError, ValueError, AttributeError):
                totals = {}
            totals[key] = totals.get(key, 0) + chars
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".usage.", suffix=".tmp")
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as out:
                    json.dump(totals, out, sort_keys=True)
                os.replace(tmp, path)
                os.chmod(path, 0o600)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return totals[key]
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
    except Exception:
        return None


def _selftest():
    import concurrent.futures
    import stat
    with tempfile.TemporaryDirectory() as home:
        env_off = {}
        assert record(100, home=home, env=env_off) is None and not os.path.exists(usage_dir(home)), "off by default, writes nothing"
        env_hosted = {ENABLE_VAR: "1", HOSTED_VAR: "1"}
        assert record(100, home=home, env=env_hosted) is None and not os.path.exists(usage_dir(home)), "hosted must never write"
        env_on = {ENABLE_VAR: "1"}
        assert record(100, home=home, month="2026-10", env=env_on) == 100
        assert record(50, home=home, month="2026-10", env=env_on) == 150
        d = usage_dir(home)
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700, "usage dir must be 0700"
        assert stat.S_IMODE(os.stat(os.path.join(d, "usage.json")).st_mode) == 0o600, "usage.json must be 0600"
        data = json.load(open(os.path.join(d, "usage.json")))
        assert data == {"2026-10": 150} and all(len(k) == 7 and isinstance(v, int) for k, v in data.items()), "month → int only"
        # parallel increments must not be lost (the sidecar lock is what guarantees it)
        with concurrent.futures.ThreadPoolExecutor(8) as ex:
            list(ex.map(lambda _: record(1, home=home, month="2026-11", env=env_on), range(200)))
        assert json.load(open(os.path.join(d, "usage.json")))["2026-11"] == 200, "parallel increments were lost"
        # a corrupt file is replaced, not crashed on
        open(os.path.join(d, "usage.json"), "w").write("{not json")
        assert record(5, home=home, month="2026-12", env=env_on) == 5
    print("  ✅ usage —— off by default · hosted forced off · 0700 dir · 0600 file · month→int only · 200 parallel increments kept · corrupt file replaced")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
