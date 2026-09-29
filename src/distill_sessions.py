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
import os, re, json, time, argparse, subprocess, datetime, hashlib
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
from claude_cli import run as claude_run, ThirdPartyGateError  # noqa: E402
import ledger  # DESIGN-GITHUB-SYNC.md §3 —— the one progress-ledger writer/reader.  Best-effort:
                # every call site below wraps ledger.append() in try/except so a device with no
                # bundle configured (KAL_VAULT unset — most tests, and any pre-sync install)
                # distils exactly as it did before this file learned about the ledger.


# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
#  Three corpora, one pipeline.  Claude, Codex and Hermes sessions distil identically —— what
#  differs is only the URI scheme their provenance gets in the openwiki bundle (claude-session://,
#  codex-session://, hermes-session://), which `okf_convert.py` derives from the `agent` field
#  carried through here as `session_agent`.
#  A missing file is not an error: a machine may have any subset of the agents installed.
CORPORA = [("claude", os.path.join(KAL_HOME, "sessions/session_docs.json")),
           ("codex",  os.path.join(KAL_HOME, "sessions/codex_session_docs.json")),
           ("hermes", os.path.join(KAL_HOME, "sessions/hermes_session_docs.json"))]
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


def write(rec, docs, seen, continues=None, no_llm=False, privacy=None):
    """continues: an original page's slug — set only for a continuation page (§4.2 step 2-3), which
    gets ` (continued)` appended to its title and a `continues:` frontmatter key pointing back.
    no_llm: inherited from the original page(s) via no_llm_of_pages(), never re-derived by path."""
    day = (rec.get("first_ts") or "")[:10]      # '2026-07-14T07:32:19.318Z' → '2026-07-14'
    no_llm = no_llm or no_llm_of_pages([continues] if continues else (), rec, privacy)
    made = []
    for d in docs:
        title = d["title"] + " (continued)" if continues else d["title"]
        slug = next_slug(title, seen)
        fm = [
            "---",
            f'title: "{title}"',
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
        ]
        if continues:
            #  §4.2 step 3 —— an openwiki extension key beyond the three named in the comment
            #  above; whether OKF's schema accepts an arbitrary extra key is DESIGN-GITHUB-SYNC.md's
            #  own open question (§9), left open here too rather than guessed at.
            fm.append(f"continues: {continues}")
        if no_llm:
            fm.append("no_llm: true")
        fm += ["---", "",
        ]
        path = os.path.join(OUT, slug + ".md")
        with open(path, "w", encoding="utf-8") as out:
            out.write("\n".join(fm) + d["body"] + "\n")
        made.append((slug, d["title"], d["doc_type"], rec["session_id"]))
    return made


#  ── what is pending —— one rule, shared with estimate.py (2026-09-26) ────────────────────────
#  estimate.py used to count pending sessions on its own: the Claude corpus only, bare-id markers
#  only (every session distilled since the markers gained an agent prefix read as pending), and
#  its own environment variable for the output.  It now asks these three.
def corpus_records():
    """Every corpus in CORPORA, minus what exclude.txt lists → (records, [(agent, kept, file,
    left out), …]).  A missing file is skipped: a machine may have any subset of the agents.

    ⚠ **exclude.txt is applied here too, not only when collecting** (2026-09-26).  A line added
       after the last collection used to be distilled, promoted and indexed anyway —— `just distill`,
       the web screen's distil step and rebuild_all.sh all start here, from the corpus as it was.
       A whole-session line drops the record; any session of a stitched Hermes conversation drops
       the conversation (its record lists them as `members`).  A cutoff cannot be applied here:
       a record's text carries no message times.  So a corpus older than the list that holds a cut
       session stops the run —— collect again first, and the collector cuts it.
    """
    import ingest_sessions as I
    raw, roots = [], {}
    for agent, path in CORPORA:
        if not os.path.exists(path):
            continue
        with open(path) as fh:
            rows = json.load(fh)
        for r in rows:
            #  ⚠ Set only when absent.  `ingest_codex_sessions` already writes it; the Claude
            #     corpus predates the field, so it is filled in here rather than by rewriting
            #     a 12 MB file that a running pipeline may be reading.
            r.setdefault("agent", agent)
            for m in r.get("members") or [r["session_id"]]:
                roots[m.lower()] = r["session_id"].lower()
        raw.append((agent, path, rows))
    excluded = I.load_excluded(known=roots, strict=False)
    listed_at = os.path.getmtime(I.EXCLUDE) if excluded else 0
    recs, loaded = [], []
    for agent, path, rows in raw:
        kept, gone = [], 0
        for r in rows:
            hit = [excluded[m.lower()] for m in r.get("members") or [r["session_id"]]
                   if m.lower() in excluded]
            if None in hit:
                gone += 1
                continue
            if hit and listed_at > os.path.getmtime(path):
                raise SystemExit(f"❌ {I.EXCLUDE} is newer than {path}, and it cuts {r['session_id']} ——\n"
                                 "   a corpus record carries no message times to cut by, so collect again "
                                 "first (just openwiki-sessions, or the ingest_*_sessions.py collector) "
                                 "and distil after.")
            kept.append(r)
        recs += kept
        loaded.append((agent, len(kept), os.path.basename(path), gone))
    return recs, loaded


def wanted(r):
    """Not in a project EXCLUDE_PROJECTS names (a substring match)."""
    return not any(x in r["project"].lower() for x in EXCLUDE_PROJECTS)


def is_done(r):
    """Does this session carry a completion marker?

    ⚠ The marker is namespaced by agent.  A Claude id and a Codex id are both UUIDs from different
       generators; nothing guarantees they never collide, and a collision would silently skip a
       session that had never been distilled.
    ⚠ **A conversation that grew since is still done** (impl rounds 4–5).  Round 4 distilled it
       again and replaced its pages; round 5 showed what that breaks further down —— two forks under
       one id deleted each other's pages, a hand-set `no_llm` is carried forward by path and was lost
       when a title changed, a reused page name brought the replaced page's extraction back as
       history, and the full build does not purge a gone page's facts.  Distilling again waits for an
       append-only design; `grew` counts what is left behind so a run can say so.
    """
    #  A crash between the staged ledger row and the marker leaves the row only in the pending file:
    #  without counting it, the session is paid for twice and review_pending keeps refusing.
    key = (f"{r.get('agent', 'claude')}_session", r["session_id"])
    return _marker_of(r) is not None or _ledger_row(r) is not None or key in ledger.pending_keys()


def _ledger_row(r):
    """The synced ledger's latest row for `r` (any device), or None (DESIGN-GITHUB-SYNC.md §3.3).

    ⚠ A device with no local marker — a reinstall, or a session file also visible from a second
       device — is still done when the bundle's ledger says some device finished it: without this a
       reinstall re-distils everything (paid model calls again) and two devices publish one session
       twice.  Best-effort like every ledger touch: an unreadable ledger reads as "no row".
    """
    key = (f"{r.get('agent', 'claude')}_session", r["session_id"])
    try:
        return ledger.status_of(*key) if _LEDGER_INDEX is None else _LEDGER_INDEX.get(key)
    except Exception as error:
        return _ledger_unreadable(error)


#  One `ledger.read_all` per run instead of one per record (O(records × rows)); set by
#  _main_distill, None elsewhere so direct callers (tests, other scripts) always read fresh.
_LEDGER_INDEX = None
_LEDGER_WARNED = False


def _ledger_unreadable(error):
    """A ledger that cannot be read reads as "no row" — but say so once, and never in sync's
    staged mode, where a silent "no row" means a second paid distillation of a done session."""
    global _LEDGER_WARNED
    if os.environ.get("KAL_LEDGER_STAGE"):
        raise error
    if not _LEDGER_WARNED:
        _LEDGER_WARNED = True
        print(f"warning: synced ledger unreadable ({type(error).__name__}); treating sessions as not done "
              "by it", file=sys.stderr)
    return None


def _prior_pages(r):
    """The page slugs already published for `r`: the local marker's, else the synced ledger row's
    (a session done on another device has no local marker but its row lists the pages)."""
    m = _marker_of(r)
    pages = (marker_info(m) or {}).get("pages") if m else None
    if not pages:
        row = _ledger_row(r)
        pages = row.get("pages") if row else None
        if row and not pages:                 # the newest row may be a page-less one; an earlier row has them
            pages = ledger.pages_of(f"{r.get('agent', 'claude')}_session", r["session_id"])
    return _valid_pages(pages)


#  What `slugify` can produce (plus a `-vN` suffix).  Another device's ledger row is data from outside
#  this machine: a page name with a newline, `..` or `:` must never reach a `continues:` frontmatter line.
_PAGE_RE = re.compile(r"[\w-]{1,80}")


def _valid_pages(pages):
    return [p for p in pages if isinstance(p, str) and _PAGE_RE.fullmatch(p)] if isinstance(pages, list) else []


def _done_ts(r):
    """How far `r` was distilled, as an epoch: the newest of the local marker's `last_ts` and the
    ledger row's `distilled_through_last_ts`.  None when neither recorded one."""
    import ingest_sessions as I
    m = _marker_of(r)
    seen = [I.epoch((marker_info(m) or {}).get("last_ts")) if m else None]
    row = _ledger_row(r)
    seen.append(row.get("distilled_through_last_ts") if row else None)
    seen = [t for t in seen if isinstance(t, (int, float))]
    return max(seen) if seen else None


def _marker_of(r):
    """The completion marker of `r`, or None.

    ⚠ The bare id is the **pre-2026-09-02 name**, written before the corpus grew a second agent.
       Every marker of that shape is Claude's by construction, and 464 of them existed when the
       scheme changed —— not honouring them would have re-distilled the whole Claude corpus, 464 LLM
       calls, for nothing.  Drop this arm once no ~/.kal/distilled/.done holds an unprefixed name.
    """
    a = r.get("agent", "claude")
    for p in [os.path.join(DONE, f"{a}-{r['session_id']}")] + \
             ([os.path.join(DONE, r["session_id"])] if a == "claude" else []):
        if os.path.exists(p):
            return p
    return None


def marker_info(path):
    """What a marker recorded —— {"last_ts": how far the text reached, "pages": the slugs it made}
    —— or None for one written before 2026-09-26, which is empty.  The exclusion check reads it to
    tell pages made from cut text from older ones (`ingest_sessions._check_listed`)."""
    try:
        with open(path, encoding="utf-8") as fh:
            info = json.loads(fh.read() or "null")
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def grew(r):
    """Distilled before, and holding turns from after what its marker recorded?  Acted on since
    DESIGN-GITHUB-SYNC.md §4.2 — see continuation_candidates()/write_continuation() — but the old
    docstring's warning still holds for the *marked* pages themselves: they are never rewritten,
    only a new page beside them.  An empty, older marker recorded nothing, so it cannot tell."""
    import ingest_sessions as I
    now = I.epoch(r.get("last_ts"))
    was = _done_ts(r)
    return now is not None and was is not None and now > was


#  ── settle rule (DESIGN-GITHUB-SYNC.md §4.2) ───────────────────────────────────────────────────
#  A session still being talked to is not a distillation candidate yet: distilling mid-conversation
#  makes a half-cut page, and that page's `last_ts` becomes the next "did it grow" baseline —
#  colliding with §4.1's "never re-distil a completed session" sooner than it should.
SETTLE_HOURS = float(os.environ.get("KAL_SETTLE_HOURS", "6"))


def settled(r, hours=None, now=None):
    """Has `r` gone quiet for `hours` (default SETTLE_HOURS) since its last turn?

    ⚠ **Fails open, not closed.**  A record with no readable `last_ts` cannot be timed at all —
       refusing to distil it would silently strand it forever (nothing later makes the timestamp
       readable).  This only adds a *reason to wait*, never a permanent exclusion; `wanted()` and
       `is_done()` already carry the exclusions that are meant to stick.
    """
    import ingest_sessions as I
    ts = I.epoch(r.get("last_ts"))
    if ts is None:
        return True
    hrs = SETTLE_HOURS if hours is None else hours
    return ((time.time() if now is None else now) - ts) >= hrs * 3600


def _ledger_row_of(r, made, continues=None, no_llm=False, pages=None):
    import ingest_sessions as I
    agent = r.get("agent", "claude")
    row = {
        "source_type": f"{agent}_session",
        "source_id": r["session_id"],
        "content_hash": hashlib.sha256((r.get("text") or "").encode("utf-8")).hexdigest(),
        "source_updated_at": r.get("last_ts"),
        "distilled_through_last_ts": I.epoch(r.get("last_ts")),
        "no_llm": bool(no_llm),
    }
    if continues:
        row["continues"] = continues
    pages = [m[0] for m in made] if pages is None else pages
    if pages:
        row["pages"] = pages
    return row


def _ledger_record(r, made, continues=None, no_llm=False, pages=None):
    """Best-effort ledger row (DESIGN-GITHUB-SYNC.md §3.2) for a completed distillation pass,
    first or continuation.  Never raises — a device with no bundle configured (KAL_VAULT unset,
    true of most tests and any pre-sync install) must distil exactly as it did before the ledger
    existed; ledger.append() already no-ops in that case, this is defence for anything else.

    Sync's explicit pending-ledger mode is strict instead: staging errors must propagate
    before a completion marker is written.
    """
    try:
        ledger.append(_ledger_row_of(r, made, continues, no_llm, pages))
    except Exception:
        if os.environ.get("KAL_LEDGER_STAGE"):
            raise
        pass


def mark_done(r, made=()):
    """The completion marker, recording how far `r` was distilled and the pages it made (`write`).
    Also appends a best-effort ledger row (§3.2, see _ledger_record)."""
    if os.environ.get("KAL_LEDGER_STAGE"):
        _ledger_record(r, made)
    with open(os.path.join(DONE, f"{r.get('agent', 'claude')}-{r['session_id']}"), "w") as fh:
        json.dump({"last_ts": r.get("last_ts"), "pages": [m[0] for m in made]}, fh)
    if not os.environ.get("KAL_LEDGER_STAGE"):
        _ledger_record(r, made)


def privacy_snapshot():
    from pathlib import Path
    from source_links import load_frontmatter
    import schema_v3 as S

    def unreadable(error):
        raise error

    entries, links, blocked = [], [], set()
    roots = [Path(OUT)]
    if os.environ.get("KAL_VAULT"):
        roots.append(Path(os.environ["KAL_VAULT"]))
    for root in roots:
        if not root.exists():
            continue
        for directory, dirs, files in os.walk(root, onerror=unreadable):
            dirs[:] = [name for name in dirs if name != ".git"]
            for name in files:
                if not name.endswith(".md"):
                    continue
                path = Path(directory) / name
                raw = path.read_text(encoding="utf-8")
                try:
                    data = load_frontmatter(raw)
                except ValueError as error:
                    raise ValueError(f"cannot resolve privacy: {error}: {path}") from None
                if not data:
                    continue
                keys = {("page", path.stem)} if root == Path(OUT) else set()
                if data.get("session_id"):
                    keys.add((str(data.get("session_agent", "claude")), str(data["session_id"]).lower()))
                sources = data.get("sources")
                for source in sources if isinstance(sources, list) else []:
                    resource = source.get("resource") if isinstance(source, dict) else None
                    if isinstance(resource, str):
                        agent, sep, sid = resource.partition("-session://")
                        if sep and agent in S.SESSION_AGENTS and sid:
                            keys.add((agent, sid.lower()))
                if isinstance(data.get("continues"), str):
                    keys.add(("page", Path(data["continues"]).stem))
                links.append(keys)
                entries.append((path, raw, keys, root == Path(OUT)))
                if S.doc_meta(raw)[2]:
                    blocked.update(keys)
    for path in Path(DONE).glob("*"):
        info = marker_info(path) or {}
        agent, sep, sid = path.name.partition("-")
        if not sep or agent not in S.SESSION_AGENTS:
            agent, sid = "claude", path.name
        links.append({(agent, sid.lower()), *(("page", slug) for slug in info.get("pages", []))})
    while True:
        before = len(blocked)
        for keys in links:
            if keys & blocked:
                blocked.update(keys)
        if len(blocked) == before:
            break
    return blocked, entries


def carry_privacy(snapshot):
    import schema_v3 as S
    from frontmatter import FM_RE

    blocked, entries = snapshot
    for path, raw, keys, local in entries:
        if not local or not keys & blocked or S.doc_meta(raw)[2]:
            continue
        match = FM_RE.match(raw)
        head = re.sub(r'''(?im)^["']?no_llm["']?\s*:[^\n]*\n?''', "", match.group(1))
        path.write_text(raw[:match.start(1)] + "no_llm: true\n" + head + raw[match.end(1):], encoding="utf-8")


def _ledger_no_llm(r):
    """Does the latest synced ledger row for `r` say `no_llm`?  Its pages may be on another device."""
    row = _ledger_row(r)
    return bool(row and row.get("no_llm"))


def no_llm_of_pages(pages, rec=None, snapshot=None):
    """Does any already-written page of a session carry `no_llm: true` in its own frontmatter —
    a human's later correction, which the corpus record never carries (§4.3)?  Looked up by the
    page's own content, not by re-deriving anything from a path or title: §4.3 exists precisely
    because a path/title-keyed lookup lost this flag once already when a title changed."""
    blocked, _ = snapshot if snapshot is not None else privacy_snapshot()
    keys = {("page", slug) for slug in pages or ()}
    if rec is not None:
        keys.update((rec.get("agent", "claude"), str(sid).lower())
                    for sid in rec.get("members") or [rec["session_id"]])
    return bool(keys & blocked)


def mark_continued(r, made):
    """Extend an existing completion marker after a continuation page was written (§4.2 step 4):
    `last_ts` advances to `r`'s current last_ts, and the new pages are APPENDED to (never replace)
    the ones already on record — they must stay discoverable by the exclusion check
    (`ingest_sessions._check_listed`) and by the next `grew()` comparison.  The marker must already
    exist: this is only ever called on a session `is_done()` already reports true for.
    """
    path = _marker_of(r) or os.path.join(DONE, f"{r.get('agent', 'claude')}-{r['session_id']}")
    prior = _prior_pages(r)               # a ledger-only "done" has no local marker: its row has the pages
    pages = prior + [m[0] for m in made]
    row = dict(continues=prior[0] if prior else None, no_llm=no_llm_of_pages(prior, r) or _ledger_no_llm(r), pages=pages)
    if os.environ.get("KAL_LEDGER_STAGE"):
        _ledger_record(r, made, **row)
    with open(path, "w") as fh:
        json.dump({"last_ts": r.get("last_ts"), "pages": pages}, fh)
    if not os.environ.get("KAL_LEDGER_STAGE"):
        _ledger_record(r, made, **row)


def tail_since(abs_path, since_epoch, *, through=None, cutoff=None):
    """Re-parse a raw session log (a corpus record's `abs_path`) for only the turns strictly after
    `since_epoch` — the continuation page's input (§4.2 step 1).  {"text", "n_msg", "last_ts"} or
    None if nothing qualifies, the file cannot be read, or the tail looks like a pipeline call.

    Mirrors `ingest_sessions.parse_session`'s per-line walk (block extraction, noise filtering,
    secret masking, machine-call detection) rather than a second implementation of it — a session
    tail is exactly as sensitive as a whole session, and every one of those checks exists because
    skipping it once already leaked something into this pipeline.

    ⚠ **`agent_ids` is collected over the WHOLE file, not just the tail.**  A subagent's `Task`
       tool_use can precede the settle boundary while its `tool_result` arrives after it —
       `parse_session`'s single forward pass gets this right only because its cutoff is an upper
       bound (both members of a pair are on the same side of it); a lower bound has no such
       guarantee, so the id has to be known before the boundary is applied.
    """
    import ingest_sessions as I
    parts, seen, agent_ids = [], set(), set()
    n_msg = 0
    last_ts = None
    try:
        fh = open(abs_path, encoding="utf-8", errors="ignore")
    except (OSError, TypeError):
        return None
    with fh:
        for ln in fh:
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            if rec.get("type") not in ("user", "assistant"):
                continue
            c = (rec.get("message") or {}).get("content")
            if isinstance(c, list):
                for b in c:
                    if (isinstance(b, dict) and b.get("type") == "tool_use"
                            and b.get("name") in I.AGENT_TOOLS and b.get("id")):
                        agent_ids.add(b["id"])
            e = I.epoch(rec.get("timestamp"))
            if e is None or e <= since_epoch:
                continue
            if (through is not None and e > through) or not I.before_cutoff(e, cutoff):
                continue
            last_ts = rec.get("timestamp")
            for t in I.blocks_of(rec.get("message"), agent_ids):
                if I.is_noise(t):
                    continue
                h = hashlib.md5(t[:400].encode()).hexdigest()
                if h in seen:
                    continue
                seen.add(h)
                role = "Me" if rec["type"] == "user" else "Claude"
                parts.append(f"**{role}**: {t.strip()[:I.TURN_MAX]}")
                n_msg += 1
    if not parts:
        return None
    text = "\n\n".join(parts)
    if I.is_pipeline_call(text):
        return None
    text, _masked = I.mask(text)
    if I.find_leaks(text):
        return None            # fail closed —— never hand a continuation with an unmasked secret to the LLM
    return {"text": text, "n_msg": n_msg, "last_ts": last_ts}


def continuation_tail(rec, since):
    import ingest_sessions as I

    agent = rec.get("agent", "claude")
    through = I.epoch(rec.get("last_ts"))
    if through is None:
        raise ValueError("continuation has no readable upper timestamp")
    members = rec.get("members") or [rec["session_id"]]
    excluded = I.load_excluded(known={sid.lower(): rec["session_id"].lower() for sid in members}, strict=False)
    cuts = [excluded[sid.lower()] for sid in members if sid.lower() in excluded]
    if None in cuts:
        raise ValueError("continuation session is excluded")
    cutoff = min(cuts) if cuts else None
    if agent == "claude":
        tail = tail_since(rec.get("abs_path"), since, through=through, cutoff=cutoff)
    elif agent == "codex":
        import ingest_codex_sessions as C
        tail = C.parse_rollout(rec["abs_path"], cutoff, since=since, through=through)
    elif agent == "hermes":
        import ingest_hermes_sessions as H
        path, sep, sid = rec["abs_path"].rpartition("#")
        if not sep or sid != rec["session_id"]:
            raise ValueError("invalid Hermes continuation locator")
        sources = tuple(s.strip() for s in os.environ.get("KAL_HERMES_SOURCES", "").split(",") if s.strip()) or H.DEFAULT_SOURCES
        if any(s not in H.DEFAULT_SOURCES for s in sources) and not H.claude_cli.no_tools_verified():
            raise ThirdPartyGateError("Hermes continuation requires verify-extract-tools before reading opted-in sources")
        conversations, _, _, _ = H.read_store(path, sources=sources, excluded=excluded)
        conversation = next((c for c in conversations if c["id"] == sid), None)
        if conversation is None:
            raise ValueError("Hermes continuation is unavailable under current collection policy")
        turns = [turn for turn in conversation["turns"]
                 if I.epoch(turn[2]) is not None and since < I.epoch(turn[2]) <= through]
        tail = {"text": H.render(turns), "n_msg": len(turns),
                "last_ts": H._iso(max(I.epoch(t[2]) for t in turns))} if turns else None
    else:
        raise ValueError(f"unsupported continuation agent: {agent}")
    if not tail or not tail["text"]:
        raise ValueError(f"{agent} continuation could not be read as eligible turns")
    if I.is_pipeline_call(tail["text"]):
        raise ValueError("continuation is a pipeline call")
    text, _ = I.mask(tail["text"])
    if I.find_leaks(text):
        raise ValueError("continuation failed masking verification")
    return {**tail, "text": text}


def continuation_candidates(recs):
    """Of `recs`, the ones that are done, settled, grew, and can actually be sliced (an `abs_path`
    to re-parse and a marker with a readable `last_ts`) — → [(rec, since_epoch), …]."""
    import ingest_sessions as I
    out = []
    for r in recs:
        if not (is_done(r) and settled(r) and grew(r)) or _ledger_no_llm(r):
            continue
        if not r.get("abs_path"):
            continue
        since = _done_ts(r)
        if since is None:
            continue
        out.append((r, since))
    return out


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

    #  ── pending sessions: one rule, and estimate.py counts with it ─────────────────────────
    #  Patched on the module *as imported* —— that is the copy estimate.py reads, and it is not
    #  this one when this file runs as __main__.
    import tempfile, contextlib, io
    from unittest import mock
    import distill_sessions as D
    import estimate
    import ingest_sessions as I
    with tempfile.TemporaryDirectory() as d:
        corp = [(a, os.path.join(d, f"{a}.json")) for a in ("claude", "codex", "hermes")]
        rows = {"claude": [{"session_id": "c1", "project": "p"}, {"session_id": "c2", "project": "p"}],
                "codex": [{"session_id": "x1", "project": "p", "agent": "codex"}],
                "hermes": [{"session_id": "h1", "project": "quad-work", "agent": "hermes"},
                           {"session_id": "h2", "project": "p", "agent": "hermes"}]}
        for a, p in corp:
            with open(p, "w") as fh:
                json.dump(rows[a], fh)
        done = os.path.join(d, ".done")
        os.makedirs(done)
        #  a legacy bare Claude marker, an agent-prefixed Hermes one, and a bare one that must not
        #  count for Codex —— the bare shape is Claude's alone
        for name in ("c1", "hermes-h2", "x1"):
            open(os.path.join(done, name), "w").close()
        #  exclude.txt and the distilled pages are read too: both point into the temp dir
        with mock.patch.object(D, "CORPORA", corp), mock.patch.object(D, "DONE", done), \
                mock.patch.object(D, "OUT", d), \
                mock.patch.object(I, "EXCLUDE", os.path.join(d, "no-exclude.txt")):
            recs, loaded = D.corpus_records()
            assert [a for a, *_ in loaded] == ["claude", "codex", "hermes"], loaded
            pending = [r["session_id"] for r in recs if D.wanted(r) and not D.is_done(r)]
            assert pending == ["c2", "x1"], f"the pending set is wrong: {pending}"
            n, why = estimate._count_distill()
            assert (n, why) == (2, "2 of 4 session(s) done"), \
                f"estimate counts pending sessions differently from distill: {(n, why)}"
    print("  ✅ pending = every corpus · project exclusions · agent-namespaced markers, and "
          "estimate.py counts the same")

    #  ── the marker records how far it distilled; a conversation that grew is reported, not redone ──
    #  (impl rounds 4–5 —— see is_done).  The record is what the exclusion check reads.
    with tempfile.TemporaryDirectory() as d:
        done = os.path.join(d, ".done")
        os.makedirs(done)
        at = "2026-09-25T09:00:00Z"
        with mock.patch.object(D, "DONE", done), mock.patch.object(D, "OUT", d):
            D.mark_done({"session_id": "same", "agent": "hermes", "last_ts": at}, [("a-page", "t", "x", "same")])
            assert D.marker_info(os.path.join(done, "hermes-same")) == {"last_ts": at, "pages": ["a-page"]}, \
                "the marker does not record how far it distilled —— the exclusion check falls back to mtimes"
            for sid in ("grown", "no-ts"):
                D.mark_done({"session_id": sid, "agent": "hermes", "last_ts": at})
            open(os.path.join(done, "legacy"), "w").close()                  # before 2026-09-26: empty
            got = {r["session_id"]: (D.is_done(r), D.grew(r)) for r in (
                {"session_id": "same", "agent": "hermes", "last_ts": at},
                {"session_id": "grown", "agent": "hermes", "last_ts": "2026-09-25T10:00:00Z"},
                {"session_id": "no-ts", "agent": "hermes"},
                {"session_id": "legacy", "last_ts": "2030-01-01T00:00:00Z"},
                {"session_id": "never", "agent": "hermes", "last_ts": at})}
            assert got == {"same": (True, False), "grown": (True, True), "no-ts": (True, False),
                           "legacy": (True, False), "never": (False, False)}, got
            #  …and main() writes that record, reports what grew without redoing it, retries a
            #  failure (no marker —— the 41 sessions lost to a marker written on failure are in this
            #  file's history), and marks a SKIP done with no page.  Every path main() writes is in the
            #  temp dir (REVIEW is derived from OUT at import, so it needs its own).
            corpus = os.path.join(d, "hermes.json")

            def rec(sid, last_ts):
                return {"session_id": sid, "project": "p", "agent": "hermes", "n_msg": 2, "first_ts": at,
                        "last_ts": last_ts, "text": "**Me**: a question\n\n**Hermes**: an answer"}

            def run(recs, reply, expect_failure=False):
                with open(corpus, "w") as fh:
                    json.dump(recs, fh)
                buf = io.StringIO()
                with mock.patch.object(D, "CORPORA", [("hermes", corpus)]), \
                        mock.patch.object(D, "REVIEW", os.path.join(d, "_distill_review.tsv")), \
                        mock.patch.object(I, "EXCLUDE", os.path.join(d, "no-exclude.txt")), \
                        mock.patch.object(D, "call", reply), \
                        mock.patch.object(sys, "argv", ["distill_sessions.py", "--workers", "1"]), \
                        contextlib.redirect_stdout(buf):
                    code = 0
                    try:
                        D._main_distill()
                    except SystemExit as e:
                        code = e.code
                    assert bool(code) == expect_failure, f"unexpected distillation exit: {code}"
                return buf.getvalue()

            def pages():
                return sorted(f for f in os.listdir(d) if f.endswith(".md"))
            doc = lambda *a, **k: ("<<<DOC>>>\ntitle: Marker check\ndoc_type: decision\nwhy_captured: w\n"
                                   "tags: a\n---\nbody\n<<<END>>>")
            run([rec("h9", at)], doc)
            assert D.marker_info(os.path.join(done, "hermes-h9")) == {"last_ts": at, "pages": ["marker-check"]}
            out = run([rec("h9", "2026-09-25T10:00:00Z")], doc)
            assert "1 of them grew since they were distilled" in out and pages() == ["marker-check.md"], \
                f"a conversation that grew was redone, or went unreported: {pages()}\n{out}"
            run([rec("hfail", at)], lambda *a, **k: "", expect_failure=True)
            assert not os.path.exists(os.path.join(done, "hermes-hfail")), \
                "a failed session was marked done —— it is never retried"
            #  A page removed by hand leaves its name taken while a marker lists it (review round 6).
            os.remove(os.path.join(d, "marker-check.md"))
            run([rec("h10", at)], doc)
            assert pages() == ["marker-check-v2.md"], f"a name a marker still lists was reused: {pages()}"
            os.remove(os.path.join(d, "marker-check-v2.md"))
            with open(os.path.join(d, "marker-check.md"), "w") as fh:
                fh.write("---\ntitle: \"t\"\nsession_id: h9\nsession_agent: hermes\n---\nbody\n")
            run([rec("hskip", at)], lambda *a, **k: "SKIP")
            assert D.marker_info(os.path.join(done, "hermes-hskip")) == {"last_ts": at, "pages": []} \
                and pages() == ["marker-check.md"], "a SKIP was not marked done, or wrote a page"
            #  estimate's --json stays JSON: the ⓘ note the exclusion check prints went ahead of it, and
            #  the web screen's parse fell back to {} (review round 6).  h9 is distilled up to `at`,
            #  listed with a later cutoff —— exactly the state that prints the note.
            excl = os.path.join(d, "exclude-note.txt")
            with open(excl, "w") as fh:
                fh.write("h9 2026-09-25T10:00:00Z\n")
            os.utime(excl, (os.path.getmtime(corpus) - 60,) * 2)
            buf = io.StringIO()
            with mock.patch.object(D, "CORPORA", [("hermes", corpus)]), mock.patch.object(I, "EXCLUDE", excl), \
                    contextlib.redirect_stdout(buf):
                estimate._count_distill()
            assert buf.getvalue() == "", f"estimate printed ahead of its JSON: {buf.getvalue()!r}"
    print("  ✅ the marker records how far it distilled and the pages it made; a grown conversation is "
          "reported, a failure retried, a SKIP marked done")

    #  ── settle rule (DESIGN-GITHUB-SYNC.md §4.2) ─────────────────────────────────────────────
    assert not D.settled({"last_ts": datetime.datetime.now(datetime.timezone.utc).isoformat()}), \
        "a session mid-conversation right now was called settled"
    assert D.settled({"last_ts": "2020-01-01T00:00:00Z"}), "an old session was called unsettled"
    assert D.settled({}), "a record with no readable last_ts must fail OPEN, not strand forever"
    old_ts = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(hours=1)).isoformat()
    assert not D.settled({"last_ts": old_ts}, hours=6), "1h old is not settled under a 6h rule"
    assert D.settled({"last_ts": old_ts}, hours=0.5), "1h old IS settled under a 30min rule"
    print("  ✅ settled() —— fails open on an unreadable last_ts, respects an explicit hours override")

    #  ── continuation pages (§4.2) — a session that grew gets a NEW page for the new turns only,
    #     never a rewrite of the original.  End to end: a synthetic raw transcript (abs_path),
    #     mark_done() at turn 2, the session grows to turn 4, and a second distil run must produce
    #     exactly one continuation page holding only turns 3-4, with the marker extended (not
    #     replaced) and continues: pointing at the original.  No LLM: `call` is a fake.
    with tempfile.TemporaryDirectory() as d:
        raw = os.path.join(d, "session.jsonl")

        def turn(role, text, ts):
            return json.dumps({"type": role, "timestamp": ts,
                                "message": {"content": [{"type": "text", "text": text}]}})
        t0, t1, t2, t3 = ("2026-09-01T00:00:0%dZ" % i for i in range(4))
        with open(raw, "w") as fh:
            fh.write("\n".join([
                turn("user", "EARLY-TURN-ONE this is well over forty characters long for sure", t0),
                turn("assistant", "EARLY-TURN-TWO also comfortably over the forty char noise floor", t1),
                turn("user", "LATE-TURN-THREE this one arrives after the settle marker was written", t2),
                turn("assistant", "LATE-TURN-FOUR the continuation page must hold only this and #3", t3),
            ]) + "\n")

        # tail_since: only turns strictly after t1's epoch survive
        since = I.epoch(t1)
        tail = D.tail_since(raw, since)
        assert tail is not None and "LATE-TURN-THREE" in tail["text"] and "LATE-TURN-FOUR" in tail["text"], tail
        assert "EARLY-TURN-ONE" not in tail["text"] and "EARLY-TURN-TWO" not in tail["text"], \
            f"a turn at or before the cutoff leaked into the continuation: {tail['text']!r}"
        assert tail["last_ts"] == t3, tail

        # a secret in the tail must never reach distill() unmasked
        with open(raw + ".secret", "w") as fh:
            fh.write("\n".join([
                turn("user", "EARLY well over forty characters so it is not dropped as noise here", t0),
                turn("assistant",
                     "sk-ant-api03-" + "x" * 95 + " this leaks an anthropic key in the tail text",
                     t2),
            ]) + "\n")
        leaked = D.tail_since(raw + ".secret", since)
        assert leaked is None or "sk-ant-api03-" not in leaked["text"], \
            "an unmasked secret in a continuation tail was handed to distill()"

        done = os.path.join(d, ".done")
        os.makedirs(done)
        rec4 = {"session_id": "grows", "agent": "hermes", "project": "p", "abs_path": raw,
                "n_msg": 2, "first_ts": t0, "last_ts": t1,
                "text": "**Me**: EARLY-TURN-ONE\n\n**Hermes**: EARLY-TURN-TWO"}
        with mock.patch.object(D, "DONE", done), mock.patch.object(D, "OUT", d):
            D.mark_done(rec4, [("original-page", "Original", "decision", "grows")])
            with open(os.path.join(d, "original-page.md"), "w") as fh:
                fh.write("---\ntitle: \"Original\"\nsession_id: grows\nsession_agent: hermes\n---\nbody\n")

            grown_rec = {**rec4, "last_ts": t3}
            cands = D.continuation_candidates([grown_rec])
            assert len(cands) == 1 and cands[0][0]["session_id"] == "grows", cands

            cont_doc = lambda *a, **k: ("<<<DOC>>>\ntitle: Late turns\ndoc_type: decision\n"
                                        "why_captured: w\ntags: a\n---\nlate body\n<<<END>>>")
            with mock.patch.object(D, "call", cont_doc):
                docs, status = D.distill({"text": tail["text"], "n_msg": tail["n_msg"]})
            assert status == "ok" and docs, (status, docs)
            seen = {"original-page": 1}
            made = D.write(grown_rec, docs, seen, continues="original-page")
            assert made and made[0][0] == "late-turns-continued", made
            cont_path = os.path.join(d, made[0][0] + ".md")
            head = open(cont_path).read()
            assert "(continued)" in head and "continues: original-page" in head, head
            D.mark_continued(grown_rec, made)
            info = D.marker_info(os.path.join(done, "hermes-grows"))
            assert info == {"last_ts": t3, "pages": ["original-page", "late-turns-continued"]}, \
                f"the marker must gain the new page, keeping the original — not replace it: {info}"
            #  the original page's own file is untouched (§4.1 — never rewritten)
            assert open(os.path.join(d, "original-page.md")).read() == \
                "---\ntitle: \"Original\"\nsession_id: grows\nsession_agent: hermes\n---\nbody\n"
    print("  ✅ continuation pages —— tail_since cuts exactly at the marker, masks a secret in the tail, "
          "the new page never replaces the original, and the marker gains a page rather than losing one")

    #  ── exclude.txt at distil time, not only at collection (2026-09-26) ────────────────────
    #  `just distill`, the web screen's distil step and rebuild_all.sh start from the corpus as it
    #  was collected; a line added since used to be distilled, promoted and indexed anyway.
    with tempfile.TemporaryDirectory() as d:
        corp = [(a, os.path.join(d, f"{a}.json")) for a in ("claude", "codex", "hermes")]
        rows = {"claude": [{"session_id": "c1", "project": "p"}, {"session_id": "c2", "project": "p"}],
                "codex": [{"session_id": "x1", "project": "p", "agent": "codex"}],
                "hermes": [{"session_id": "h1", "project": "p", "agent": "hermes", "members": ["h1"]},
                           {"session_id": "h2", "project": "p", "agent": "hermes",
                            "members": ["h2", "h2-cont"]}]}
        at = time.time() - 3600
        for a, p in corp:
            with open(p, "w") as fh:
                json.dump(rows[a], fh)
            os.utime(p, (at, at))
        done = os.path.join(d, ".done")
        os.makedirs(done)
        excl = os.path.join(d, "exclude.txt")

        def listing(text, when):
            with open(excl, "w") as fh:
                fh.write(text)
            os.utime(excl, (when, when))

        def stops(needle):
            try:
                D.corpus_records()
            except SystemExit as e:
                assert needle in str(e), f"the stop does not say {needle!r}: {e}"
                return
            raise AssertionError(f"distil went ahead where it should have stopped ({needle!r})")

        with mock.patch.object(D, "CORPORA", corp), mock.patch.object(D, "DONE", done), \
                mock.patch.object(D, "OUT", d), mock.patch.object(I, "EXCLUDE", excl), \
                contextlib.redirect_stdout(io.StringIO()):
            #  listed after the last collection: a Claude id (in capitals), and a *later* session of
            #  a stitched Hermes conversation —— which takes the whole conversation with it.  The
            #  third id is in no corpus here (another machine's, or filtered at collection): distil is
            #  not the place to judge that —— it may not see every collector —— so it is let be.
            listing("C2\nh2-cont\nnot-in-any-corpus\n", at + 60)
            recs, loaded = D.corpus_records()
            assert sorted(r["session_id"] for r in recs) == ["c1", "h1", "x1"], \
                f"exclude.txt was not applied at distil time: {sorted(r['session_id'] for r in recs)}"
            assert [g for *_, g in loaded] == [1, 0, 1], f"the left-out counts are wrong: {loaded}"
            #  a cutoff the corpus predates cannot be applied here —— no message times —— so stop
            listing("x1 2026-09-25T09:00:00Z\n", at + 60)
            stops("collect again first")
            n, why = estimate._count_distill()           # a time estimate reports it, not dies
            assert n == 0 and "newer than" in why, (n, why)
            #  …with the reason, where it sits on the stop's second line: the first alone is
            #  "❌ <path>:" (review round 5).  `c` is only the start of c1 and c2.
            listing("c\n", at + 60)
            stops("only the start of session")
            n, why = estimate._count_distill()
            assert n == 0 and "only the start of session" in why, (n, why)
            #  …while one the corpus postdates was applied by the collector already
            listing("x1 2026-09-25T09:00:00Z\n", at - 60)
            assert "x1" in [r["session_id"] for r in D.corpus_records()[0]], "an applied cutoff dropped x1"
            #  a whole-session line for a conversation already distilled stops, pointing at its
            #  marker —— found through the later session's first one
            open(os.path.join(done, "hermes-h2"), "w").close()
            listing("h2-cont\n", at + 60)
            stops(os.path.join(done, "hermes-h2"))
    print("  ✅ exclude.txt applies at distil time: whole lines drop (any session of a stitched "
          "conversation), a cutoff the corpus predates stops the run, a distilled one points at "
          "its marker")

    #  ── the third-party gate's refusal ends the run, loudly (impl review round 2) ───────────
    #  The real gate, end to end: other people's text marked as collected, no no-tools receipt,
    #  no relay, and no `claude` on PATH —— so nothing here can reach a model, whatever happens.
    with tempfile.TemporaryDirectory() as d:
        home = os.path.join(d, "kal")
        os.makedirs(os.path.join(home, "sessions"))
        os.makedirs(os.path.join(home, "checks"))
        with open(os.path.join(home, "sessions", "session_docs.json"), "w") as fh:
            json.dump([{"session_id": "s1", "project": "p", "n_msg": 2,
                        "text": "**Me**: a question\n\n**Claude**: an answer"}], fh)
        open(os.path.join(home, "checks", "third-party-corpus"), "w").close()
        env = {k: v for k, v in os.environ.items()
               if k not in ("KAL_CLAUDE_RELAY", "KAL_DISTILLED", "KAL_SESSIONS")}
        env.update(KAL_HOME=home, PATH="/usr/bin:/bin")
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "--workers", "1"],
                           env=env, capture_output=True, text=True, timeout=120)
        out = r.stdout + r.stderr
        assert r.returncode != 0 and "verify-extract-tools" in out, \
            f"the gate's refusal was swallowed (exit {r.returncode}):\n{out}"
        assert "simply run again" not in out, "the refusal was reported as a retry"
        assert not os.listdir(os.path.join(home, "distilled", ".done")), "a refused session was marked done"
        runs = os.path.join(home, "runs")
        recorded = [json.load(open(os.path.join(runs, f))) for f in os.listdir(runs)]
        assert recorded and all(x.get("status") == "failed" for x in recorded), \
            f"the refused run was not recorded as failed: {recorded}"
    print("  ✅ the third-party gate's refusal stops distillation: non-zero exit, its message, no "
          "marker, recorded failed")

    #  ── the corpora follow the agents the index knows ────────────────────────────────────
    #  CORPORA names the agents a second time; `schema_v3.SESSION_AGENTS` is the list the index
    #  classifies provenance by.  A corpus whose agent the index does not know is indexed as a
    #  **vault** note, silently; an agent the index knows with no corpus here is never distilled.
    #  Compared rather than derived, so distilling does not import lancedb.
    from schema_v3 import SESSION_AGENTS
    assert {a for a, _ in CORPORA} == set(SESSION_AGENTS), \
        f"CORPORA {[a for a, _ in CORPORA]} and schema_v3.SESSION_AGENTS {list(SESSION_AGENTS)} differ"
    #  …and the Hermes corpus is the file its ingester writes.  A missing corpus reads as "not
    #  installed" (see CORPORA), so a drifted name would mean zero Hermes sessions, forever, quietly.
    import ingest_hermes_sessions
    assert os.path.basename(dict(CORPORA)["hermes"]) == ingest_hermes_sessions.DOCS, \
        "distill reads a different Hermes file from the one ingest_hermes_sessions writes"
    print("  ✅ CORPORA matches schema_v3.SESSION_AGENTS, and the Hermes path matches its ingester")

    print("  ✅ prompt contract —— supersession · closed threads only · correction is a document")

    print("  ✅ early abort —— a trailing window, so a deteriorating run is caught while a\n          recovered one finishes")
    print("  ✅ distill_sessions —— slug collisions · a partial window failure is not finalised")


def _backfill_ledger(recs):
    """Sessions marked done locally but absent from the synced ledger (its file was reset to a
    remote that lacks them) get their row back, from the marker — without it other devices would
    distil them again.  Only markers that recorded how far they reached (`last_ts`)."""
    staged = ledger.pending_keys()        # rows an interrupted sync already staged are not missing
    rows = []
    for r in recs:
        m = _marker_of(r)
        info = marker_info(m) if m else None
        key = (f"{r.get('agent', 'claude')}_session", r["session_id"])
        if info and info.get("last_ts") and _ledger_row(r) is None and key not in staged:
            rows.append(_ledger_row_of({**r, "last_ts": info["last_ts"]}, (), pages=info.get("pages") or []))
    try:
        ledger.append_many(rows)
    except Exception:
        if os.environ.get("KAL_LEDGER_STAGE"):
            raise


def _main_distill():
    global _LEDGER_INDEX
    _LEDGER_INDEX = None
    try:
        _LEDGER_INDEX = ledger.latest_rows()
    except Exception as error:
        _ledger_unreadable(error)
    try:
        _run_distill()
    finally:
        _LEDGER_INDEX = None


def _run_distill():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--restart", action="store_true",
                    help="distil every session again —— the new pages are written beside the old ones, "
                         "which stay (see is_done)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest(); return

    os.makedirs(OUT, exist_ok=True)
    os.makedirs(DONE, exist_ok=True)
    os.chmod(OUT, 0o700)
    recs, loaded = corpus_records()
    for _agent, _n, _file, _gone in loaded:
        print(f"  {_agent}: {_n} session(s) from {_file}"
              + (f" · {_gone} left out by exclude.txt" if _gone else ""))
    if not recs:
        #  A device may hold no agent session corpus at all (a fresh sync device; github_sync.run_distill sets KAL_DEVICE): nothing to distil
        #  is a no-op, not a failure — but a plain local run still says "run the collectors first".
        sync = bool(os.environ.get("KAL_LEDGER_STAGE") or os.environ.get("KAL_DEVICE"))
        print("no session corpus found — run ingest_sessions.py / ingest_codex_sessions.py / "
              "ingest_hermes_sessions.py first", file=sys.stderr)
        raise SystemExit(0 if sync else 1)
    if EXCLUDE_PROJECTS:
        n0 = len(recs)
        recs = [r for r in recs if wanted(r)]
        if n0 != len(recs):
            print(f"excluded — skipped {n0-len(recs)} session(s) from {'/'.join(EXCLUDE_PROJECTS)}")
    try:
        privacy = privacy_snapshot()
    except ValueError as error:
        raise SystemExit(str(error)) from None
    carry_privacy(privacy)
    n0 = len(recs)
    recs = [r for r in recs if not (no_llm_of_pages((), r, privacy) or _ledger_no_llm(r))]
    if n0 != len(recs):
        print(f"privacy: skipped {n0 - len(recs)} no_llm session(s) before model calls")
    if a.limit:
        recs = recs[:a.limit]
    # Settle rule (§4.2) — a session still being talked to is not a candidate yet.  Checked before
    # the resume/continuation split below so neither one ever acts on a still-growing session.
    n0 = len(recs)
    recs = [r for r in recs if settled(r)]
    if n0 != len(recs):
        print(f"ⓘ {n0 - len(recs)} session(s) skipped —— active within the last "
              f"{SETTLE_HOURS:g}h (settle rule; KAL_SETTLE_HOURS to change)")
    # Resume — sessions with a completion marker are skipped.  464 sessions × an LLM call
    # means starting over after an interruption is not affordable.
    continuations = []
    if not a.restart:
        _backfill_ledger(recs)
        n0 = len(recs)
        grown_recs = [r for r in recs if is_done(r) and settled(r) and grew(r)]
        continuations = continuation_candidates(grown_recs)
        recs = [r for r in recs if not is_done(r)]
        if n0 != len(recs):
            print(f"resuming — skipped {n0 - len(recs)} completed")
        if grown_recs:
            unsliceable = len(grown_recs) - len(continuations)
            print(f"ⓘ {len(grown_recs)} of them grew since they were distilled —— {len(continuations)} "
                  "will get a continuation page for the turns after their last marker (§4.2; the "
                  "original pages are never rewritten)"
                  + (f", {unsliceable} could not be sliced (no abs_path or unreadable marker last_ts)"
                     if unsliceable else ""))
    if not recs and not continuations:
        # SystemExit("a string") prints that string to stderr and exits with **code 1**.
        # Having nothing to do is not a failure —— the web UI displayed this as "failed".
        print("all done (re-run with --restart)")
        raise SystemExit(0)
    print(f"{len(recs)} session(s) → distilled into brain-ingest documents (workers {a.workers}, {MODEL})")

    t0 = time.time()
    # Register the slugs a previous run created — so resuming does not overwrite the same names
    seen = {os.path.basename(f)[:-3]: 1
            for f in __import__("glob").glob(os.path.join(OUT, "*.md"))}
    #  …and every name a marker still lists, even if its page was removed by hand: reused, the exclusion
    #  check read two markers' records for one page, and the removed page's extraction came back under
    #  it as history (review round 6).
    for _m in (os.listdir(DONE) if os.path.isdir(DONE) else ()):
        seen.update((s, 1) for s in (marker_info(os.path.join(DONE, _m)) or {}).get("pages") or [])
    rows, done, empty, failed, aborted, recent, cancelled = [], 0, 0, 0, False, [], 0
    with cf.ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(distill, r): r for r in recs}
        for f in cf.as_completed(futs):
            r = futs[f]
            done += 1
            try:
                docs, status = f.result()
            except ThirdPartyGateError:
                #  ⚠ The one refusal that is not a retry.  Counted as a failure, it printed "simply
                #     run again", exited 0 and recorded the run as ok —— while nothing could pass
                #     until the no-tools receipt is renewed (impl review round 2).  Stop the queue
                #     and let it out.
                for fut in futs:
                    fut.cancel()
                raise
            except Exception:
                docs, status = [], "fail"
            made = write(r, docs, seen, privacy=privacy) if status == "ok" else []
            rows += made
            if status == "skip":
                empty += 1
            if status != "fail":             # a failure leaves no marker → the next run picks it up again
                mark_done(r, made)
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

    # ── continuation pages (§4.2) — a session that grew after it was already distilled ─────────
    # Never rewrites the original page(s); each one gets a NEW page holding only the turns after
    # its marker's last_ts, linked back with `continues:` (write()) and the marker is extended,
    # not replaced (mark_continued()).  Sequential, not thread-pooled: there are normally far
    # fewer of these than fresh sessions, and each one calls tail_since() (a file read) before
    # the LLM call, which the resume-safe ThreadPoolExecutor path above was not built to interleave.
    cont_made = cont_failed = cont_empty = 0
    for r, since in continuations:
        try:
            tail = continuation_tail(r, since)
        except ThirdPartyGateError:
            raise
        except Exception as error:
            print(f"continuation read failed ({r.get('agent', 'claude')}): {type(error).__name__}")
            cont_failed += 1
            continue          # nothing new to say, or the tail could not be read/masked safely
        cont_rec = {**r, "text": tail["text"], "n_msg": tail["n_msg"],
                    "first_ts": tail["last_ts"] or r.get("first_ts")}
        try:
            docs, status = distill(cont_rec)
        except ThirdPartyGateError:
            raise
        except Exception:
            docs, status = [], "fail"
        if status == "fail":
            cont_failed += 1
            continue          # no marker update — the next run's continuation_candidates() retries it
        if status == "skip":
            cont_empty += 1
            mark_continued(r, [])
            continue
        prior = _prior_pages(r)
        no_llm = no_llm_of_pages(prior, r, privacy) or _ledger_no_llm(r)
        made = write(r, docs, seen, continues=(prior or [None])[0],
                     no_llm=no_llm, privacy=privacy)
        rows += made
        cont_made += len(made)
        mark_continued(r, made)
    if continuations:
        print(f"  continuations: {cont_made} page(s) from {len(continuations)} grown session(s) "
              f"· {cont_empty} small-talk skip(s) · {cont_failed} failure(s)")

    # The review list is built from **the real files on disk**.  Built from the in-memory rows,
    # a resumed run would hold only this round's documents and drop the earlier ones.
    import glob as _g
    allrows = []
    for f in sorted(_g.glob(os.path.join(OUT, "*.md"))):
        with open(f, encoding="utf-8") as page:
            head = page.read(1500)
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
    if failed or cont_failed:
        raise SystemExit(f"distillation incomplete: {failed} session(s) and {cont_failed} continuation(s) failed; "
                         "successful completion markers were retained; retry to process only unfinished work")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        #  A self-check is not a run.  Recorded, every `just selftest` put a successful "distill"
        #  into the real ~/.kal/runs —— the history the web screen reads (audit 2026-09-26).
        _selftest()
        sys.exit(0)
    # Record the run under ~/.kal/runs/ —— the web screen's "last run" only knew about runs
    # started from the web UI, so a CLI success still showed yesterday's failure as the last.
    from run_log import record
    try:
        with record("distill"):                # records the refusal as a failed run, then re-raises
            _main_distill()
    except ThirdPartyGateError as e:
        print(f"❌ {e}")
        sys.exit(1)
