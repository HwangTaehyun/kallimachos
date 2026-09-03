#!/usr/bin/env python3
"""Check the numbers written in the docs against the real DB and files.

Why it is needed
  Numbers copied by hand go wrong.  They went wrong three times —— disk usage written as
  2.5GB (measured 499MB), row counts not updated after a rebuild, and a type distribution
  filtered by degree>=2 recorded as the total.  Nobody can eyeball that every time, so a machine does.

  report_stats.py is the tool that "extracts the current state"; this is the tool that
  "checks whether the docs match that state".

Usage:
  python verify_docs.py            # exit 1 on any mismatch
"""
import os, re, sys, glob, argparse, subprocess, collections
import lancedb


# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
DB = os.environ.get("KAL_PATH", os.path.join(KAL_HOME, "db"))
#  ⚠ **The repository root is measured from `__file__`.**  It used to be a hardcoded absolute
#     path, and when a move (super-brain/second-brain → kal-workspace/kal) made that path
#     vanish, `glob` quietly returned an empty list: this checker walked **0 repository
#     documents** and printed "✅ everything matches".  CI's "doc number verification" was green too.
#     (deep review 2026-08-25: fact-checker and consistency found it independently)
#     Hardcode a path and it dies the moment things move, and the death is invisible.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOTS = [
    _REPO,
    #  ⚠ The path above is **an older installed copy (the CLI edition)**.  The canonical
    #    `skills/kal-recall/` this repository ships is **already** covered by ROOTS[0] (the
    #    repository root) globbing `**/*.md` —— it was once added here as a separate root, and
    #    then that file was scanned twice, doubling both the errors and the counts.  Worse,
    #    only this path was computed relatively, so it pointed at a different tree inside a
    #    worktree or a container.  (r4-fresh, round 5)
]
# Paragraphs recording history state the facts of their time, so they are excluded (PIPELINE's 'before → after' tables and the like)
SKIP_LINE = re.compile(r"→\s*[\d,]+\s|\bbefore\b|\bv1\b|pre-tuning|used to be")
# This marker near the top of a document means "a snapshot of that moment", and it is not checked
SNAPSHOT_MARK = "<!-- snapshot-doc -->"
# On a line carrying this marker, "entities N · relations N" is a declaration of **the current
# DB totals**.  Without it, nothing is checked —— many lines use the same words for other populations.
DBCOUNT_MARK = "<!-- db-count -->"

# ── Code constants ↔ documented numbers ─────────────────────────────────
# Why it is needed: the row-count check (the tbl loop below) only fires when a table name sits
# beside the number.  Constants appear in the docs as bare values, slipped through the net, and three had gone stale.
#
# How: **only lines where the document names the constant** are checked.
#   `8+ fragments        the LLM rewrites them     ← FORCE_LLM_SUMMARY_ON_MERGE = 8`
#                                                    ^^^^^^^^^^^^^^^^^^^^^^^^ this is what arms the check
#
# Guessing the context from the value alone produces false positives — written that way once, it
# compared "500-char chunks" (storage) against CHUNK_CHARS (extraction, 2400) and insisted it was
# wrong, and flagged the example flag `--top 30` as a violation of ANSWER_N (20).  A checker that
# cries wolf is worth nothing.  So it was inverted into **explicit opt-in**: name the constant beside the number you want verified.
CONSTS = [
    ("CHUNK_CHARS",                "lr_extract.py",       r"^CHUNK_CHARS\s*=\s*([\d.]+)"),
    ("FORCE_LLM_SUMMARY_ON_MERGE", "lr_extract.py",       r"^FORCE_LLM_SUMMARY_ON_MERGE\s*=\s*([\d.]+)"),
    ("SUMMARY_MAX_CHARS",          "lr_extract.py",       r"^SUMMARY_MAX_CHARS\s*=\s*([\d.]+)"),
    ("MIN_COMM",                   "export_kal_graph.py", r"^MIN_COMM\s*=\s*([\d.]+)"),
    ("TOP_COMM",                   "export_kal_graph.py", r"^TOP_COMM\s*=\s*([\d.]+)"),
    ("WARN_RATIO",                 "refresh_kg.py",       r"^WARN_RATIO\s*=\s*([\d.]+)"),
    ("ANSWER_N",                   "kal_search.py",       r"^ANSWER_N\s*=\s*([\d.]+)"),
    ("HOP",                        "kal_search.py",       r"^HOP\s*=\s*([\d.]+)"),
]


def const_truth(src_dir):
    """Pull a constant's real value from the source.  Anything not found drops out quietly."""
    out = {}
    for name, fn, code_re in CONSTS:
        try:
            body = open(os.path.join(src_dir, fn), encoding="utf-8").read()
        except OSError:
            continue
        m = re.search(code_re, body, re.M)
        if m:
            out[name] = m.group(1)
    return out


def check_consts(line, truth):
    """On a line naming a constant, compare `name = value`, or a number within 24 chars after `name`."""
    out = []
    for name, want in truth.items():
        if name not in line:
            continue
        m = re.search(rf"{re.escape(name)}\s*=\s*(\d[\d.]*)", line)
        if not m:
            m = re.search(rf"{re.escape(name)}\D{{0,24}}?(\d[\d.]*)", line)
        if not m:
            continue
        got = m.group(1).rstrip(".")
        if float(got) != float(want):
            out.append((f"const:{name}", got, want))
    return out


def selftest():
    """Checks only the constant-comparison traps —— it runs without touching the DB.

    It died once: when a document mentioned a constant **with no number**, as in
    `refresh_kg.WARN_RATIO`, the `[\\d.]+` grabbed the sentence's final full stop and blew up
    in float('').  One line stopped the whole of verify.  (2026-08-19)
    """
    T = {"WARN_RATIO": 0.20, "CHUNK_CHARS": 2400}
    assert check_consts("see the `refresh_kg.WARN_RATIO` comment.", T) == [], "a mention with no number passes"
    assert check_consts("see WARN_RATIO (no value here)", T) == [], "parentheses alone still pass"
    bad = check_consts("split at CHUNK_CHARS = 500", T)
    assert bad and bad[0][1] == "500", f"a genuine mismatch must be caught: {bad}"
    assert check_consts("split at CHUNK_CHARS = 2400", T) == [], "a match must stay quiet"

    # ── Type vocabulary —— without this check, 6 new types slipped out silently ──────
    import collections, schema_v3
    C = collections.Counter({t: 111 for t in schema_v3.CANON_TYPES})
    #  ★ The point: **walk the whole canonical set** and confirm none is missing.  Revert the
    #    vocabulary to a hardcoded list (= write only part of it) and this assertion breaks on the missing type.
    for t in schema_v3.CANON_TYPES:
        got = check_types(f"{t} 999", C)
        assert got and got[0][0] == f"type:{t}", f"canonical type {t} dropped out of the check"
        assert check_types(f"{t} 111", C) == [], f"{t} matches and was still flagged"
    #  A line with no live type at all is left alone —— it prevents false positives on prose.
    #  What the guard actually blocks is **a retired name appearing alone in prose**.
    #  (It was first tested with `library 1,234`, which is caught with or without the guard ——
    #   it is neither RETIRED nor live.  A test that guarded nothing.)
    assert check_types("the same pattern repeats 1,139 times", C) == [], "prose must not be flagged"
    #  A retired name **on the same line** is caught, but must not be a target for auto-fixing
    r = check_types("concept 111 · pattern 1,139", C)
    assert any(w == "type-retired:pattern" for w, _, _ in r), f"a retired type was missed: {r}"
    assert not any(w == "type:pattern" for w, _, _ in r), "it tries to fix a retired type by number"

    # ── The canonical list line (ERD entity_types_canonical) ──────────────
    want = "|".join(sorted(schema_v3.CANON_TYPES) + ["other"])
    assert check_vocab(f"   entity_types_canonical   {want}") == [], "a match was flagged"
    stale = check_vocab("   entity_types_canonical   concept|event|method|other")
    assert stale and stale[0][2] == want, f"a stale list was missed: {stale}"
    assert check_vocab("entity_types_canonical is defined in the ERD") == [], "a mention with no value passes"

    # ── The table-name scan must not read the *next* population's number ──────────
    #  ⚠ `| index | 1,115 documents · 9,304 chunks · 6,132,241 postings |` reads forward from
    #     `chunks`, finds postings' number and calls chunks wrong —— and `--fix` would then have
    #     written the live chunk count over a **dated timing record**.  The look-ahead list held
    #     only the four table names; `postings`, `terms` and `doclen` are counted the same way.
    #     This is the false-positive shape the note in `check()` describes, in the guard written
    #     to prevent it.  (reproduced 2026-09-04)
    import tempfile as _tf, os as _os
    _T = {"documents": 1116, "chunks": 9329, "lr_entities": 25372, "lr_relations": 35158,
          "ix_terms": 275957, "ix_postings": 6146494, "ix_doclen": 9329, "_types": {}}
    _d = _tf.mkdtemp()
    _f = _os.path.join(_d, "t.md")
    open(_f, "w", encoding="utf-8").write(
        "| index | 1,115 documents · 9,304 chunks · 6,132,241 postings · 108 s |\n")
    _hits = [b[1] for b in check(_f, _T)]
    assert "chunks" not in _hits, f"a trailing population's number was read as chunks: {_hits}"
    #     …and the check still fires when the number really is the table's own.
    open(_f, "w", encoding="utf-8").write("chunks 3,370 rows\n")
    assert "chunks" in [b[1] for b in check(_f, _T)], "a genuinely stale count stopped being caught"
    import shutil as _sh2; _sh2.rmtree(_d, ignore_errors=True)

    print("  ✅ verify_docs constants · type vocabulary · canonical list · table-name look-ahead")


def truth():
    """The DB's real values right now.  The docs are compared against these."""
    #  ⚠ Check that it is **ready** before opening it.  Otherwise a fresh install sees
    #     `ValueError: Table 'lr_entities' was not found` (round 6).
    import db_ready
    db_ready.require(DB, ["documents", "lr_entities", "chunks"], "doc number verification")
    db = lancedb.connect(DB)
    t = {n: db.open_table(n).count_rows() for n in db.list_tables().tables}
    E = db.open_table("lr_entities").search().limit(999999).to_list()
    t["_types"] = collections.Counter(e["type"] for e in E)
    #  ⚠ The default of 0 is required.  After only `just run index` without `extract` —— a
    #     state the README explicitly allows —— `lr_entities` is **empty**.  This used to die
    #     with `ValueError: max() iterable argument is empty`, so doc verification broke
    #     depending on how far along the pipeline was.
    #     (deep review 2026-08-25, newcomer lens)
    t["_maxdeg"] = max((e["degree"] for e in E), default=0)
    out = subprocess.run(["du", "-sm", os.path.join(KAL_HOME, "db")],
                         capture_output=True, text=True).stdout.split()
    t["_disk_mb"] = int(out[0]) if out else 0
    return t


# The canonical type vocabulary.  **The code is the truth** and the docs follow —— not the reverse.
# This list used to be a regex baked into `check()`.  So when the type system moved to 10 kinds,
# the 6 new types (actor · artifact · decision · failure · metric · project) **dropped out of the
# check silently**, while the 3 dead names (pattern · person · organization) kept being checked
# and `--fix` nearly rewrote the docs to say "pattern 0".  (2026-08-21)
def live_types():
    import schema_v3
    return set(schema_v3.CANON_TYPES) | {"other"}      # other = where norm_type lands


# Names that were canonical once and have retired.  When one of these appears **on the same line
# as a live type** carrying a number, that line describes the old scheme —— fixing the number is
# not enough, the sentence has to be rewritten, so it goes to a person rather than the auto-fixer.
#
# This is a one-way list: **add** to it when a type leaves the canonical set.  Never delete.
# (The 51 variant names the LLM invents do not go here —— `library`, `service`, `system` and the
#  like are ordinary English words and would fire on prose.  No crying wolf.)
RETIRED = ("pattern", "person", "organization")


def check_types(line, counts):
    """Compare a type-distribution line.  A line counts as a type distribution only if it holds
    at least one **live type plus a number** —— otherwise it catches things like `method 3` in ordinary prose."""
    live = live_types()
    hits = re.findall(r"\b([a-z_]{3,14})\s+([\d,]{3,})", line)
    if not any(n in live for n, _ in hits):
        return []
    out = []
    for n, c in hits:
        if n in live:
            got = int(c.replace(",", ""))
            if got != counts[n]:
                out.append((f"type:{n}", got, counts[n]))
        elif n in RETIRED:
            out.append((f"type-retired:{n}", c, "a type no longer canonical —— rewrite the line"))
    return out


VOCAB_RX = re.compile(r"entity_types_canonical\s+([a-z_|]{10,})")


def check_vocab(line):
    """Does the ERD's `entity_types_canonical` line match the code's canonical list.

    That line defines what the DB's `type` column may hold.  When it drifts, code written by
    trusting the schema doc is wrong.  With no check it really did go stale at 8 kinds (2026-08-21).
    """
    m = VOCAB_RX.search(line)
    if not m:
        return []
    import schema_v3
    want = "|".join(sorted(schema_v3.CANON_TYPES) + ["other"])
    return [] if m.group(1) == want else [("vocab", m.group(1), want)]


def check(path, T):
    bad = []
    head = open(path, encoding="utf-8").read(400)
    if SNAPSHOT_MARK in head:
        return bad
    for i, line in enumerate(open(path, encoding="utf-8"), 1):
        if SKIP_LINE.search(line):
            continue
        for tbl in ("documents", "chunks", "lr_entities", "lr_relations",
                    "ix_terms", "ix_postings", "ix_doclen"):
            # Only numbers within 24 chars after a table name.  Numbers inside parentheses, as in ngram(2,3), are excluded
            #
            #  ⚠ **A number followed by another table's name belongs to that one.**  The prose
            #     shape `97 documents / 1,577 chunks` puts each count *before* its word, so
            #     reading forward from `documents` picks up 1,577 —— chunks' number —— and
            #     reports it as documents' count being wrong.  Measured 2026-09-01: the moment
            #     TEMPORAL_DESIGN's harness table went from `97문서 / 1,577청크` to English, one
            #     line produced 3 findings, all false.  Korean hid this because 문서/청크 are not
            #     the table names.  A checker that cries wolf gets switched off, so look ahead
            #     and drop the match when the number is the next word's.
            for m in re.finditer(rf"\b{tbl}\b(?![.\w])[^\n\d(]{{0,24}}?([\d,]{{3,}})", line):
                #     Up to two words may sit between —— "278 session documents", "1,577 raw chunks".
                #     The cost is real and accepted: `documents 999 chunks` is skipped, because
                #     under this rule the 999 is read as chunks'.  A number sandwiched between two
                #     table names is ambiguous to a machine either way, and a false alarm is
                #     worse here than a miss —— a missed one still has the other table's check.
                #  ⚠ **The look-ahead list has to hold every counted population, not just the
                #     four table names.**  `| index | 1,115 documents · 9,304 chunks · 6,132,241
                #     postings |` reads forward from `chunks`, finds postings' number, and reports
                #     chunks as wrong —— and `--fix` would have written 9,329 over a **dated timing
                #     record**.  `postings`, `terms` and `doclen` are counted the same way and were
                #     missing.  This is the false-positive shape the note above describes, in the
                #     very guard written to prevent it.  (reproduced 2026-09-04)
                if re.match(r"\s*(?:\w+\s+){0,2}\b(documents|chunks|lr_entities|lr_relations"
                            r"|entities|relations|postings|terms|doclen|ix_postings|ix_terms"
                            r"|ix_doclen)\b", line[m.end():]):
                    continue
                n = int(m.group(1).replace(",", ""))
                if n != T[tbl]:
                    bad.append((i, tbl, n, T[tbl], line.strip()[:70]))
        # Code constants — only lines that name them (opt-in)
        for what, got, want in check_consts(line, CONSTS_TRUTH):
            bad.append((i, what, got, want, line.strip()[:70]))
        # Prose form —— "entities 8,121 · relations 12,228" has no table name, so the tbl loop
        # above cannot see it.  The mirror README had gone stale in exactly this shape.
        #
        # But a word like "entities" refers to **several populations** across the docs ——
        # the extraction cache (8,406), cluster coverage (6,386) and pre-merge history (8,121)
        # are all legitimate.  Guessing from the value alone flags all three (measured).  So the
        # same rule as constants applies: check **only when the line carries the `<!-- db-count -->` marker**.
        if DBCOUNT_MARK in line:
            marked = 0
            for word, tbl in (("entities", "lr_entities"), ("relations", "lr_relations"),
                              ("documents", "documents"), ("chunks", "chunks")):
                #  Both orders appear in prose: "relations 12,355" and "12,355 relations".
                #  Watching only one form missed "12,251 relations" in
                #  `skills/kal-recall/SKILL.md` —— the marker was there and it passed quietly
                #  (2026-08-21).  The check is **opt-in per line**, so widening the shapes adds no false positives.
                for rx in (rf"\b{word}\s+([\d,]{{3,}})",
                           rf"([\d,]{{3,}})\s+{word}"):
                    for m in re.finditer(rx, line):
                        marked += 1
                        n = int(m.group(1).replace(",", ""))
                        if n != T[tbl]:
                            bad.append((i, f"{word} (prose)", n, T[tbl], line.strip()[:70]))
            #  ⚠ **A marker that matches nothing guards nothing.**  The check is line-level, so
            #     reflowing a paragraph can leave the number on one line and its word on the
            #     next —— the marker still sits there and reads as coverage while checking 0
            #     values.  Measured 2026-09-01, right after the docs were rewritten in English:
            #     `skills/kal-recall/SKILL.md` had the marker on a line holding "12,355" while
            #     "relations" had wrapped to the following line.  This is the same shape as the
            #     defects this whole file exists to catch, so an empty marker is a finding.
            if not marked:
                bad.append((i, "db-count (marker matches nothing)", 0, 0, line.strip()[:70]))
        # Type distribution — the "concept 3,515" shape.  The vocabulary **comes from the code**.
        for what, got, want in check_types(line, T["_types"]):
            bad.append((i, what, got, want, line.strip()[:70]))
        # The canonical type list itself (the ERD's entity_types_canonical)
        for what, got, want in check_vocab(line):
            bad.append((i, what, got, want, line.strip()[:70]))
    return bad


def fix(path, bad):
    """Replace a mismatch with the real value.  Line by line, only the number that was caught."""
    lines = open(path, encoding="utf-8").read().split("\n")
    for ln, what, got, want, _ctx in bad:
        i = ln - 1
        if not (0 <= i < len(lines)):
            continue
        # ⚠ It used to chain `.replace(f"{got:,}", …).replace(str(got), …)`.  The second
        #   **matches inside what the first wrote** —— 376 → 3376 turns into "3,3376"
        #   (measured).  refresh_kg.py runs this unattended, so it corrupted docs quietly.
        #   Replace once, and only at a digit boundary.
        #   (adversarial review 2026-08-18)
        if str(what).startswith(("type-retired:", "db-count (")):
            continue                     # a number swap cannot fix it —— a person rewrites the line
        if what == "vocab":              # the whole list —— mechanical, so safe to fix
            lines[i] = lines[i].replace(got, want, 1)
            continue
        pats = [str(got)] if str(what).startswith("const:") else [f"{got:,}", str(got)]
        for pat in pats:
            rx = re.compile(rf"(?<![\d,]){re.escape(pat)}(?![\d,])")
            if rx.search(lines[i]):
                rep = str(want) if str(what).startswith("const:") else (
                    f"{want:,}" if "," in pat else str(want))
                lines[i] = rx.sub(rep, lines[i], count=1)
                break
    open(path, "w", encoding="utf-8").write("\n".join(lines))


CONSTS_TRUTH = {}

#  ⚠ It once matched only things starting with `\.{1,2}/` —— that is, links written `./` or `../`.
#     **A bare relative link like `[x](docs/STACK.md)` was never checked once**
#     (measured 2026-09-01: planting a broken link under docs/ still came out green).  Most
#     links in this repository, README included, are that shape, so the real coverage was tiny.
#     Everything is accepted now and `_is_local` below does the filtering.
LINK_RX = re.compile(r"\]\(([^)\s]+)")


def _is_local(t: str) -> bool:
    """Is this something to check —— only relative paths inside the repository."""
    if not t or t.startswith("#"):
        return False                       # an in-document anchor
    if t.startswith("/"):
        return False                       # an absolute path is not relative to this repository
    if "://" in t or t.startswith("mailto:"):
        return False                       # an external address
    if t.startswith("<"):
        return False                       # the `](<path>)` shape —— rare, and parsed differently
    return True


def _assert_roots():
    """Is there really anything to walk.  **If not, die loudly.**

    Walking 0 files and passing quietly is a failure this file actually suffered —— in that
    state, "there is no check" and "the check passed" look identical on screen.
    """
    live = [r for r in ROOTS if os.path.isdir(r)]
    if not live:
        raise SystemExit(f"❌ not a single folder to walk: {ROOTS}")
    n = sum(len(glob.glob(os.path.join(r, "**", "*.md"), recursive=True)) for r in live)
    if n == 0:
        raise SystemExit(f"❌ found no .md at all (has a path gone stale?): {live}")
    return live, n


def check_links(path):
    """Do relative links point at real files.  → [(line, link)]

    Why it exists —— copying docs into a mirror silently breaks links like `../src/x.py`.
    The 2026-08-18 review found 20 broken in the mirror alone, some of them broken *by* a fix
    to the original.  That is not a failure a person can see; a machine has to catch it.
    """
    d = os.path.dirname(os.path.abspath(path))
    out = []
    for i, line in enumerate(open(path, encoding="utf-8", errors="ignore"), 1):
        for m in LINK_RX.finditer(line):
            tgt = m.group(1)
            if not _is_local(tgt):
                continue
            #  For `path#anchor` only the file part is checked —— the anchor is not verified.
            f = tgt.split("#", 1)[0]
            if not f:
                continue
            if not os.path.exists(os.path.normpath(os.path.join(d, f))):
                out.append((i, tgt))
    return out


#  ── Retired check: the README folder list ↔ the real folders ────────────────
#
#  Rewriting the README in English (2026-09-01) **deliberately removed** the folder-tree block
#  —— the Docs table and the flow diagram take its place.  The README no longer promises a
#  folder list, so **there is nothing left to disagree with.**
#
#  ⚠ Do not re-point this at the flow diagram.  That is a pipeline flow, not a folder list,
#     and it is not the kind of thing you compare against "does this folder exist in a clone".
#
#  Why it existed (the rationale is here should it ever need reviving): the list named
#  `viewer/`, which is `.gitignore`d and absent from a clone, while `api/`, `web/`, `skills/`
#  and `ops/` —— which really do ship —— were missing (deep review 2026-08-25).
#  Putting a folder list back into the README means reviving this check with it.


def check_symbol_citations():
    """When a `file.py:123` citation names a symbol, **is it really near that line**.

    Line numbers slide with every edit.  A check that passes as long as the number is in range
    is not enough —— all five stale citations round 5 found by hand were **inside the range**
    (`schema_v3.py:405 scan_vault()` → really 528).

    So when a backticked symbol sits beside the citation (`scan_vault()` · `blocked_path()` ·
    an uppercase constant), it checks whether that name appears within ±4 lines.  A citation
    with no symbol beside it gives nothing to check against, so it is left alone.
    → (list of problems).  An empty list means healthy.
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    #  A `bar()` or `BAZ` within 60 chars either side of `src/foo.py:123`
    #  ⚠ Range citations (`file.py:12-34`) exist too.  The start line is used —— rewriting a
    #     whole range once produced `125-136` → `294-136` (start > end).  (2026-08-25)
    cite = re.compile(r"`?(?:src/)?([a-z_0-9]+\.py):(\d+)(?:-(\d+))?`?")
    #  Two shapes are read: a backticked `foo()`, and the `file.py:123  foo()` used in code blocks.
    #  Leaving the latter out missed `scan_vault()` at `README.md:236` (2026-08-25).
    sym = re.compile(r"`([A-Za-z_][A-Za-z_0-9]*(?:\(\))?)`"
                     r"|:\d+(?:-\d+)?\s+([A-Za-z_][A-Za-z_0-9]*\(\))")
    bad = []
    for md in sorted(glob.glob(os.path.join(repo, "*.md"))
                     + glob.glob(os.path.join(repo, "docs", "*.md"))):
        text = open(md, encoding="utf-8").read()
        for m in cite.finditer(text):
            fname, ln = m.group(1), int(m.group(2))
            src = os.path.join(repo, "src", fname)
            if not os.path.isfile(src):
                continue
            if m.group(3) and int(m.group(3)) < ln:
                bad.append(f"{os.path.basename(md)}: `{fname}:{ln}-{m.group(3)}` "
                           f"—— the start of the range is greater than its end")
                continue
            near = text[max(0, m.start() - 60): m.end() + 60]
            names = [(a or b).rstrip("()") for a, b in sym.findall(near)
                     if (a or b).rstrip("()") != fname.replace(".py", "")]
            if not names:
                continue                    # no symbol named —— nothing to check against
            lines = open(src, encoding="utf-8").read().splitlines()
            #  ⚠ For a range citation, **the whole range** is read.  Reading only the start
            #     line ±4 missed `NO_LLM` at 294 inside `289-297` (2026-08-25).
            end = int(m.group(3)) if m.group(3) else ln
            window = "\n".join(lines[max(0, ln - 5): end + 4])
            #  ⚠ Judged only by symbols **defined in the cited file**.  A sentence often
            #     mentions another file's symbol **by contrast** —— the ERD's "this is
            #     `SKIP`, not no_llm" refers to `schema_v3.py` while the citation is
            #     `lr_extract.py`.
            #     Going red on that is crying wolf (measured 2026-08-25).
            #  ⚠ Judged only by symbols **defined** in that file.  It span twice:
            #     ① `SKIP` matched as a substring inside `NO_LLM_SKIPPED`
            #        → fixed with word boundaries.
            #     ② even then `SKIP` appeared in `lr_extract.py`'s **comment prose** and
            #        matched.  Documents commonly name another file's symbol **by
            #        contrast** ("this is `SKIP`, not no_llm").
            #     Only names with a definition (`def X` · `X = `) count.  (2026-08-25)
            whole = "\n".join(lines)
            def _defined(n):
                return re.search(
                    rf"^\s*(?:def|class)\s+{re.escape(n)}\b|^\s*{re.escape(n)}\s*(?::[^=]*)?=",
                    whole, re.M)
            names = [n for n in names if _defined(n)]
            if not names:
                continue
            if not any(re.search(rf"\b{re.escape(n)}\b", window) for n in names):
                bad.append(f"{os.path.basename(md)}: `{fname}:{ln}` has no "
                           f"{names[:2]} nearby")
    return bad


def _main_verify():
    #  ⚠ Without `global` this is **a local variable**, and the global check() reads stays an
    #     empty dict —— it would print "8 constants checked" while looking at 0.
    global CONSTS_TRUTH
    CONSTS_TRUTH = const_truth(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true",
                    help="replace mismatches with the real values automatically (so numbers need not be chased by hand)")
    ap.add_argument("--links-only", action="store_true",
                    help="check links only (no knowledge DB —— for the CI runner)")
    ap.add_argument("--selftest", action="store_true",
                    help="check only the constant-comparison logic (no DB connection)")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        raise SystemExit(0)
    #  ⚠ **Called here.**  Merely defining it makes the guard decorative —— the defect this
    #     file exists to fix was exactly "a check that quietly walks 0 files".
    live, n_md = _assert_roots()
    print(f"walking {len(live)} root(s) · {n_md} .md file(s)")
    if a.links_only:
        #  The DB is not opened —— a CI runner has no knowledge DB.
        #  ⚠ **Checks that need no DB run here too.**  Putting the folder-list and symbol
        #     citation checks after `truth()` meant they never ran in CI (always
        #     `--links-only`) —— a check built and then guarded by nobody
        #     (2026-08-25, round 6).  Measured: making a citation stale still left
        #     `verify-links` green.
        #  ⚠ Checks that are not about links also run here.  But every result was being
        #     counted as "N broken links" —— one folder-check failure was reported as "1 broken
        #     link" and sent people hunting for a link that did not exist (measured 2026-09-01).  Counted by kind now.
        other = check_symbol_citations()
        for _m in other:
            print(f"❌ {_m}")
        bad = 0
        for root in ROOTS:
            for p_ in sorted(glob.glob(f"{root}/**/*.md", recursive=True)):
                if "/node_modules/" in p_ or "/plugin/" in p_:
                    continue
                for ln, tgt in check_links(p_):
                    print(f"❌ {p_}\n     L{ln}  broken link  {tgt}")
                    bad += 1
        if not bad and not other:
            print("✅ every link resolves · symbol citations match reality too")
        else:
            parts = ([f"{bad} broken link(s)"] if bad else []) + \
                    ([f"{len(other)} mismatched symbol citation(s)"] if other else [])
            print(" · ".join(parts))
        raise SystemExit(1 if (bad or other) else 0)
    T = truth()
    print(f"measured  documents {T['documents']:,} · chunks {T['chunks']:,} · "
          f"lr_entities {T['lr_entities']:,} · lr_relations {T['lr_relations']:,}")
    print(f"      types {' · '.join(f'{k} {v:,}' for k, v in T['_types'].most_common())}")
    print(f"      max degree {T['_maxdeg']} · disk {T['_disk_mb']}MB")
    print(f"      {len(CONSTS_TRUTH)} constant(s) (checked only on lines that name them): "
          + " · ".join(f"{k}={v}" for k, v in CONSTS_TRUTH.items()) + "\n")

    #  ⚠ `_fold` used to be here, holding the README folder-list check's findings.  Retiring
    #     that check (see the block above `check_symbol_citations`) removed the function and
    #     left both references behind, so **every full run died with `NameError: _fold`** from
    #     f2b6a66 until 2026-09-01.  Nothing caught it because CI runs `--links-only`, which
    #     returns before this line —— the one path a machine exercises was the one path that
    #     could not reach the break.
    _cite = check_symbol_citations()
    for _m in _cite:
        print(f"❌ {_m}")

    total = len(_cite)
    #  ⚠ The tail below counts the findings `--fix` cannot repair, and it read a name (`num`)
    #     that has never existed in this scope —— `bad` is per-file and gone by then.  So every
    #     full run that found **anything** died with `NameError: num` right after printing the
    #     findings, hiding the summary line.  Same shape as the `_fold` break above, same
    #     reason it survived: `--links-only` returns first, and that is the path CI runs.
    #     (found 2026-09-01, by running the full path by hand)
    all_bad = []
    for root in ROOTS:
        for p in sorted(glob.glob(f"{root}/**/*.md", recursive=True)):
            if "/node_modules/" in p or "/plugin/" in p:
                continue
            bad = check(p, T)
            all_bad += bad
            links = check_links(p)
            if not bad and not links:
                continue
            print(f"❌ {p.replace(os.path.expanduser('~'), '~')}")
            for ln, what, got, want, ctx in bad:
                # A constant is a string ("0.20"); a row count is an integer.  One format cannot print both.
                g = f"{got:,}" if isinstance(got, int) else str(got)
                w = f"{want:,}" if isinstance(want, int) else str(want)
                print(f"     L{ln:<5}{what:<28}doc {g:>9} ≠ real {w:>9}   {ctx}")
            for ln, tgt in links:
                # --fix cannot repair this.  Where it should point is something a person knows.
                print(f"     L{ln:<5}{'broken link':<26}{tgt}")
            total += len(bad) + len(links)
            if a.fix and bad:
                fix(p, bad)
                print("     → fixed")
    if a.fix and total:
        print(f"\nreplaced {total}.  Please check again.")
        sys.exit(0)
    # --fix repairs numbers only.  For a broken link, a machine does not know where it should point.
    if total == 0:
        print("\n✅ everything matches")
    else:
        n_link = sum(len(check_links(p)) for root in ROOTS
                     for p in sorted(glob.glob(f"{root}/**/*.md", recursive=True))
                     if "/node_modules/" not in p and "/plugin/" not in p)
        _manual = n_link + sum(1 for _, w, *_ in all_bad
                               if str(w).startswith(("type-retired:", "db-count (")))
        tail = (f" ({total - _manual} number(s) via --fix, {_manual} by hand)" if _manual
                else " (auto-replaced with --fix)")
        print(f"\n{total} mismatch(es){tail}")
    sys.exit(1 if total else 0)


if __name__ == "__main__":
    #  Record the run under `~/.kal/runs/` —— so the web screen knows about a CLI run too.
    #  Without it, "three successes today and the screen still shows yesterday's failure as
    #
    #  Recording exit code 1 (a mismatch) here **is right** —— "the docs are stale" is a real
    #  result the user should see.  (refresh_kg's `--check` is the opposite: cron uses it as a
    #   probe in `--check || refresh_kg`, so recording a failure there would be a false alarm.)
    from run_log import record
    with record("verify"):
        _main_verify()
