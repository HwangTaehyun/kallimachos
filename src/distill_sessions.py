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
SESS = os.path.join(KAL_HOME, "sessions/session_docs.json")
OUT = os.environ.get("KAL_DISTILLED", os.path.join(KAL_HOME, "distilled"))          # built outside the vault first
# ⚠ REVIEW and DONE **must follow OUT.**  They used to be hardcoded to KAL_HOME, so an attempt
# to test safely with `KAL_DISTILLED=/tmp/probe` still wrote the review list and the completion
# markers **to the real path**.  That is how a 401-row _distill_review.tsv was overwritten with
# 6 rows (2026-08-19).  Change the output path and the by-products have to follow it.
REVIEW = os.path.join(OUT, "_distill_review.tsv")
DONE = os.path.join(OUT, ".done")   # completion markers, for resuming
MODEL = "haiku"
WINDOW = 55_000            # the most characters put into one call
MAX_PARTS = 8              # even a long session splits into at most 8 — beyond that, head and tail first
SPLIT = "---8<---"         # the separator the LLM uses when splitting topics
DOC_TYPES = ("plan", "analysis", "design", "discussion", "decision", "retro", "investigation")

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
PROMPT = """You distil Claude Code conversation logs into raw/conversations/ documents for a
personal wiki.  The rules below are the wiki's ingest protocol; breaking them means the document is rejected.

Forbidden
- Do not carry over speech transcripts such as "Me:" / "Claude:".
- Do not carry over tool-call logs, command output dumps, or progress narration.
- Do not mix different topics into one document.

Required
- Keep conclusions and decisions only.  Include process only as far as the conclusion needs it.
- State the rationale for each decision — in the form "chose A over B.  Reason: …".
- One top-level summary paragraph at the very top (3–5 sentences).  Reading only that should say what was decided.
- Keep concrete numbers, paths and commands.  That is where this document's value is.
- Write in the language of the conversation.

Output format — exactly this format, nothing else:

<<<DOC>>>
title: <a specific title.  Do not include a date or session ID>
doc_type: <one of plan|analysis|design|discussion|decision|retro|investigation>
why_captured: <one sentence on why a future me would look this up again>
tags: <3–6 comma-separated lowercase kebab-case tags>
---
<body markdown.  Use ## sections.  400–1500 words.>
<<<END>>>

If the conversation covered several topics, repeat the block above once per topic (at most 3).
If there is no content, or only small talk, output exactly the single word SKIP.

--- conversation log begins ---
{body}
--- conversation log ends ---"""

MERGE = """Below are documents distilled separately from pieces of one long Claude Code conversation.
They are the same conversation, so merge them into one, remove duplication and put them in time order.
If the topics are plainly different, split by topic, but into at most 3.

The output format is the same <<<DOC>>> … <<<END>>> blocks as the input.  Nothing else.

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
    recs = json.load(open(SESS))
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
        recs = [r for r in recs if not os.path.exists(os.path.join(DONE, r["session_id"]))]
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
    rows, done, empty, failed = [], 0, 0, 0
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
            else:
                failed += 1
            if status != "fail":             # a failure leaves no marker → the next run picks it up again
                open(os.path.join(DONE, r["session_id"]), "w").close()
            if done % 20 == 0 or done == len(recs):
                el = time.time() - t0
                print(f"  {done}/{len(recs)}  {len(rows)} document(s) · {empty} small-talk skip(s) · "
                      f"{failed} failure(s)  {el/60:.1f} min "
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
