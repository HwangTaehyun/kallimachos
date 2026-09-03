#!/usr/bin/env python3
"""Promote distilled documents into the vault — brain-ingest steps 5–8.

brain-ingest's SKILL.md says "never write outside the vault", so the distilled results have
to land under the vault's raw/conversations/.  They go under raw/conversations/sessions/ so
that the path itself shows they came from sessions.

Of the vault CLAUDE.md's 10 INGEST steps, what is actually done — honestly:

  1 read the whole source        ✅ distill_sessions.py reads all of it through an LLM
  2 establish why_captured       △  the LLM proposes; the user reviews afterwards (_distill_review.tsv)
  3 discuss the key takeaway     ❌ 464 sessions cannot be discussed one by one — the summary sits at the top of each document
  4 write a wiki/sources/ summary △  written as **one corpus page**.  900 pages would bury the
                                    97 curated notes (a gold-in/gold-out violation)
  5 update existing pages        ❌ not done — automatic merging quietly buries contradictions
  6 new entity pages             ❌ not done — the knowledge graph (lr_entities) plays that role instead
  7 update wiki/index.md         ✅ one line registering the corpus page
  8 wiki/log.md append      ✅
  9 lint                         ❌ not done
 10 report                       ✅ stdout + README

Skipping 5, 6 and 9 is a matter of scale.  It is a compromise, not full compliance.
To ingest one session properly, pick that single document and run brain-ingest interactively
again (the raw/ original is already there, so it can resume from step 5).

Undo:  git -C <vault> revert --no-edit <sha>
       (reset --hard is deliberately not suggested —— it destroys unstaged work too)
"""
import vault_path
import os, re, glob, shutil, argparse, collections, datetime, subprocess


# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
# Where the vault lives.  Mounted at /vault inside the container (see docker-compose).
VAULT = vault_path.vault()
SRC = os.path.join(KAL_HOME, "distilled")
DEST = os.path.join(VAULT, "raw/conversations/sessions")
PAGE = os.path.join(VAULT, "wiki/sources/claude-session-corpus.md")
INDEX = os.path.join(VAULT, "wiki/index.md")
LOG = os.path.join(VAULT, "wiki/log.md")


def fm(path):
    """A shallow frontmatter parse — split at the first colon only, so values may contain colons."""
    d, txt = {}, open(path, encoding="utf-8").read()
    m = re.match(r"---\n(.*?)\n---\n", txt, re.S)
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            if v.strip():
                d[k.strip()] = v.strip().strip('"')
    return d


def corpus_page(metas, today):
    types = collections.Counter(m.get("doc_type", "?") for m in metas)
    projs = collections.Counter(m.get("session_project", "?") for m in metas)
    tags = collections.Counter(t.strip() for m in metas
                               for t in m.get("tags", "").strip("[]").split(",") if t.strip())
    days = sorted(m.get("captured", "") for m in metas if m.get("captured"))
    sess = {m.get("session_id") for m in metas}

    def rows(c, n, fmt="{k}"):
        return "\n".join(f"| {fmt.format(k=k)} | {v} |" for k, v in c.most_common(n))

    return f"""---
title: "Claude Code session corpus"
type: source
created: {today}
tags: [claude-code, session-log, corpus, knowledge-db]
why_captured: "To find again the decisions made in past conversations and the reasons behind them.  The individual sessions live in raw/conversations/sessions/ and this page is the map of the whole thing."
---

# Claude Code session corpus

**{len(sess)} Claude Code sessions** distilled by an LLM into **{len(metas)} documents**.
Not transcripts —— only conclusions, decisions and rationale ([[gold-in-gold-out]]).
Covering {days[0] if days else '?'} to {days[-1] if days else '?'}.

This page is **the map of the corpus**.  Individual documents are not summarised here — search finds them.

## How it was built

```
~/.claude/projects/*.jsonl          the original sessions
   │  ingest_sessions.py            secret masking (Slack · AWS · Anthropic · OpenAI keys)
   ▼
{len(sess)} transcript markdown files
   │  distill_sessions.py           LLM distillation — transcripts and tool logs removed, split by topic
   ▼
raw/conversations/sessions/*.md     {len(metas)} of them  ← this corpus
   │  lr_extract.py                 entity and relation extraction
   ▼
knowledge graph + LanceDB index     search: the /kal-search skill
```

## Composition

### Document types

| doc_type | count |
|---|---|
{rows(types, 10)}

### Top 15 projects

| project | documents |
|---|---|
{rows(projs, 15)}

### Frequent topics

| tag | frequency |
|---|---|
{rows(tags, 20)}

## Limits — to my future self reading this

- **An LLM distilled these.**  The nuance and the false starts of the original conversation are
  gone.  For the exact original, find it under `~/.claude/projects/` by `session_id`.
- **`why_captured` and `doc_type` are LLM proposals too.**  The user did not approve them one by one.
  Review list: `~/.kal/distilled/_distill_review.tsv`
- **The guesses and mistakes of that moment are mixed in.**  Do not treat a session document as fact.
  A curated `wiki/` page takes precedence.
- **For very long sessions the head and tail were taken first** (an 8-fragment cap).  The middle may be missing.

## Searching

```bash
kal_search.py "query" --origin session     # session documents only
kal_search.py "query" --origin vault       # curated notes only
kal_search.py "query"                      # everything
```

Related: [[kallimachos-build-plan]] · [[gold-in-gold-out]]
"""


SHRINK_RATIO = 0.8


#  A marker attached only to documents this script wrote.  `distill_sessions.py:249` inserts it.
#  Measured (2026-08-28): all 280 in the real vault carry it.
OWN_MARK = "origin: claude-session"


def _frontmatter(path):
    """The raw frontmatter block, or ''.  Body text is never searched for ownership."""
    try:
        raw = open(path, encoding="utf-8", errors="ignore").read()
    except OSError:
        return ""
    m = re.match(r"\A\ufeff?\s*---[ \t]*\r?\n(.*?)\r?\n(?:---|\.\.\.)[ \t]*\r?\n", raw, re.S)
    return m.group(1) if m else ""


def owned(d):
    """Only the `.md` files inside `d` **that this script wrote**.  The rest belong to someone else.

    ⚠ **Frontmatter only.**  This used to look for `OWN_MARK` anywhere in the first 2,000
       characters, so a hand-written note *about* how session documents are marked —— one that
       merely contains the words `origin: claude-session` in a sentence —— was classified as ours
       and reached `os.remove`.  Worse, `foreign()` excludes what `owned()` claims, so the
       "leaving N file(s) that belong to someone else" reassurance stayed silent about it.
       `openwiki_emit.OWNED_LINE` was hardened against exactly this class and says so in its own
       comment; this script never got the fix.  (reproduced 2026-09-04)
    """
    out = []
    for f in sorted(glob.glob(os.path.join(d, "*.md"))):
        #  ⚠ **Column zero, and block-scalar bodies removed.**  Restricting the search to the
        #     frontmatter was not enough: `^[ \t]*` still matched a **nested** key (`meta:` then an
        #     indented `origin: claude-session`) and the *content* of a `description: |` scalar.
        #     Both were classified as ours and reached `os.remove`, and `foreign()` stayed silent
        #     about them because it excludes whatever `owned()` claims.  A top-level YAML key has
        #     no leading whitespace, so anchoring at column zero is the rule —— the same one
        #     `openwiki_emit.OWNED_LINE` arrived at.  (reproduced 2026-09-04)
        #
        #  ⓘ A `_scalar_free()` pass stood here briefly, stripping `|`/`>` block-scalar bodies.
        #     A mutation test could not distinguish it: YAML requires a block scalar's content to
        #     be indented, so the column-zero anchor already excludes it, and removing the call
        #     left every check green.  A branch no test can tell apart is a branch that rots, so
        #     it is gone —— the anchor is the whole rule.
        if re.search(r"^" + re.escape(OWN_MARK), _frontmatter(f), re.M):
            out.append(f)
    return out


def foreign(d):
    """Everything inside `d` that is **not this script's** (subfolders included)."""
    if not os.path.isdir(d):
        return []
    mine = set(owned(d))
    out = []
    for f in glob.glob(os.path.join(d, "**", "*"), recursive=True):
        if os.path.isfile(f) and f not in mine:
            out.append(f)
    return sorted(out)


def is_openwiki_bundle(root):
    """Is this an openwiki bundle rather than an Obsidian vault?

    Two markers, both written by `openwiki_emit.py` and by nothing else: the generated manifest,
    and the root index carrying `okf_version` —— OKF §12 allows that key **only** there, so finding
    it is unambiguous.  Either alone is enough: a bundle mid-build may have written one not the other.
    """
    if os.path.exists(os.path.join(root, ".page-manifest.json")):
        return True
    try:
        return "okf_version" in open(os.path.join(root, "index.md"),
                                     encoding="utf-8", errors="replace").read(2000)
    except OSError:
        return False


def vault_is_git(v):
    """Is undoing possible.  `git revert` is this script's only recovery mechanism."""
    r = subprocess.run(["git", "-C", v, "rev-parse", "--git-dir"],
                       capture_output=True, text=True)
    return r.returncode == 0


def would_shrink(n_new, n_existing):
    """Would this shrink the vault's document count.

    `DEST` is emptied and refilled, so if SRC is **partially** empty that much disappears from
    the vault —— and this script then goes on to git commit it.  It passes quietly.

    **This is the repository's only data-loss guard, and nothing was running it.**  The
    self-check only watched whether `--dry-run` exits 0, which returns **before** the `rmtree`.
    Replacing the guard with `if False:` still passed `just selftest`.  (r4-guard, 2026-08-21)
    """
    return bool(n_existing) and n_new < n_existing * SHRINK_RATIO


def _selftest():
    import tempfile, subprocess, pathlib as _pl

    # ⓪ **Do not delete other people's files.**  The most expensive defect was here ——
    #    it used to be `shutil.rmtree(DEST)`, and the only guard, `would_shrink`, counts
    #    top-level `*.md` alone, so it **could not see** attachments or subfolders.  Measured
    #    (2026-08-28): a folder with 200 attachments and hand-written notes went from **201
    #    files to 3**, and the guard never fired once.
    with tempfile.TemporaryDirectory() as _d:
        _dest = os.path.join(_d, "sessions")
        os.makedirs(os.path.join(_dest, "attachments"))
        open(os.path.join(_dest, "attachments", "a.png"), "w").close()
        open(os.path.join(_dest, "mine.md"), "w", encoding="utf-8").write("a hand-written note\n")
        open(os.path.join(_dest, "ours.md"), "w", encoding="utf-8").write(
            "---\ntitle: x\n" + OWN_MARK + "\n---\n")
        assert owned(_dest) == [os.path.join(_dest, "ours.md")], \
            f"the ownership test is wrong: {owned(_dest)}"
        _f = foreign(_dest)
        assert os.path.join(_dest, "mine.md") in _f, "a hand-written .md is treated as ours"
        assert os.path.join(_dest, "attachments", "a.png") in _f, "attachments go unseen"
        assert len(_f) == 2, f"the count of other people's files is wrong: {_f}"

    # ⓪-b **If it cannot be undone, do not delete.**  In a vault that is not a git repository
    #    it used to delete first and then die on `git add` with 128 —— no files, no history.
    with tempfile.TemporaryDirectory() as _d:
        assert not vault_is_git(_d), "a non-git directory is reported as git"
        subprocess.run(["git", "-C", _d, "init", "-q"], check=True)
        assert vault_is_git(_d), "a git repository is reported as not git"

    # ① The data-loss guard **makes a judgement**
    assert would_shrink(5, 280), "280 down to 5 is not blocked"
    assert would_shrink(223, 280), "a drop of more than 20% is not blocked"
    assert not would_shrink(224, 280), "within 20% and it blocks —— healthy runs die"
    assert not would_shrink(300, 280), "it blocks a growing count"
    assert not would_shrink(5, 0), "it blocks the first run (0 existing)"

    # ② Does the commit hold **only what this script wrote**.
    #    Narrowing `add` without narrowing `commit` sweeps in someone else's pre-staged
    #    changes.  Measured (2026-08-21): the vault had 2 renames and 2 viewer/*.html
    #    deletions staged, and following the suggested `git revert` resurrects a file holding real names.
    def g(d, *a):
        return subprocess.run(["git", "-C", d, *a], capture_output=True, text=True)
    with tempfile.TemporaryDirectory() as d:
        g(d, "init", "-q"); g(d, "config", "user.email", "t@t"); g(d, "config", "user.name", "t")
        for f in ("mine.txt", "other.txt"):
            _pl.Path(d, f).write_text("v1\n")
        g(d, "add", "-A"); g(d, "commit", "-q", "-m", "base")
        _pl.Path(d, "other.txt").write_text("someone else's work\n"); g(d, "add", "other.txt")
        _pl.Path(d, "mine.txt").write_text("v2\n")
        paths = ["mine.txt"]
        g(d, "add", "--", *paths)
        staged = g(d, "diff", "--cached", "--name-only", "--", *paths).stdout.split()
        assert staged == paths, f"the query counts other people's files too: {staged}"
        g(d, "commit", "-q", "-m", "ingest", "--", *paths)
        got = g(d, "show", "--name-only", "--format=", "HEAD").stdout.split()
        assert got == paths, f"someone else's change was swept into the commit: {got}"
        left = g(d, "diff", "--cached", "--name-only").stdout.split()
        assert left == ["other.txt"], f"someone else's staged change was disturbed: {left}"
    # ③ ★ **Does the caller go through that guard.**  ① only checks that the predicate is
    #    right —— replacing `if would_shrink(...)` with `if False:` still passes ①.
    #    And then 280 vault documents are deleted and committed.  The predicate beautifully
    #    checked and the path entirely bypassable —— the defect this session kept catching,
    #    one layer up.  (r4-guard, round 5, 2026-08-21)
    #
    #    The predicate is replaced with "always fire" and **the real main is run.**  If the
    #    wiring is alive, SystemExit must follow.  Every path is isolated into a temp directory.
    import tempfile as _tf, sys as _sys
    _g = globals()
    _keep = {k: _g[k] for k in ("SRC", "DEST", "VAULT", "PAGE", "INDEX", "LOG",
                                "would_shrink")}
    _argv = _sys.argv
    try:
        with _tf.TemporaryDirectory() as _d:
            _g["VAULT"] = _d
            _g["SRC"] = os.path.join(_d, "distilled")
            _g["DEST"] = os.path.join(_d, "raw", "sessions")
            _g["PAGE"] = os.path.join(_d, "wiki", "page.md")
            _g["INDEX"] = os.path.join(_d, "wiki", "index.md")
            _g["LOG"] = os.path.join(_d, "wiki", "log.md")
            os.makedirs(_g["SRC"]); os.makedirs(_g["DEST"])
            #  ⚠ **Make the vault a git repository.**  Otherwise the new git guard stops
            #     before the shrink guard, and this test passes for the wrong reason.
            subprocess.run(["git", "-C", _g["VAULT"], "init", "-q"], check=True)
            _pl.Path(_g["SRC"], "a.md").write_text("---\ntitle: t\n---\nbody\n",
                                                   encoding="utf-8")
            _g["would_shrink"] = lambda *_a: True        # it must fire
            _sys.argv = ["promote_distilled.py"]
            try:
                _main_promote()
            except SystemExit as e:
                assert "would disappear" in str(e), \
                    f"it stopped, but not because of the shrink guard: {e}"
            else:
                raise AssertionError(
                    "would_shrink was True and it went straight through —— **the caller does "
                    "not go through the guard.** vault documents are deleted and committed.")
    finally:
        _g.update(_keep)
        _sys.argv = _argv

    #  ⚠ An **openwiki bundle** must be refused, and a plain vault must not be.  A guard that
    #     refused both would be removed the first time it fired on a real vault.
    import tempfile as _tf
    import shutil as _sh
    _b = _tf.mkdtemp(); open(os.path.join(_b, ".page-manifest.json"), "w").write("{}")
    assert is_openwiki_bundle(_b), "a manifest did not identify a bundle"

    #  ⚠ Ownership is decided by the **frontmatter**, never the body.  A hand-written note *about*
    #     how session documents are marked —— one that merely contains the words in a sentence ——
    #     was classified as ours and reached `os.remove`, while `foreign()` (which excludes what
    #     `owned()` claims) stayed silent about it.  The fixture used to be a note that never
    #     mentions the marker, so nothing distinguished body from frontmatter.  (2026-09-04)
    _od = _tf.mkdtemp()
    try:
        open(os.path.join(_od, "real.md"), "w", encoding="utf-8").write(
            "---\ntitle: x\n" + OWN_MARK + "\n---\n본문\n")
        open(os.path.join(_od, "about.md"), "w", encoding="utf-8").write(
            "---\ntitle: how session docs are marked\n---\n\n"
            "Session documents carry `" + OWN_MARK + "` in their frontmatter.\n")
        _own = sorted(os.path.basename(x) for x in owned(_od))
        assert _own == ["real.md"], f"ownership read the body: {_own}"
        #     …and inside the frontmatter, only a **top-level** key counts.  Restricting the
        #     search to the frontmatter was not enough: a nested key and a block-scalar body both
        #     still matched and reached os.remove.  (2026-09-04)
        open(os.path.join(_od, "nested.md"), "w", encoding="utf-8").write(
            "---\ntitle: n\nmeta:\n  " + OWN_MARK + "\n---\n본문\n")
        open(os.path.join(_od, "scalar.md"), "w", encoding="utf-8").write(
            "---\ntitle: s\ndescription: |\n  " + OWN_MARK + "\n  is the marker\n---\n본문\n")
        _own2 = sorted(os.path.basename(x) for x in owned(_od))
        assert _own2 == ["real.md"], f"a nested key or block scalar was called ours: {_own2}"
    finally:
        _sh.rmtree(_od, ignore_errors=True)
    _b2 = _tf.mkdtemp(); open(os.path.join(_b2, "index.md"), "w").write('okf_version: "0.2"\n')
    assert is_openwiki_bundle(_b2), "a root index with okf_version did not identify a bundle"
    _v = _tf.mkdtemp(); os.makedirs(os.path.join(_v, "wiki"), exist_ok=True)
    open(os.path.join(_v, "index.md"), "w").write("# my vault\n")
    assert not is_openwiki_bundle(_v), "a plain Obsidian vault was called a bundle"
    assert not is_openwiki_bundle(_tf.mkdtemp()), "an empty folder was called a bundle"
    import shutil as _sh
    for _d in (_b, _b2, _v):
        _sh.rmtree(_d, ignore_errors=True)
    print("  ✅ promote_distilled —— shrink guard · wiring · commit scope (other people's staged changes untouched)"
          " · refuses an openwiki bundle, accepts a vault")


def _main_promote():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--no-commit", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="proceed even when the vault's document count drops sharply")
    a = ap.parse_args()
    if a.selftest:
        _selftest(); return

    files = sorted(glob.glob(os.path.join(SRC, "*.md")))
    if not files:
        #  **Having nothing to do is not a failure.**  This used to exit 1, so on a fresh
        #  machine (distillation not yet run) `rebuild_all.sh:50` died wholesale under
        #  `set -e`, and `just selftest-py` stopped here so every self-check after it never
        #  ran (2026-08-25, round 4).  justfile:261 records having already hit the same
        #  defect in the web UI, and this path had survived.
        #  ⚠ It does not go quiet —— the guidance still prints, and it says **what to do
        #    next**.  The signal is this sentence, not the exit code.
        print(f"no distilled documents: {SRC}")
        print("  If distillation has not been run yet:  just run distill")
        return
    #  ⚠ **The vault may now be an openwiki bundle, and this step must not touch one.**
    #     `just openwiki-adopt` points the vault setting at the bundle so every surface reads
    #     what was indexed —— and from that moment the web UI's "Promote to vault" button (this
    #     script, step 2 in src/status.py) writes 1,017 documents in the **old brain-ingest
    #     format** into `<bundle>/raw/conversations/sessions/`: outside `personal/`, never
    #     converted to OKF, and then indexed as duplicates of the pages already there.
    #     Reproduced 2026-09-02 by clicking through the running UI —— one click, no confirmation.
    #     The bundle path is `openwiki_emit.py`; this script is the older one it replaced.
    if is_openwiki_bundle(VAULT):
        raise SystemExit(
            f"  ❌ the vault is an openwiki bundle: {VAULT}\n"
            f"     This step writes the older brain-ingest layout, which the bundle does not use.\n"
            f"     Use the bundle path instead:  just openwiki-sessions\n"
            f"     (to promote into an Obsidian vault again:  just vault <that vault>)")
    metas = [fm(f) for f in files]
    today = datetime.date.today().isoformat()
    sess = {m.get("session_id") for m in metas}
    print(f"{len(files)} distilled documents ({len(sess)} sessions) → {os.path.relpath(DEST, VAULT)}")
    if a.dry_run:
        # The user asked for --dry-run, so this is what was asked for.  Not a failure.
        print("(dry-run)")
        raise SystemExit(0)

    # DEST is emptied and refilled —— which is why duplicates cannot arise structurally.
    # But if SRC is **partially** empty, that much disappears from the vault.  The empty case
    # is blocked above (no files → exit), yet 5 remaining means deleting 280 and inserting 5.
    # This script then goes on to git commit, so it passes quietly.
    #  ⚠ **Check first that it can be undone.**  The deletion below is recoverable only by
    #     `git revert`.  Outside a git repository it used to delete first and then die on
    #     `git add` with 128 —— the files already gone and no history either.  The user sees
    #     a red "failed" and a Python traceback (reproduced 2026-08-28: a hand-written `mine.md` lost for good).
    if not vault_is_git(VAULT):
        raise SystemExit(
            f"  ❌ the vault is not a git repository: {VAULT}\n"
            f"     This step deletes documents inside the vault and rewrites them.  The only\n"
            f"     way back is `git revert`, so with no history there is no way to recover.\n"
            f"       git -C {VAULT} init && git -C {VAULT} add -A && git -C {VAULT} commit -m init")

    #  ⚠ **Other people's files are left alone.**  It used to delete the whole folder with
    #     `shutil.rmtree(DEST)`.  Measured (2026-08-28): a folder with 200 attachments and
    #     hand-written notes went from **201 files to 3**.  The only guard, `would_shrink`,
    #     counts top-level `*.md` alone and therefore **sees neither attachments nor
    #     subfolders** —— in that run it never fired once.
    #     Now only what this script wrote (`OWN_MARK`) is deleted.  The original purpose,
    #     clearing slug leftovers, still holds —— leftovers are this script's output too.
    mine = owned(DEST) if os.path.isdir(DEST) else []
    others = foreign(DEST)
    existing = len(mine)
    if would_shrink(len(files), existing) and not a.force:
        raise SystemExit(
            f"there are {len(files)} distilled documents but {existing} in the vault —— "
            f"{existing - len(files)} would disappear.\n"
            f"  Check that ~/.kal/distilled/ is intact.  If this is intended, pass --force")
    if others:
        print(f"  ⓘ leaving {len(others)} file(s) that belong to someone else "
              f"(for example: {os.path.relpath(others[0], VAULT)})")
    for f in mine:
        os.remove(f)
    os.makedirs(DEST, exist_ok=True)
    os.makedirs(os.path.dirname(PAGE), exist_ok=True)
    for f in files:
        shutil.copy2(f, os.path.join(DEST, os.path.basename(f)))
    print(f"  ✅ Step 5  placed {len(files)} file(s) under raw/")

    open(PAGE, "w", encoding="utf-8").write(corpus_page(metas, today))
    print(f"  ✅ Step 4  wiki/sources/claude-session-corpus.md  (one corpus page)")

    line = (f"- [[claude-session-corpus]] — {len(files)} documents distilled from "
            f"{len(sess)} Claude Code sessions.  Decisions and rationale\n")
    idx = open(INDEX, encoding="utf-8").read() if os.path.exists(INDEX) else "# Index\n"
    if "claude-session-corpus" not in idx:
        open(INDEX, "a", encoding="utf-8").write("\n" + line)
    print(f"  ✅ Step 7  registered in wiki/index.md")

    # Idempotent — a re-run replaces the same block rather than appending another.
    # (As distillation grows, promote gets run repeatedly)
    block = (f"## [{today}] ingest | Claude Code session corpus\n\n"
             f"- {len(sess)} sessions → {len(files)} distilled documents "
             f"(`raw/conversations/sessions/`)\n"
             f"- page created: [[claude-session-corpus]]\n"
             f"- INGEST steps 3·5·6·9 skipped for scale — "
             f"rationale in the header comment of `promote_distilled.py`\n")
    txt = open(LOG, encoding="utf-8").read() if os.path.exists(LOG) else ""
    #  ⚠ The old Korean heading is still matched.  This regex is what makes the step
    #     idempotent, and a vault written before 2026-09-01 holds blocks titled
    #     the Korean equivalent of "Claude Code session corpus".  Matching only the new English
    #     title would leave the old
    #     block in place and append beside it —— the log would grow a duplicate on every run,
    #     which is exactly what the idempotency exists to prevent.  Drop the alternative once
    #     no vault can still hold the old title.
    pat = re.compile(r"## \[[\d-]+\] ingest \| Claude Code "
                     r"(?:session corpus|\uc138\uc158 \ucf54\ud37c\uc2a4)\n.*?(?=\n## |\Z)", re.S)
    txt = pat.sub("", txt).rstrip() + "\n\n" + block
    open(LOG, "w", encoding="utf-8").write(txt)
    print(f"  ✅ Step 8  recorded in wiki/log.md (idempotent)")
    print(f"  △ Step 2·4     abridged (LLM proposals + one corpus page)")
    print(f"  ❌ Step 3·5·6·9 not done (scale) — see the promote_distilled.py comments")

    if not a.no_commit:
        msg = (f"ingest: Claude Code session corpus — the decisions and rationale of "
               f"{len(sess)} conversations as {len(files)} searchable documents\n\n"
               f"Source: raw/conversations/sessions/ ({len(files)} files)\n"
               f"Pages created: 1 (wiki/sources/claude-session-corpus.md)\n"
               f"Pages updated: 2 (wiki/index.md, wiki/log.md)\n\n"
               f"Distilled documents holding conclusions, decisions and rationale rather than transcripts.  Secrets are masked before indexing.\n"
               f"INGEST steps 3·5·6·9 skipped — a compromise for scale, recorded in the promote_distilled.py comments.\n\n"
               f"Co-Authored-By: Claude <noreply@anthropic.com>")
        # ⚠ It used to stage **the whole vault** with `git add -A`.  Whatever note edit was in
        #   progress got swept into this commit, and the undo it suggested
        #   (`reset --hard HEAD~1`) deleted that too.  Only the paths this script writes are staged.
        #   (adversarial review 2026-08-18, BLOCKER)
        paths = sorted({os.path.relpath(DEST, VAULT),
                        "wiki/sources/claude-session-corpus.md",
                        "wiki/index.md", "wiki/log.md"})
        subprocess.run(["git", "-C", VAULT, "add", "--"] + paths, check=True)
        #  ⚠ `--` plus paths must be here **as well**.  The 2026-08-18 review narrowed only
        #    `add`, while this query and the `commit` below saw the **whole** index.  So
        #    unrelated pre-staged changes were swept into this commit and "N files (only what
        #    this script wrote)" became a lie.
        #
        #    Measured (2026-08-21): the vault had 2 renames and 2 `viewer/*.html` deletions
        #    staged.  Following the suggested `git revert` undoes those deletions and
        #    **resurrects a galaxy.html holding 292 real names.**
        staged = subprocess.run(["git", "-C", VAULT, "diff", "--cached", "--name-only",
                                 "--"] + paths,
                                capture_output=True, text=True).stdout.split()
        #  ⚠ Captured **before the commit**.  Afterwards the index is empty and everything
        #     reads 0 (2026-08-28: noticed after it printed "0 added or modified").
        _st = subprocess.run(
            ["git", "-C", VAULT, "diff", "--cached", "--name-status", "--"] + paths,
            capture_output=True, text=True).stdout.splitlines()
        if not staged:
            print("  · no changes — commit skipped")
        else:
            # The identity is **stated explicitly**.  A container has no git config and it
            # died with "Author identity unknown" (pipeline check, 2026-08-19).
            ident = ["-c", "user.name=%s" % os.environ.get("GIT_AUTHOR_NAME", "kallimachos pipeline"),
                     "-c", "user.email=%s" % os.environ.get("GIT_AUTHOR_EMAIL", "pipeline@kallimachos.local")]
            # A failed commit **does not fail the whole step.**  The files are already
            # written and the commit is a convenience.  This used to exit 1, so the UI showed
            # "failed" even though every piece of work had succeeded.
            c = subprocess.run(["git", "-C", VAULT] + ident + ["commit", "-m", msg,
                                                                "--"] + paths,
                               capture_output=True, text=True)
            if c.returncode != 0:
                print("  ⚠ commit failed — the files are already in place.  Please commit by hand.")
                print("    " + (c.stderr or c.stdout).strip().splitlines()[-1][:160])
            else:
                sha = subprocess.run(["git", "-C", VAULT, "rev-parse", "--short", "HEAD"],
                                     capture_output=True, text=True).stdout.strip()
                #  ⚠ **Deletions and additions are counted separately.**  They used to be
                #     printed together as "N files (only what this script wrote)", and that N
                #     included other people's files this script had **deleted** (reproduced
                #     2026-08-28: 2 of 5 were someone else's deletions).  It no longer deletes
                #     other people's files, but the meaning of the number stays visible on screen.
                _add = sum(1 for l in _st if l[:1] in ("A", "M"))
                _del = sum(1 for l in _st if l[:1] == "D")
                _bits = [f"{_add} added or modified"] + ([f"{_del} deleted"] if _del else [])
                print(f"  ✅ commit {sha} — {' · '.join(_bits)}"
                      f"  (all of them documents this script wrote)")
                # reset --hard is deliberately not suggested —— it destroys unstaged work too.
                print(f"\nUndo:  git -C {VAULT} revert --no-edit {sha}")
                print(f"       (other edits in progress are left untouched)")


if __name__ == "__main__":
    # Record the run under ~/.kal/runs/ —— the web screen's "last run" only knew about runs
    # started from the web UI, so a CLI success still showed yesterday's failure as the last.
    from run_log import record
    with record("promote"):
        _main_promote()
