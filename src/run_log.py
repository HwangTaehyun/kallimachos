#!/usr/bin/env python3
"""Record pipeline runs under `~/.kal/runs/` —— **in the same place as the web UI.**

Why it is needed
  The web screen's "last run 1 day ago · failed" knows only about **runs started from the web
  UI**.  Run from the CLI or outside the container and nothing is recorded, so three successes
  today still leave yesterday's failure showing as the last —— the user concludes "it is broken right now".

  The record format is one line of JSON, so the CLI can write it too.  One place holds the truth.

The writing side (each step's script calls this):

    from run_log import record
    with record("extract") as r:
        ...
        r.lines = 32          # optional —— the number of output lines

  On an exception it records exit_code=1 and re-raises.  A clean exit records 0.
  **A failed record does not kill the pipeline** —— the log is incidental.
"""
import contextlib, json, os, time

KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
RUNS = os.path.join(KAL_HOME, "runs")


class _Run:
    def __init__(self, step):
        self.step = step
        self.lines = 0
        #  How many "units of work" this run actually processed (LLM calls · chunks · sessions …).
        #  estimate.py **learns speed per unit** from this —— without it the seed constants
        #  keep being used and the estimate never improves.  It is safe to omit
        #  (None simply drops that record from the learning).
        self.units = None
        self.id = time.strftime("%Y%m%d-%H%M%S")
        self.started_at = int(time.time())


#  Where a run in progress reports "how many did it process".
#  Why a global —— the code that knows the count (lr_extract's todo calculation, say) sits
#  three or four frames inside where `record()` was opened.  Carrying a return value out
#  there would mean changing the main contract of four files, and missing one would drop
#  that step from the learning, silently.
_PENDING = {"units": None}


def count(n):
    """How many units of work this run processed.  Call it anywhere inside `record()`."""
    _PENDING["units"] = n


@contextlib.contextmanager
def record(step, lines=0, units=None):
    r = _Run(step)
    r.lines = lines
    r.units = units
    _PENDING["units"] = None          # stops a previous run's value from leaking
    code = 0
    try:
        yield r
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        raise
    except BaseException:
        code = 1
        raise
    finally:
        try:
            os.makedirs(RUNS, exist_ok=True)
            payload = {
                "id": r.id, "step": r.step,
                "started_at": r.started_at, "ended_at": int(time.time()),
                "exit_code": code,
                "status": "ok" if code == 0 else "failed",
                "lines": int(r.lines or 0),
                #  An explicit argument wins.  Otherwise the value reported through count().
                "units": (int(r.units) if r.units is not None
                          else (int(_PENDING["units"]) if _PENDING["units"] is not None else None)),
                # Distinguished from a run started by the web.  The screen has to be able to say
                # "where was this run from", or people ask "why is my CLI run not showing".
                "origin": "cli",
            }
            with open(os.path.join(RUNS, r.id + ".json"), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
        except Exception:
            pass          # a failed record does not kill the pipeline


def write(step, code, started_at, lines=0):
    """Record an already-finished run —— **for shell entry points.**

    `record()` is a context manager and therefore usable only from Python.  But
    `export_all.sh` and `rebuild_all.sh` are shell, which is why the `export` step
    left **no record at all** when run from the CLI —— exactly the state this module's
    docstring exists to prevent ("three successes today and the screen shows yesterday's failure").
    (2026-08-21)

        python run_log.py --step export --code $? --started $T0
    """
    r = _Run(step)
    r.started_at = int(started_at)
    r.lines = lines
    try:
        os.makedirs(RUNS, exist_ok=True)
        payload = {"id": r.id, "step": r.step, "started_at": r.started_at,
                   "ended_at": int(time.time()), "exit_code": int(code),
                   "status": "ok" if int(code) == 0 else "failed",
                   "lines": int(lines or 0), "origin": "cli"}
        with open(os.path.join(RUNS, f"{r.id}.json"), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
    except Exception:
        pass              # a failed record does not kill the pipeline


if __name__ == "__main__" and "--step" in os.sys.argv:
    import argparse
    _ap = argparse.ArgumentParser()
    _ap.add_argument("--step", required=True)
    _ap.add_argument("--code", type=int, default=0)
    _ap.add_argument("--started", type=int, default=int(time.time()))
    _a = _ap.parse_args()
    write(_a.step, _a.code, _a.started)
    raise SystemExit(0)


if __name__ == "__main__":
    # Self-check —— is a record really written, and is an exception caught as a failure
    import tempfile, shutil
    tmp = tempfile.mkdtemp()
    try:
        globals()["RUNS"] = os.path.join(tmp, "runs")
        with record("selftest") as r:
            r.lines = 7
        f = os.listdir(globals()["RUNS"])[0]
        d = json.load(open(os.path.join(globals()["RUNS"], f), encoding="utf-8"))
        assert d["status"] == "ok" and d["lines"] == 7 and d["origin"] == "cli", d
        try:
            with record("selftest-fail"):
                raise RuntimeError("deliberate")
        except RuntimeError:
            pass
        bad = [json.load(open(os.path.join(globals()["RUNS"], x), encoding="utf-8"))
               for x in os.listdir(globals()["RUNS"])]
        assert any(x["status"] == "failed" for x in bad), "an exception is not caught as a failure"
        print("  ✅ run_log self-check passed — both success and failure recorded")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
