#!/usr/bin/env python3
"""Session transcripts → brain-ingest documents.

Why it is needed
  What ingest_sessions.py produces is a transcript, `**Me**: … **Claude**: …`.
  The raw/conversations/ rules of the brain-ingest skill
  (~/.claude/skills/brain-ingest/SKILL.md) forbid exactly that:
      · no prompt copying — a "User: … / Claude: …" transcript does not go into raw
      · no tool-call logs
      · do not mix several topics from one conversation — separate files, one topic each
  So an LLM rewrites it into a document holding only conclusions and decisions.

Format (SKILL.md step 5 frontmatter + the special rules for self-generated sources)
  title / type: conversation / captured / origin: claude-session
  doc_type ∈ {plan, analysis, design, discussion, decision, retro, investigation}
  why_captured  — one sentence on the context in which a future me or agent will look for this
  tags / session_id  — so it can be traced back

On batch automation
  SKILL.md is explicit: "batch automation ❌ · confirm classification and why_captured with 1–2 questions".
  464 sessions cannot be asked about one at a time, so **the LLM proposes the classification and
  why_captured** and the full list is left in `_distill_review.tsv` for the user to review and correct.
  This compromise follows an explicit user request (process every session at once) and is recorded in the README.

No wiki/ synthesis
  SKILL.md step 6 asks for wiki/sources/ plus index and log updates, but synthesising 464 of them
  buries the 97 curated notes (a gold-in/gold-out violation).  The user's request centres on
  "turn them into documents and load them into the tables", so this stops at placing them under raw/.

Usage:
  python distill_sessions.py                 # everything
  python distill_sessions.py --limit 5       # a taste
  python distill_sessions.py --workers 6
"""
import os, re, json, time, argparse, subprocess, datetime
import sys
import concurrent.futures as cf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# LLM calls go through **one canonical path** —— claude_cli.run().
# This used to invoke `claude` through subprocess directly, and two things were wrong at once:
#   ① NO_TOOLS and child_env were never imported, so it raised NameError, and the caller's
#      `except Exception: pass` swallowed it 3 times —— **every session came back empty**.
#   ② Even with the names right, the claude inside the container has no auth ("Not logged in").
#      It has to go through the host relay (KAL_CLAUDE_RELAY), and a direct call bypasses that.
# claude_cli.run decides between relay and local itself.  (pipeline check, 2026-08-19)
from claude_cli import run as claude_run  # noqa: E402


# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
#  Two corpora, one pipeline.  Claude and Codex sessions distil identically —— what differs is
#  only the URI scheme their provenance gets in the openwiki bundle (claude-session:// vs
#  codex-session://), which `openwiki_emit.py` derives from the `agent` field carried through here.
#  A missing file is not an error: a machine may have only one of the two agents installed.
CORPORA = [("claude", os.path.join(KAL_HOME, "sessions/session_docs.json")),
           ("codex",  os.path.join(KAL_HOME, "sessions/codex_session_docs.json"))]
SESS = CORPORA[0][1]        # kept for the self-check and for anything still naming it
OUT = os.environ.get("KAL_DISTILLED", os.path.join(KAL_HOME, "distilled"))          # built outside the vault first
# ⚠ REVIEW and DONE **must follow OUT.**  They used to be hardcoded to KAL_HOME, so an attempt
# to test safely with `KAL_DISTILLED=/tmp/probe` still wrote the review list and the completion
# markers **to the real path**.  That is how a 401-row _distill_review.tsv was overwritten with
# 6 rows (2026-08-19).  Change the output path and the by-products have to follow it.
REVIEW = os.path.join(OUT, "_distill_review.tsv")
DONE = os.path.join(OUT, ".done")   # completion markers, for resuming
#  ⚠ **haiku could not do this job, and the evidence is in the corpus.**  Measured 2026-09-02:
#     96% of sessions (561 of 586) fit in a **single** window, so the model saw a claim and its
#     later retraction in one call —— and still emitted the retracted claim as a document.  One
#     session in this project alone carried five statements that were made confidently and then
#     overturned in the same conversation.  Telling a superseded claim from a standing one is a
#     reading-comprehension task over a long transcript, which is exactly where a small model
#     fails quietly: it produces a well-formed document that is wrong.
#
#     The windowing problem is real but small (4%).  The model was the main cause.
MODEL = os.environ.get("KAL_DISTILL_MODEL", "opus")
WINDOW = 55_000            # the most characters put into one call
MAX_PARTS = 8              # even a long session splits into at most 8 — beyond that, head and tail first
SPLIT = "---8<---"         # the separator the LLM uses when splitting topics
#  `correction` is new (2026-09-02).  A conversation that reached a wrong conclusion and then
#  overturned it produces knowledge the vault had **no type for** —— so it was either dropped
#  or, worse, filed as a "decision" in its pre-correction form.
DOC_TYPES = ("plan", "analysis", "design", "discussion", "decision", "retro",
             "investigation", "correction")

# Projects to exclude (a substring match on session_project).  Removed from the corpus at the user's request.
# The original transcripts are untouched in ~/.kal/sessions/session_docs.json, so undoing a
# removal here is a matter of editing this list alone.
EXCLUDE_PROJECTS = ("quad",)

#  ⚠ **A deliberate behaviour change, 2026-09-01.**  This prompt used to end with "write in
#     Korean", which was right while the only vault was a Korean one and wrong for anything
#     shipped.  It now says "write in the language of the conversation": a Korean session still
#     produces a Korean document, so nothing changes for the vault this was built against, and
#     an English session stops being forced into Korean.
#
#     Already-distilled sessions carry a `.done` marker and are not re-run, so nothing existing
#     is rewritten by this.
#  ⚠ **The rule this prompt was missing.**  It used to say only "keep conclusions and decisions",
#     which does not tell the model that a claim overturned later in the same conversation is not
#     a conclusion.  A conversation is a **thinking process**: wrong intermediate states belong in
#     it, and they are normal.  A document must carry the state the conversation *ended at*.
#
#     Measured on the corpus this replaces: statements made confidently and then overturned in the
#     same session were shipped as standing documents, indistinguishable from correct ones.  Once
#     indexed, a wrong sentence and a right one carry the same weight, and the reader cannot tell.
#
#     The second half matters as much: **a correction is knowledge, not noise.**  "X was believed,
#     then Y overturned it, and here is why" is often the most valuable thing in a session ——
#     it stops the same mistake being made again.  It gets its own doc_type.
PROMPT = """You distil a Claude Code conversation into documents for a personal wiki.

A conversation is a thinking process.  It contains claims that were later corrected, paths that
were abandoned, and guesses that were checked and found wrong.  **That is normal and expected.**
Your job is to emit only what the conversation **arrived at**, plus the corrections themselves.

## The one rule that matters

**Later beats earlier.**  If something is asserted and then contradicted, retracted, or
superseded anywhere later in the conversation, the later state is the truth.  Never emit the
earlier state as though it still stands.

Read the whole conversation before writing anything.  A claim near the top may be overturned near
the bottom; a document written from the top alone is confidently wrong.

## What to emit

Emit a document only for a thread that **closed** —— it reached a conclusion, a decision, or a
verified fact, and nothing later in the conversation undoes it.

Signals that a thread closed:
- the user confirmed it ("맞아", "좋아", "됐다", "확인했어", "works", "that's right", "ship it")
- a check was run and passed, and the conversation moved on
- a decision was made and then acted on

Signals that a thread did **not** close —— emit nothing for these:
- it was still being debugged when the conversation ended
- the user pushed back and the answer never settled
- it was a plan that was never executed or confirmed
- an error was reported and no fix was verified

**When a claim was corrected, emit the correction as its own document** with
`doc_type: correction`.  It must say three things:
1. what was believed, stated plainly
2. what overturned it —— the measurement, the file, the error, the user's objection
3. why the first belief was reasonable, and what makes the second one better evidence

A correction document is often the single most valuable thing in a session: it stops the same
mistake being repeated.  Do not soften it into "we explored options".

## Forbidden

- Speech transcripts ("Me:" / "Claude:")
- Tool-call logs, command output dumps, progress narration
- Mixing unrelated topics into one document
- Emitting a superseded claim as though it stands
- Inventing confidence the conversation did not have.  If it ended uncertain, emit nothing.

## Required

- Rationale for each decision, as "chose A over B.  Reason: …"
- One summary paragraph at the very top (3–5 sentences).  Reading only that must say what was
  concluded —— and, for a correction, what was wrong.
- Concrete numbers, paths and commands.  That is where the value is.
- Write in the language of the conversation.

Output format —— exactly this, nothing else:

<<<DOC>>>
title: <specific.  No date, no session ID>
doc_type: <plan|analysis|design|discussion|decision|retro|investigation|correction>
why_captured: <one sentence: why a future me would look this up again>
tags: <3-6 comma-separated lowercase kebab-case tags>
---
<body markdown.  ## sections.  400-1500 words.>
<<<END>>>

Repeat the block once per closed thread, at most 3.
If nothing closed —— only small talk, or everything is still open —— output exactly: SKIP

--- conversation log begins ---
{body}
--- conversation log ends ---"""


#  ⚠ The merge used to say "put them in time order", which is precisely the wrong instruction:
#     it preserves both a claim and its retraction as neighbours, in order, as if both stood.
#     Ordering is not reconciling.  Only 4% of sessions are windowed, but for those the merge is
#     the **only** place where a claim in window 1 can meet its correction in window 5 —— the
#     windows are distilled by independent calls that never see each other's input.
MERGE = """Below are documents distilled separately from consecutive pieces of ONE conversation.
They are in order: the first came from the earliest part, the last from the latest.

Because they were written independently, an earlier piece may assert something that a later piece
corrects.  **Later beats earlier.**

Reconcile them, do not merely order them:
- If a later document contradicts, retracts or supersedes an earlier one, **delete the earlier
  version** and keep one document stating the final position.  Where the change is instructive,
  make it `doc_type: correction` and say what was believed, what overturned it, and why.
- If they simply cover different things, keep them separate.
- Remove duplication.

Output the same <<<DOC>>> … <<<END>>> blocks, at most 3.  Nothing else.

{body}"""


def slugify(s, n=60):
    s = re.sub(r"[^\w\s-]", "", (s or "").lower())
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return (s[:n].rstrip("-") or "untitled")


def call(prompt, timeout=600, tries=3):
    """The claude CLI.  ANTHROPIC_API_KEY is stripped so it runs on the subscription account.

    Why retries are mandatory — with 12 workers attached at once, transient failures are common.
    Run without retries, 41 of 180 sessions (23%) were lost to empty output, and the median of
    those was a perfectly good 9,266-character session.  Calling once more by hand succeeded immediately.
    """
    # The retry lives here —— transient failures are common with 12 workers (measured above).
    # But it **does not swallow quietly**: an empty result after the last attempt leaves a trace.
    # There used to be no trace, so nobody knew about the wholesale loss.
    for i in range(tries):
        out = claude_run(MODEL, prompt, timeout=timeout, tools=False)
        if out:
            return out
        if i == tries - 1:
            print(f"    ⚠ no LLM response ({tries} attempts) —— check the relay and authentication")
        time.sleep(3 * (i + 1))
    return ""


def parse(out):
    """<<<DOC>>> blocks → [{title, doc_type, why_captured, tags, body}]"""
    docs = []
    for blk in re.findall(r"<<<DOC>>>(.*?)<<<END>>>", out or "", re.S):
        head, _, body = blk.partition("\n---\n")
        if not body.strip():
            continue
        meta = {}
        for line in head.strip().splitlines():
            k, _, v = line.partition(":")
            if v.strip():
                meta[k.strip().lower()] = v.strip()
        if not meta.get("title"):
            continue
        dt = meta.get("doc_type", "discussion").lower()
        docs.append({
            "title": meta["title"].strip('"'),
            "doc_type": dt if dt in DOC_TYPES else "discussion",
            "why_captured": meta.get("why_captured", "").strip('"'),
            "tags": [t.strip() for t in meta.get("tags", "").split(",") if t.strip()][:6],
            "body": body.strip(),
        })
    return docs[:3]


def windows(text):
    """Split a long session into WINDOW-sized pieces.  Beyond MAX_PARTS, head and tail come first
    (the opening = the problem statement, the close = the conclusion; the middle false starts matter least)."""
    parts = [text[i:i + WINDOW] for i in range(0, len(text), WINDOW)]
    if len(parts) <= MAX_PARTS:
        return parts
    h = MAX_PARTS // 2
    return parts[:h] + parts[-(MAX_PARTS - h):]


def distill(rec):
    """→ (docs, status).  status: ok | skip | fail

    fail and skip must be told apart.  Marking a fail as 'done' loses that session
    permanently — 41 were really lost that way.
    """
    parts = windows(rec["text"])
    if len(parts) == 1:
        out = call(PROMPT.format(body=parts[0]))
        if not out:
            return [], "fail"                        # the call itself failed
        if out.strip() == "SKIP":
            return [], "skip"                        # the LLM judged it small talk
        docs = parse(out)
        return (docs, "ok") if docs else ([], "fail")  # a broken format is a retry candidate
    outs = [call(PROMPT.format(body=p)) for p in parts]
    #  **If even one window has no response, nothing is finalised.**
    #
    #  It used to be `if not any(outs)` —— fail only when everything failed.  So 7 of 8 windows
    #  could exhaust their retries and return empty strings, the remaining 1 would be merged
    #  into an "ok", `.done` would be written and it would **never be attempted again**, while
    #  the frontmatter read `distilled_from: 412 messages` as if all of it had been read.  It read 12%.
    #  On screen there is only an unattributed `⚠ no LLM response` drifting between 10 workers.
    #  (r4-silent, 2026-08-21)
    #
    #  ⚠ `SKIP` is **a legitimate response** (the LLM judged it small talk).  Only an empty
    #    string is a failure.  Conflate them and healthy sessions are retried forever.
    answered = [o for o in outs if o]
    if len(answered) < len(parts):
        return [], "fail"                  # rather than finalise a partial result, do it again next run
    real = [o for o in answered if o.strip() != "SKIP"]
    if not real:
        return [], "skip"
    docs = parse(call(MERGE.format(body="\n\n".join(real)[:120_000])))
    if not docs:                                     # merge failed → keep the per-piece results
        docs = [d for o in real for d in parse(o)][:3]
    return (docs, "ok") if docs else ([], "fail")


def next_slug(title, seen):
    """The first slug not already in `seen`.  On a collision, -v2, -v3 … (SKILL.md step 3).

    ⚠ The old version incremented the counter under **the new slug's** key:
          if slug in seen: slug = f"{slug}-v{seen[slug]+1}"
          seen[slug] = seen.get(slug, 1) + 1      # ← slug is already -v3 here
      So the base key stayed at 1 forever and four documents with the same title became
      ['x', 'x-v3', 'x-v3', 'x-v3'] —— **documents 2 and 3 vanish without a sound.**
      `-v2` is never used at all.  Worse, `.done` is written for the session so it is never
      retried, and the output counts `rows` rather than what is on disk, so it still says "N documents".
      (reproduced by r4-silent, 2026-08-21)

    Now it counts until it finds a free name —— there is no counter to keep in step.

    **Why it is a separate function**: inlined inside `write()`, the self-check would have to
    reimplement the same logic, and that guards nothing (written that way once, and a mutation went uncaught).
    """
    base = slugify(title)
    slug, n = base, 1
    while slug in seen:
        n += 1
        slug = f"{base}-v{n}"
    seen[slug] = 1
    return slug


#  ⚠ **The sibling had this and this file did not.**  `lr_extract.abort_early()` gives up once a
#     run is plainly failing, with the note "three hundred more confirmations of something twenty
#     would have shown" (2026-08-21).  `distill_sessions` never got the same guard, so on
#     2026-09-02 it ran all 391 sessions and failed 166 of them —— the rate climbed 1 → 21 → 78 →
#     166 as it went, the signature of a rate limit, and it kept calling for another twenty
#     minutes after that was obvious.  Each failure costs three LLM attempts (`call(tries=3)`).
#
#     Deliberately more lenient than lr_extract's: distillation failures are **recoverable** ——
#     no completion marker is written, so re-running retries exactly the failures.  Giving up
#     early therefore costs a re-run, not the work.
#
#  ⚠ **A trailing window, not a cumulative rate.**  The first version of this guard used the
#     cumulative figure and a self-check written against the real run showed it would not have
#     fired: 166/391 is 42%, under any sane cumulative threshold.  But the run was not failing
#     at 42% —— it *deteriorated*, and the last 131 sessions failed at 67%.  A cumulative rate
#     averages a collapsing run against its healthy beginning and so is at its most forgiving
#     exactly when things are worst.  The window sees the present.
ABORT_MIN_SAMPLE = 40        # never judge a short run
ABORT_WINDOW = 60            # how many recent sessions the verdict looks at
ABORT_RATE = 0.60


def tally(status, aborted, failed, cancelled):
    """One result → the updated (failed, cancelled) pair.

    ⚠ **A cancelled call is not a failed one.**  After the guard fires, every future already
       submitted still comes back through `as_completed`, instantly, as an exception.  Counting
       those as failures made a run that deliberately stopped at 76 of 481 report **436
       failures** —— about 360 of which were never attempted.  That number reads as "the model is
       broken" rather than "we stopped on purpose", and it is the difference between retrying and
       giving up.  Pulled out of the loop so it can be asserted; inlined it could not be.
    """
    if status != "fail":
        return failed, cancelled
    return (failed, cancelled + 1) if aborted else (failed + 1, cancelled)


def should_abort(recent):
    """Is this run failing *now*?  `recent` is the outcome of the last calls, newest last.

    Counts only —— the caller decides what to do.  Kept separate from the loop so a mutation
    to it is catchable; inlined, the sibling's version could not be tested at all.
    """
    if len(recent) < ABORT_MIN_SAMPLE:
        return False
    w = recent[-ABORT_WINDOW:]
    return sum(1 for x in w if x == "fail") / len(w) >= ABORT_RATE


def write(rec, docs, seen):
    day = (rec.get("first_ts") or "")[:10]      # '2026-07-14T07:32:19.318Z' → '2026-07-14'
    made = []
    for d in docs:
        slug = next_slug(d["title"], seen)
        fm = [
            "---",
            f'title: "{d["title"]}"',
            "type: conversation",
            f"captured: {day}",
            "origin: claude-session",
            f"doc_type: {d['doc_type']}",
            # The LLM occasionally omits this field (3 of 263).  Leaving it silently blank makes
            # it invisible in the review list too, so a marker is left instead.
            f'why_captured: "{d["why_captured"] or "(not generated by the LLM — needs review)"}"',
            f"tags: [{', '.join(d['tags'])}]",
            f"session_id: {rec['session_id']}",
            f"session_project: {rec['project']}",
            #  Not an openwiki extension —— this is the *intermediate* format under
            #  ~/.kal/distilled/.  openwiki_emit turns it into sources[].resource
            #  (`claude-session://` / `codex-session://`), so the published bundle
            #  keeps to OKF v0.2 plus exactly three extension keys.
            f"session_agent: {rec.get('agent', 'claude')}",
            f"distilled_from: {rec['n_msg']} messages, {len(rec['text']):,} chars",
            "distilled_by: distill_sessions.py (LLM, needs review afterwards)",
            # Provenance of the generation —— OKF §5.1 provenance in a flat form.
            # `promote_distilled` rmtree's DEST and lays the files made here back down, so
            # without these two lines **here**, the vault-side values are wiped on every
            # pipeline run.  (2026-08-19, the same vocabulary as fm_migrate.py)
            "generated_by: distill_sessions.py (LLM, needs review afterwards)",
            f"generated_at: {day}",
            "---", "",
        ]
        path = os.path.join(OUT, slug + ".md")
        open(path, "w", encoding="utf-8").write("\n".join(fm) + d["body"] + "\n")
        made.append((slug, d["title"], d["doc_type"], rec["session_id"]))
    return made


def _selftest():
    """Guards this file's two silent losses —— in both, data vanished with no error."""
    import types

    # ① Slug collisions —— several documents with one title must all stay distinct
    #  ★ The logic is not reimplemented —— **the real function** is called.  It was reproduced
    #    here at first, so reverting the real code to the old bug still passed.
    def names(seen, k):
        return [next_slug("work notes", seen) for _ in range(k)]
    fresh = names({}, 4)
    assert len(set(fresh)) == 4, f"identical titles overwrite each other: {fresh}"
    assert fresh[1].endswith("-v2"), f"-v2 is skipped: {fresh}"
    prev = {"work-notes": 1, "work-notes-v2": 1}          # resuming: two already on disk
    again = names(dict(prev), 3)
    assert not (set(again) & set(prev)), f"resuming overwrites existing files: {again}"
    assert len(set(again)) == 3, f"a collision on resume: {again}"

    # ② A partial window failure is not finalised —— once `.done` is written it is never fixed
    orig_call, orig_win, orig_parse = globals()["call"], globals()["windows"], globals()["parse"]
    try:
        globals()["windows"] = lambda t: ["w1", "w2", "w3"]
        globals()["parse"] = lambda o: [{"title": "t", "doc_type": "x", "body": "b"}] if o else []
        rec = {"text": "x" * 10, "n_msg": 3}

        replies = iter(["body", "", ""])                # only 1 window answers
        globals()["call"] = lambda *a, **k: next(replies, "")
        assert distill(rec)[1] == "fail", "2 of 3 windows silent and it finalises as ok"

        replies = iter(["body", "SKIP", "body", "merged"])  # SKIP is a legitimate response
        globals()["call"] = lambda *a, **k: next(replies, "")
        assert distill(rec)[1] == "ok", "SKIP counted as failure —— a healthy session is retried forever"
    finally:
        globals()["call"], globals()["windows"], globals()["parse"] = orig_call, orig_win, orig_parse
    #  ── a plainly failing run gives up ───────────────────────────────────────────────────
    #  2026-09-02: 391 sessions, 166 failures, the rate climbing 1 → 21 → 78 → 166 as it went.
    #  It kept calling for twenty minutes after that was obvious.  `lr_extract` has had this
    #  guard since 2026-08-21; the sibling never got it.
    assert not should_abort(["fail"] * 39), "it gave up on too short a run —— a slow start is not a failure"
    assert not should_abort(["ok"] * 200), "a healthy run was stopped"
    #  ⚠ A normal run has failures in it —— the first 80 sessions of the real run failed at 1%,
    #     and 20% is still a run worth finishing.  Without this the threshold could be tightened
    #     to something that stops healthy work, and no check would notice.
    import itertools as _it
    _tolerable = list(_it.islice(_it.cycle(["fail"] + ["ok"] * 4), 200))      # 20% failing
    assert not should_abort(_tolerable), \
        "a run failing at 20% was stopped —— that is ordinary, not a collapse"
    #  ⚠ **the real 2026-09-02 run.**  A cumulative rate would not have fired (166/391 = 42%);
    #     the window does, because the run deteriorated —— the last stretch failed at ~67%.
    _real = (["ok"] * 80 + ["fail"] * 20 + ["ok"] * 60 + ["fail"] * 58
             + ["ok"] * 43 + ["fail"] * 88)          # 391 total, 166 fail, worsening
    assert should_abort(_real), \
        "the window did not catch a run that deteriorated to 67% —— this is the case it exists for"
    assert sum(1 for x in _real if x == "fail") / len(_real) < 0.5, \
        "the fixture no longer reproduces the run it is modelled on (cumulative must stay under 50%)"
    #  and a run that was bad early but recovered must be allowed to finish
    assert not should_abort(["fail"] * 100 + ["ok"] * 60), \
        "a run that recovered was stopped on its history"

    #  ── the prompt contract ──────────────────────────────────────────────────────────────
    #  These are not style preferences.  Each line pins a rule whose absence produced a measured
    #  defect: documents that shipped a claim the same conversation had already overturned.
    #  A prompt is code with no type checker, so the contract is asserted here instead.
    for _need, _why in (
        ("Later beats earlier", "the supersession rule —— the whole point of the rewrite"),
        ("Read the whole conversation before writing", "a top-down read ships the pre-correction claim"),
        ("closed", "a document may only come from a thread that resolved"),
        ("doc_type: correction", "a correction must be emittable as its own document"),
        ("what overturned it", "a correction must name its evidence, not just say 'we revised'"),
        ("If it ended uncertain, emit nothing", "the guard against inventing confidence"),
        ("SKIP", "there has to be a way to emit nothing"),
    ):
        assert _need in PROMPT, f"PROMPT lost: {_need!r} —— {_why}"
    #  the merge must reconcile, not order.  "time order" was the original instruction and it is
    #  exactly wrong: it keeps a claim and its retraction as neighbours, both apparently standing.
    assert "Later beats earlier" in MERGE, "MERGE lost the supersession rule"
    assert "delete the earlier" in MERGE, "MERGE no longer removes the superseded version"
    assert "time order" not in MERGE, \
        "MERGE went back to ordering —— ordering is not reconciling"
    #  `correction` must be an offered type, or the model cannot use it even when told to
    assert "correction" in DOC_TYPES, "correction is not in the accepted doc_type vocabulary"
    assert all(f"|{d}|" in PROMPT or f"<{d}|" in PROMPT or f"|{d}>" in PROMPT
               for d in ("plan", "correction")), \
        "the prompt's doc_type list drifted from DOC_TYPES"
    #  ── a cancelled call is not a failed one ─────────────────────────────────────────────
    assert tally("fail", False, 0, 0) == (1, 0), "a real failure was not counted"
    assert tally("fail", True, 5, 0) == (5, 1), "a cancelled future was counted as a failure"
    assert tally("ok", True, 5, 2) == (5, 2), "a success moved a counter"
    assert tally("skip", False, 0, 0) == (0, 0), "a small-talk skip was counted as a failure"
    #  the real shape: 76 attempted (46 failing) then 360 cancelled
    _f = _c = 0
    for _i in range(76):
        _f, _c = tally("fail" if _i >= 30 else "ok", False, _f, _c)
    for _ in range(360):
        _f, _c = tally("fail", True, _f, _c)
    assert (_f, _c) == (46, 360), f"the split is wrong: {(_f, _c)}"
    print("  ✅ cancelled futures are not reported as failures")

    print("  ✅ prompt contract —— supersession · closed threads only · correction is a document")

    print("  ✅ early abort —— a trailing window, so a deteriorating run is caught while a\n          recovered one finishes")
    print("  ✅ distill_sessions —— slug collisions · a partial window failure is not finalised")


def _main_distill():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--restart", action="store_true", help="ignore the completion markers and start over")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest(); return

    os.makedirs(OUT, exist_ok=True)
    os.makedirs(DONE, exist_ok=True)
    os.chmod(OUT, 0o700)
    recs = []
    for _agent, _path in CORPORA:
        if not os.path.exists(_path):
            continue
        _rows = json.load(open(_path))
        for _r in _rows:
            #  ⚠ Set only when absent.  `ingest_codex_sessions` already writes it; the Claude
            #     corpus predates the field, so it is filled in here rather than by rewriting
            #     a 12 MB file that a running pipeline may be reading.
            _r.setdefault("agent", _agent)
        recs += _rows
        print(f"  {_agent}: {len(_rows)} session(s) from {os.path.basename(_path)}")
    if not recs:
        print(f"❌ no session corpus found — run ingest_sessions.py / ingest_codex_sessions.py first")
        raise SystemExit(1)
    if EXCLUDE_PROJECTS:
        n0 = len(recs)
        recs = [r for r in recs
                if not any(x in r["project"].lower() for x in EXCLUDE_PROJECTS)]
        if n0 != len(recs):
            print(f"excluded — skipped {n0-len(recs)} session(s) from {'/'.join(EXCLUDE_PROJECTS)}")
    if a.limit:
        recs = recs[:a.limit]
    # Resume — sessions with a completion marker are skipped.  464 sessions × an LLM call
    # means starting over after an interruption is not affordable.
    if not a.restart:
        n0 = len(recs)
        #  ⚠ The marker is namespaced by agent.  A Claude id and a Codex id are both UUIDs from
        #     different generators; nothing guarantees they never collide, and a collision would
        #     silently skip a session that had never been distilled.
        def _done(r):
            #  ⚠ The bare id is the **pre-2026-09-02 name**, written before the corpus grew a
            #     second agent.  Every marker of that shape is Claude's by construction, and
            #     464 of them existed when the scheme changed —— not honouring them would have
            #     re-distilled the whole Claude corpus, 464 LLM calls, for nothing.
            #     Drop this arm once no ~/.kal/distilled/.done holds an unprefixed name.
            a = r.get("agent", "claude")
            if os.path.exists(os.path.join(DONE, f"{a}-{r['session_id']}")):
                return True
            return a == "claude" and os.path.exists(os.path.join(DONE, r["session_id"]))
        recs = [r for r in recs if not _done(r)]
        if n0 != len(recs):
            print(f"resuming — skipped {n0 - len(recs)} completed")
    if not recs:
        # SystemExit("a string") prints that string to stderr and exits with **code 1**.
        # Having nothing to do is not a failure —— the web UI displayed this as "failed".
        print("all done (re-run with --restart)")
        raise SystemExit(0)
    print(f"{len(recs)} session(s) → distilled into brain-ingest documents (workers {a.workers}, {MODEL})")

    t0 = time.time()
    # Register the slugs a previous run created — so resuming does not overwrite the same names
    seen = {os.path.basename(f)[:-3]: 1
            for f in __import__("glob").glob(os.path.join(OUT, "*.md"))}
    rows, done, empty, failed, aborted, recent, cancelled = [], 0, 0, 0, False, [], 0
    with cf.ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(distill, r): r for r in recs}
        for f in cf.as_completed(futs):
            r = futs[f]
            done += 1
            try:
                docs, status = f.result()
            except Exception:
                docs, status = [], "fail"
            if status == "ok":
                rows += write(r, docs, seen)
            elif status == "skip":
                empty += 1
            if status != "fail":             # a failure leaves no marker → the next run picks it up again
                open(os.path.join(DONE, f"{r.get('agent','claude')}-{r['session_id']}"), "w").close()
            recent.append(status)
            #  ⚠ After the abort, `as_completed` still yields every future that was already
            #     submitted.  They come back instantly as exceptions (cancelled), and counting
            #     those as failures makes the summary lie: a run that stopped at 76 of 481
            #     reported **436 failures**, of which ~360 were never attempted.  A number that
            #     large reads as "the model is broken" rather than "we stopped early on purpose".
            failed, cancelled = tally(status, aborted, failed, cancelled)
            if should_abort(recent) and not aborted:
                aborted = True
                w = recent[-ABORT_WINDOW:]
                nf = sum(1 for x in w if x == "fail")
                print(f"  ⛔ {nf}/{len(w)} of the most recent failing ({nf/len(w)*100:.0f}%) "
                      f"—— stopping ({failed}/{done} overall). "
                      f"Nothing is lost: a failure leaves no marker, so re-running retries "
                      f"exactly these.  Try fewer workers (`--workers 4`).", flush=True)
                for fut in futs:
                    fut.cancel()
            if done % 20 == 0 or done == len(recs):
                el = time.time() - t0
                print(f"  {done}/{len(recs)}  {len(rows)} document(s) · {empty} small-talk skip(s) · "
                      f"{failed} failure(s)"
                      + (f" · {cancelled} cancelled" if cancelled else "")
                      + f"  {el/60:.1f} min "
                      f"({el/done*(len(recs)-done)/60:.0f} min left)", flush=True)

    # The review list is built from **the real files on disk**.  Built from the in-memory rows,
    # a resumed run would hold only this round's documents and drop the earlier ones.
    import glob as _g
    allrows = []
    for f in sorted(_g.glob(os.path.join(OUT, "*.md"))):
        head = open(f, encoding="utf-8").read()[:1500]
        g = lambda k: (re.search(rf"^{k}:\s*\"?(.+?)\"?\s*$", head, re.M) or [None, ""])[1]
        allrows.append((os.path.basename(f)[:-3], g("title"), g("doc_type"),
                        g("why_captured"), g("session_id")))
    with open(REVIEW, "w", encoding="utf-8") as fh:
        fh.write("slug\ttitle\tdoc_type\twhy_captured\tsession_id\n")
        for x in allrows:
            fh.write("\t".join(x) + "\n")

    print(f"\n{len(rows)} document(s) · {empty} small-talk skip(s) · {failed} failure(s) · {(time.time()-t0)/60:.1f} min")
    if failed:
        print(f"  ⚠️ the {failed} failure(s) left no completion marker — simply run again to retry them.")
    print(f"  {OUT}")
    print(f"  review list {REVIEW}  ({len(allrows)} rows) ← confirm classification and why_captured afterwards")


if __name__ == "__main__":
    # Record the run under ~/.kal/runs/ —— the web screen's "last run" only knew about runs
    # started from the web UI, so a CLI success still showed yesterday's failure as the last.
    from run_log import record
    with record("distill"):
        _main_distill()
