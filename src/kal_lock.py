#!/usr/bin/env python3
"""A DB write lock — so two scripts cannot edit the same LanceDB at once.

Why it is needed
  schema_v3.py, sync_v3.py and manual_index.py all replace tables with `mode="overwrite"`, and
  sync_v3 mixes delete and add.  Overlap any two of the three and one side reads a table the
  other is mid-write on, leaving **a quietly inconsistent DB**.
  It is silent corruption rather than a crash, which makes it hard to notice.

  This was left as "concurrency — unverified" in the 2026-08-17 review.

Why flock
  Only mutual exclusion between processes on one machine is needed.  One file suffices, and
  when a process dies the kernel releases it (a leftover lock file does not lock anything).

Usage:
    from kal_lock import db_lock
    with db_lock("schema_v3"):
        ...writes...
Read-only scripts (kal_search and the like) do not need it — LanceDB does not block writes
during a read, and what we want to block is writers colliding with each other.
"""
import contextlib
import errno
import fcntl
import os
import sys
import time


# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
LOCK_PATH = os.path.join(KAL_HOME, ".write.lock")


@contextlib.contextmanager
def db_lock(who: str, timeout: float = 0.0):
    """An exclusive lock.  With timeout=0 it fails immediately and says who is holding it.

    Why not waiting is the default — the write jobs in this pipeline run 90 seconds to 3 hours.
    Waiting in silence makes the user think it has hung.  Better to name the holder and die.
    """
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    fh = open(LOCK_PATH, "a+", encoding="utf-8")
    deadline = time.time() + timeout
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EACCES):
                raise
            if time.time() >= deadline:
                fh.seek(0)
                holder = fh.read().strip() or "(unknown)"
                fh.close()
                sys.exit(
                    f"❌ the DB is already in use — {holder}\n"
                    f"   Editing the same DB concurrently corrupts it silently.\n"
                    f"   Run again once that job finishes. "
                    f"(If it really is a stale lock, rm {LOCK_PATH})"
                )
            time.sleep(0.5)
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(f"{who} · pid {os.getpid()} · {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        fh.flush()
        yield
    finally:
        try:
            fh.seek(0)
            fh.truncate()
            fh.flush()
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


if __name__ == "__main__":
    import subprocess
    # Self-check — does a second process really get blocked
    with db_lock("selftest-A"):
        r = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r);\n"
             "from kal_lock import db_lock\n"
             "with db_lock('selftest-B'): print('WRONG: the second one took it')"
             % os.path.dirname(os.path.abspath(__file__))],
            capture_output=True, text=True)
    assert r.returncode != 0, "the second process took the lock"
    assert "already in use" in (r.stdout + r.stderr), (r.stdout, r.stderr)
    assert "selftest-A" in (r.stdout + r.stderr), "it does not say who is holding it"
    # ★ **The contract with Go** —— `api/main.go:lockHolder()` decides "who holds it" by reading
    #   this file's **contents**, not by flock (:658).  If Python stops writing the contents,
    #   Go's guard goes permanently blind —— and the Python-side test watches only flock, so
    #   **nothing breaks**.  It is quieter still for crossing a language boundary.
    with db_lock("selftest-D"):
        _body = open(LOCK_PATH, encoding="utf-8").read().strip()
        assert _body, "the lock is held and the file is empty —— Go's lockHolder() reads that as 'no lock'"
        assert "selftest-D" in _body, f"who took it is not written to the file: {_body!r}"
        assert f"pid {os.getpid()}" in _body, f"no pid —— there is no way to judge a stale lock: {_body!r}"
    #   Releasing must empty it —— otherwise Go blocks forever
    assert not open(LOCK_PATH, encoding="utf-8").read().strip(), \
        "the lock was released and the file still has contents —— Go keeps reading it as 'in use'"

    # Once released it must be acquirable again
    with db_lock("selftest-C"):
        pass

    print("  ✅ self-check passed — concurrent runs blocked · holder shown · reacquired after release")
