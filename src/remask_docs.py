#!/usr/bin/env python3
"""Re-apply the masking patterns to documents that already exist.

⚠ This tool alone is not enough —— its coverage is the distilled documents and the vault
   copies, and it never touches the LanceDB that search actually runs on
   (chunks · lr_entities · lr_relations), the 3 LLM caches, the 700 kg/ entity notes,
   kal-graph.json or viewer/*.html.  After fixing a pattern, `--purge-caches` must invalidate
   the caches and everything must be rebuilt for the index to follow.  The caches are keyed on
   the chunk content hash, so wrongly extracted text left in place is **never regenerated.**

Why it is needed
  ingest_sessions.py's masking runs once, **while the transcript is being made**.  The
  documents an LLM distils afterwards are never re-checked.  So two things can leak:

    ① the pattern had a hole —— it really did.  The PEM pattern required `-----END` and missed
       a key whose tool output was cut off mid-stream.
    ② the LLM rewrote a masked position while distilling, as if restoring it

  Fixing a pattern leaves **the documents already made unchanged.**  This script cleans those.

What it walks
  ~/.kal/distilled/*.md                      the distilled originals
  <vault>/raw/conversations/sessions/*.md    the vault copies
  ~/.kal/sessions/*session_docs.json         the transcript stores —— every one distill reads

Usage:
  python remask_docs.py --dry-run     # only what it catches
  python remask_docs.py
"""
import vault_path
import os, sys, json, glob, shutil, argparse


# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ingest_sessions import SECRETS, mask, find_leaks, SYNTH

# Where the vault lives.  Mounted at /vault inside the container (see docker-compose).
VAULT = vault_path.vault()
TARGETS = [
    os.path.join(KAL_HOME, "distilled/*.md"),
    f"{VAULT}/raw/conversations/sessions/*.md",
]
#  Every transcript store —— the corpora distill reads, from its one list.  This named
#  session_docs.json alone, so the Codex and Hermes stores were never re-masked (2026-09-26).
from distill_sessions import CORPORA  # noqa: E402
STORES = [p for _, p in CORPORA]


#  `mask` is **taken from ingest_sessions as it is.**  There used to be a second implementation
#  here, and that one preserved `m.group(1)` while this one did not —— the same text gave
#  different results.  So the two could not verify each other either.  When pre-index masking
#  and post-hoc cleanup use different rules, there is no telling which to believe.  (r4-guard, 2026-08-21)


def _selftest():
    """Does this tool **really remove**, and do its targets **really exist**.

    This is a post-hoc secret cleaner.  Break `mask()` and it writes nothing and prints
    "✅ nothing caught" —— leading to the conclusion that the vault is clean.
    It does not re-run `find_leaks` on its own output, so **it cannot tell clean from broken.**
    Replacing `mask()` with a no-op still passed `just selftest`
    (r4-guard mutation, 2026-08-21).

    Only synthetic secrets are used —— this file is committed.
    """
    import glob as _g
    blob = "before " + " / ".join(v for _, v in SYNTH) + " after"
    assert find_leaks(blob), "the synthetic secrets are not caught at all —— the test is meaningless"
    masked, hits = mask(blob)
    assert hits, "the masking found nothing"
    assert not find_leaks(masked), \
        f"survives the masking: {sorted(find_leaks(masked))}"
    for _, v in SYNTH:
        assert v not in masked, "the original value survives verbatim"
    assert "[REDACTED:" in masked, "no substitution marker"
    #  ★ **At least one** target glob must really exist.  With all of them empty this tool
    #    inspects nothing and prints "clean" —— which is what happens if the vault's shape changes.
    #  ⚠ This assertion inspects **the environment**.  On a fresh clone, in a container or in
    #    CI there is no vault, so it goes red, and that red is indistinguishable from a real
    #    defect —— which is how people learn to ignore red.  It only checks when a vault really
    #    exists.  (r4-guard, round 5)
    #  ⚠ The condition was **the opposite of its own explanation**.  `KAL_HOME` (`~/.kal`)
    #     exists for everyone who ran `just setup`, so the assertion fired for a new user who
    #     had never run distillation and turned `just selftest-py` red.  A vault existing does
    #     not mean session documents exist —— those appear only after the LLM pipeline runs.
    #     CI stayed green because the runner had **no** vault.  That is, this guard fired
    #     precisely in the situation it existed to prevent.  (deep review 2026-08-25)
    #     Now it checks **only when the glob's parent exists** —— "somewhere to walk and it is empty" is the real anomaly.
    live = [t for t in TARGETS if os.path.isdir(os.path.dirname(t))]
    if live:
        assert any(_g.glob(t) for t in live), \
            f"the target folder exists and holds 0 documents —— it would say 'clean' with nothing to walk: {live}"
    else:
        print("     (no distilled or session folder yet —— target glob check skipped)")
    #  Ordinary prose is left alone (no crying wolf)
    plain = "A story that starts with sk-, the acronym AKIA, and a mention of hooks.slack.com."
    assert not find_leaks(plain) and mask(plain)[0] == plain, "it touches ordinary prose"
    #  Every transcript store is re-masked —— not the Claude one alone, as it was.  Two checks,
    #  because each mutation audit found the other one missing: the list is the corpora distill
    #  reads (a single-store list stays green on the run below, which patches it) ……
    assert sorted(STORES) == sorted(p for _, p in CORPORA), \
        f"remask walks {STORES}, distill reads {[p for _, p in CORPORA]}"
    #  …… and `run()` really visits every one of them, on three small stores in a temp dir.
    import io, contextlib, tempfile, stat
    from unittest import mock
    import distill_sessions, ingest_sessions
    with tempfile.TemporaryDirectory() as d:
        stores = []
        for n, (_, sample) in zip(("claude", "codex", "hermes"), SYNTH):
            p = os.path.join(d, f"{n}_session_docs.json")
            with open(p, "w") as fh:
                json.dump([{"session_id": n, "text": f"{n} said {sample} once"}], fh)
            stores.append(p)
        me = sys.modules[__name__]
        with mock.patch.object(me, "STORES", stores), \
                mock.patch.object(me, "TARGETS", [os.path.join(d, "none", "*.md")]):
            at = int(os.path.getmtime(stores[0])) - 3600     # collected an hour ago (whole seconds)
            for p in stores:
                os.utime(p, (at, at))
            before = [open(p).read() for p in stores]
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                run(dry=True)
            seen = [os.path.basename(p) for p in stores if f"{os.path.basename(p)}  1 hit(s)" in buf.getvalue()]
            assert len(seen) == 3, f"the dry run visited {seen} of the three stores:\n{buf.getvalue()}"
            assert [open(p).read() for p in stores] == before, "a dry run changed a store"
            with contextlib.redirect_stdout(io.StringIO()):
                run(dry=False)
            for p in stores:
                assert not find_leaks(open(p).read()), f"{os.path.basename(p)} still leaks after the run"
                assert stat.S_IMODE(os.stat(p).st_mode) == 0o600, f"{os.path.basename(p)} is left readable"
                assert os.path.getmtime(p) == at, f"{os.path.basename(p)} took a new time from the remask"
        #  …so distill's refusal to cut by a list newer than the corpus still fires after a remask.
        excl = os.path.join(d, "exclude.txt")
        with open(excl, "w") as fh:
            fh.write("codex 2026-09-25T09:00:00Z\n")
        os.utime(excl, (at + 60, at + 60))              # listed after the collection, before the remask
        corp = [(os.path.basename(p).split("_")[0], p) for p in stores]
        with mock.patch.object(distill_sessions, "CORPORA", corp), \
                mock.patch.object(distill_sessions, "OUT", os.path.join(d, "distilled")), \
                mock.patch.object(distill_sessions, "DONE", os.path.join(d, "distilled", ".done")), \
                mock.patch.object(ingest_sessions, "EXCLUDE", excl):
            try:
                distill_sessions.corpus_records()
                raise AssertionError("after a remask, distil cut by a list newer than the corpus")
            except SystemExit as e:
                assert "collect again first" in str(e), str(e)
    print("  ✅ remask_docs —— masking effective · re-verified · target globs exist · no false positives "
          "· every transcript store visited and masked, its collection time kept")


def run(dry):
    total = {}
    touched = []
    for pattern in TARGETS:
        for p in sorted(glob.glob(pattern)):
            src = open(p, encoding="utf-8").read()
            out, hits = mask(src)
            if not hits:
                continue
            touched.append((p, hits))
            for k, v in hits.items():
                total[k] = total.get(k, 0) + v
            if not dry:
                open(p, "w", encoding="utf-8").write(out)

    # The transcript stores too —— they are distillation's input, so leaving them revives them on the next run
    jhits = {}
    for store in STORES:
        if not os.path.exists(store):
            continue
        recs = json.load(open(store))
        changed, here = 0, {}
        for r in recs:
            out, hits = mask(r["text"])
            if hits:
                changed += 1
                r["text"] = out
                for k, v in hits.items():
                    here[k] = here.get(k, 0) + v
        if here and not dry:
            # ⚠ It used to copy the original to .bak here —— a tool for deleting secrets creating
            #   a file that preserves them forever.  Confirmed by measurement:
            #   session_docs.json.bak held a live, unmasked PRIVATE KEY.
            #   A backup's purpose (undoing) is served by the masked previous version too.
            #   (adversarial review 2026-08-18, BLOCKER)
            tmp = store + ".tmp"
            was = os.stat(store)
            json.dump(recs, open(tmp, "w"), ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, store)                  # an atomic swap —— there is no in-between state
            #  ⚠ …keeping the time it was collected.  distill refuses to cut by an exclude.txt newer
            #     than the corpus ("collect again first"), and a fresh time here would read as a new
            #     collection —— the refusal would go quiet while nothing had been cut (2026-09-26).
            os.utime(store, ns=(was.st_atime_ns, was.st_mtime_ns))
        if here:
            print(f"  {os.path.basename(store)}  {sum(here.values())} hit(s) across {changed} session(s)")
        for k, v in here.items():
            jhits[k] = jhits.get(k, 0) + v

    print(f"\ncaught in {len(touched)} file(s)")
    for p, hits in touched[:20]:
        print(f"    {os.path.basename(p)[:56]:<58}{hits}")
    if total or jhits:
        merged = dict(total)
        for k, v in jhits.items():
            merged[k] = merged.get(k, 0) + v
        print(f"\n  totals by pattern: {merged}")
    else:
        print("  ✅ nothing caught")
    print("  (dry-run — no file was changed)" if dry else "  ✅ applied")
    return len(touched) + (1 if jhits else 0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
    else:
        run(a.dry_run)
