#!/usr/bin/env python3
"""Delete LanceDB's old versions.

Why it is separate
  schema_v3 deletes only versions older than 24 hours at the end of a rebuild —— reads take no
  lock (MVCC), so a handle opened before the cleanup would die looking for files that are gone.
  But building several times a day leaves everything accumulated inside that window.  During
  development it really did grow from 410MB to 512MB.  Call this explicitly for that.

  This DB is a derivative regenerated in full from the vault, so old versions have no value.
  Measured 2026-08-18: 1,466MB of 1,569MB (93%) was dead versions.

Usage:
    python vacuum.py              clean up anything older than an hour
    python vacuum.py --hours 0    everything but the current one (only with no readers open)
"""
import argparse
import datetime
import os
import sys
import time


# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kal_lock import db_lock  # noqa: E402

DB = os.environ.get("KAL_PATH", os.path.join(KAL_HOME, "db"))


def size():
    return sum(os.path.getsize(os.path.join(r, f))
               for r, _, fs in os.walk(DB) for f in fs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=1.0,
                    help="delete versions older than this many hours (default 1)")
    a = ap.parse_args()

    import lancedb
    before, t0 = size(), time.time()
    with db_lock("vacuum"):
        db = lancedb.connect(DB)
        for name in sorted(db.list_tables().tables):
            s0 = size()
            try:
                db.open_table(name).optimize(
                    cleanup_older_than=datetime.timedelta(hours=a.hours))
            except Exception as e:
                print(f"  {name:<14} ✗ {e}")
                continue
            print(f"  {name:<14}reclaimed {(s0-size())/1e6:>7,.0f}MB")
    after = size()
    pct = (before - after) / before * 100 if before else 0
    print(f"\n  {before/1e6:,.0f}MB → {after/1e6:,.0f}MB   "
          f"reclaimed {(before-after)/1e6:,.0f}MB ({pct:.0f}%) · {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
