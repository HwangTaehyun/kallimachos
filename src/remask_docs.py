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
  ~/.kal/sessions/session_docs.json          the transcript store

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
SESSIONS_JSON = os.path.join(KAL_HOME, "sessions/session_docs.json")


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
    print("  ✅ remask_docs —— masking effective · re-verified · target globs exist · no false positives")


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

    # The transcript store too —— it is distillation's input, so leaving it revives them on the next run
    jhits = {}
    if os.path.exists(SESSIONS_JSON):
        recs = json.load(open(SESSIONS_JSON))
        changed = 0
        for r in recs:
            out, hits = mask(r["text"])
            if hits:
                changed += 1
                r["text"] = out
                for k, v in hits.items():
                    jhits[k] = jhits.get(k, 0) + v
        if jhits and not dry:
            # ⚠ It used to copy the original to .bak here —— a tool for deleting secrets creating
            #   a file that preserves them forever.  Confirmed by measurement:
            #   session_docs.json.bak held a live, unmasked PRIVATE KEY.
            #   A backup's purpose (undoing) is served by the masked previous version too.
            #   (adversarial review 2026-08-18, BLOCKER)
            tmp = SESSIONS_JSON + ".tmp"
            json.dump(recs, open(tmp, "w"), ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, SESSIONS_JSON)          # an atomic swap —— there is no in-between state
        if jhits:
            print(f"  session_docs.json  {sum(jhits.values())} hit(s) across {changed} session(s)")

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
