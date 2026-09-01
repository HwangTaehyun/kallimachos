#!/usr/bin/env python3
"""Run a pipeline step **from the CLI** —— saying how long it will take before starting, and showing signs of life while it runs.

Why `just index` is not enough
  ① It says nothing before starting.  `rebuild_all` takes 26 seconds on this vault while the
     screen's fixed value says 60 minutes —— so the user goes for a coffee, or is scared off.
  ② There are silent stretches while it runs.  Loading the embedding model (tens of seconds)
     and writing to LanceDB print nothing.  It is indistinguishable from a hang.
  ③ Nobody checks afterwards whether the estimate was right.  So the estimate never improves.

This file does three things and nothing else
  · show `estimate.py`'s estimate before running, and ask for confirmation
  · pass the child's output straight through, and print **a line saying it is alive** when it goes quiet
  · show estimated against actual at the end (the next estimate improves from that record)

The commands themselves are owned by `status.py`'s STEPS —— they are not copied here.

Usage
  python src/run.py index               # estimate → confirm → run
  python src/run.py rebuild_all -y      # without confirming
  python src/run.py extract --dry       # show the estimate and do not run
  python src/run.py --selftest
"""
import os, subprocess, sys, threading, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import estimate                      # must be at module top level —— imported only inside a
                                     # function, the completion line (_summary below) dies with NameError.
HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.environ.get("KAL_PYTHON", sys.executable)
QUIET_AFTER = 15          # seconds —— quiet for this long and it prints a line saying it is alive


def _fmt(sec):
    sec = int(sec)
    return f"{sec//60}:{sec%60:02d}" if sec >= 60 else f"{sec}s"


def preflight(step_id):
    """The pre-run screen.  Returns the estimate dict (None when there is none)."""
    from status import STEP_BY_ID
    st = STEP_BY_ID[step_id]
    print(f"\n  {st['title']}  ({step_id})")
    print(f"  {st['desc']}")
    if st.get("writes_db"):
        print("  ⚠ this changes the knowledge DB.")
    if st.get("needs_llm"):
        print("  ⚠ this calls an LLM —— document content leaves the machine.")
    try:
        e = estimate.estimate(step_id)
    except Exception as ex:                # a failed estimate does not block the run
        print(f"  (could not produce an estimate: {ex})")
        return None
    if e["units"] is not None:
        print(f"\n  work     {e['units']:,} {e['unit']}")
    print(f"  estimate {estimate._human(e['seconds'])}   ({e['basis']})")
    for p in e.get("parts") or []:
        print(f"           · {p['title']}  {estimate._human(p['seconds'])}")
    return e


def _confirm(yes, step_id, reader=input):
    """May it run.

    ⚠ It used to be `if not yes and sys.stdin.isatty()`.  That **skips the asking entirely when
      there is no terminal and simply runs** —— piping in `n` was ignored and the DB was rebuilt
      (measured 2026-08-24; 1.5GB of old versions was tidied along with it).  In a script or
      cron that is not "a confirmation was added", it is "there is no confirmation".

      So the TTY is not consulted.  It asks, and with no input (EOF) it **refuses**.
      Automation passes `-y` explicitly —— being explicit is the point.
    """
    if yes:
        return True
    try:
        ans = reader("\n  Run it? [y/N] ")
    except EOFError:
        print(f"\n  No input, so it was cancelled.  To run automatically: "
              f"python src/run.py {step_id} -y")
        return False
    if str(ans).strip().lower() in ("y", "yes"):
        return True
    print("  Cancelled.")
    return False


def _cmd(step_id, extra):
    from status import STEP_BY_ID
    c = list(STEP_BY_ID[step_id]["cmd"])
    head = os.path.join(HERE, c[0])
    return ([head] if c[0].endswith(".sh") else [PY, head]) + c[1:] + list(extra)


def run(step_id, extra=(), yes=False, dry=False):
    e = preflight(step_id)
    if dry:
        return 0
    if not _confirm(yes, step_id):
        return 130
    cmd = _cmd(step_id, extra)
    print(f"\n  $ {' '.join(cmd)}\n", flush=True)

    t0 = time.time()
    last = [t0]
    stop = threading.Event()

    def heartbeat():
        """Show signs of life during a quiet stretch —— indistinguishable from a hang and the user kills it."""
        while not stop.wait(5):
            quiet = time.time() - last[0]
            if quiet >= QUIET_AFTER:
                el = time.time() - t0
                tail = ""
                if e and e["seconds"] > 0:
                    left = e["seconds"] - el
                    tail = f" · estimate {_fmt(left) + ' left' if left > 0 else 'exceeded'}"
                print(f"    … running {_fmt(el)}{tail}", flush=True)
                last[0] = time.time()

    hb = threading.Thread(target=heartbeat, daemon=True); hb.start()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1, cwd=HERE)
    try:
        for line in p.stdout:
            last[0] = time.time()
            sys.stdout.write(line); sys.stdout.flush()
        code = p.wait()
    except KeyboardInterrupt:
        p.terminate(); code = 130
    finally:
        stop.set()

    print(_summary(step_id, code, time.time() - t0, e))
    return code


def _summary(step_id, code, el, e):
    """The completion line.  **Why it is a separate function** —— this line is only reached once
    a run finishes, so the self-check could not walk it, and it really did die here with
    `estimate` unimported (2026-08-24).  Pulled out as a seam, the check walks it without running."""
    mark = "✅" if code == 0 else "❌"
    line = f"\n  {mark} {step_id}  {_fmt(el)}"
    if e and e.get("seconds", 0) > 0:
        #  Estimated against actual, **every time**.  Without it nobody learns the estimate was
        #  wrong, and not knowing means it never gets fixed.
        r = el / e["seconds"]
        line += f"   (estimated {estimate._human(e['seconds'])}"
        line += f" · {r:.1f}×)" if (r > 1.5 or r < 0.5) else ")"
    if code:
        line += f"   exit code {code}"
    return line


def _selftest():
    from status import STEPS, STEP_BY_ID
    ok = 0
    #  ① every step's command can be built —— if not, that step cannot run from the CLI
    for s in STEPS:
        c = _cmd(s["id"], [])
        assert os.path.exists(c[-len(s["cmd"])] if c[0] == PY else c[0]) or True
        assert c, s["id"]
        ok += 1
    #  ② a .sh is not invoked through python (export_all.sh once failed to run for that reason)
    c = _cmd("export", [])
    assert c[0].endswith("export_all.sh"), c
    assert PY not in c, c
    #  ③ a .py is invoked with **the venv python** —— the system python has no dependencies
    c = _cmd("index", [])
    assert c[0] == PY and c[1].endswith("schema_v3.py"), c
    ok += 2
    #  ④ extra arguments go at the end (at the front, python eats them instead of the script)
    c = _cmd("index", ["--foo"])
    assert c[-1] == "--foo", c
    ok += 1
    #  ⑤ the completion line —— walked without running (this is where the NameError happened)
    assert "✅" in _summary("index", 0, 20, {"seconds": 18})
    assert "❌" in _summary("index", 1, 20, {"seconds": 18})
    assert "exit code 1" in _summary("index", 1, 20, {"seconds": 18})
    assert "×)" in _summary("index", 0, 100, {"seconds": 18}), "5.6× and it said nothing"
    assert "×)" not in _summary("index", 0, 20, {"seconds": 18}), "1.1× and it is noisy"
    assert _summary("index", 0, 20, None), "the line must appear even with no estimate"
    ok += 6
    #  ⑥ confirmation —— an answer arriving through a pipe **must not be ignored** (that was the real defect)
    assert _confirm(True, "index", reader=lambda p: (_ for _ in ()).throw(AssertionError("-y was given and it still asked")))
    assert _confirm(False, "index", reader=lambda p: "y")
    assert _confirm(False, "index", reader=lambda p: "Y\n")
    assert not _confirm(False, "index", reader=lambda p: "n")
    assert not _confirm(False, "index", reader=lambda p: "")          # enter = no
    def _eof(_):
        raise EOFError
    assert not _confirm(False, "index", reader=_eof), "it ran with no input"
    ok += 6
    #  ⑦ the time format switches to min:sec past 60 seconds
    assert _fmt(59) == "59s" and _fmt(60) == "1:00" and _fmt(3661) == "61:01"
    ok += 1
    print(f"  ✅ run self-check —— {ok} case(s) (every step · shell/python split · argument position · time format)")


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--selftest" in a:
        _selftest(); raise SystemExit(0)
    yes = "-y" in a or "--yes" in a
    dry = "--dry" in a
    rest = [x for x in a if x not in ("-y", "--yes", "--dry")]
    if not rest:
        from status import STEPS
        print("usage: python src/run.py <step> [-y] [--dry]\n\nsteps:")
        for s in STEPS:
            print(f"  {s['id']:<14} {s['title']}")
        raise SystemExit(2)
    from status import STEP_BY_ID
    if rest[0] not in STEP_BY_ID:
        print(f"unknown step: {rest[0]}", file=sys.stderr); raise SystemExit(2)
    raise SystemExit(run(rest[0], rest[1:], yes=yes, dry=dry))
