#!/usr/bin/env python3
"""Fill the OKF extension keys a converted page is missing —— with an LLM, one call per page.

Why it is a separate step
  `openwiki_emit.py` converts.  It carries a key across when the source had one and **never
  invents metadata**, which is right: a vault note that says nothing about why it was kept should
  not have a sentence made up for it and filed as the author's.  The consequence is measurable ——
  of the 96 pages migrated from the Obsidian vault, 17% had `doc_type` and 28% `why_captured`,
  against 100% for the 1,017 written by `distill_sessions` (measured 2026-09-02).

  So the choice is not "invent or not" but **who says it**.  This step says it out loud: an LLM
  read the page and proposed the value, and `filled_by` records that on the page itself.  A person
  can then correct or delete it, which they cannot do with a value that was silently absent.

  It is separate from the emitter for the same reason the two halves of the pipeline are separate:
  conversion is pure and takes seconds, this needs an LLM and can be stopped by a rate limit.  A
  rate limit must not stop a conversion that never needed the network.

What it will not do
  · It never sends a page the transmission gate blocks (`no_llm`), and never sends `references/`.
  · It never overwrites a key that is already there —— including one a person wrote.
  · It only accepts a `doc_type` from the vocabulary; anything else is dropped, not written.
  · It refuses to run outside a git repository, because it edits pages in place.

Usage
  python openwiki_enrich.py --wiki <bundle>            # fill what is missing
  python openwiki_enrich.py --wiki <bundle> --dry-run  # report, write nothing
  python openwiki_enrich.py --selftest
"""
import os
import re
import sys
import json
import glob
import time
import hashlib
import argparse
import concurrent.futures as cf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from openwiki_emit import _fm_block, git_ok                      # noqa: E402
from distill_sessions import DOC_TYPES                           # noqa: E402
from schema_v3 import NO_LLM_RE                                  # noqa: E402
from claude_cli import run as claude_run                         # noqa: E402

KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
CACHE = os.path.join(KAL_HOME, "openwiki_enrich_cache.jsonl")
MODEL = os.environ.get("KAL_ENRICH_MODEL", "haiku")
#  ⚠ Bump this when PROMPT changes.  The cache key carries it, so old answers are not reused
#     against a new question —— the mistake `lr_extract` made once and now guards the same way.
PROMPT_VERSION = "e1"
WORKERS = int(os.environ.get("KAL_ENRICH_WORKERS", "4"))
FILL = ("doc_type", "why_captured", "description")
BODY_CHARS = 6000

PROMPT = """You read one page from a personal knowledge wiki and propose the metadata it is missing.

Output **exactly one JSON object** and nothing else:

{"doc_type": "...", "why_captured": "...", "description": "..."}

doc_type      One of exactly these, and nothing else:
              plan · analysis · design · discussion · decision · retro · investigation · correction
              Choose by what the page **is**, not what it is about:
                analysis      it works something out and states what it found
                investigation it chases a question, and the chase is the content
                decision      a choice was made, and the page records it and why
                design        it describes how something is or should be built
                plan          it lays out work to be done
                retro         it looks back at something that happened
                discussion    it weighs options without settling them
                correction    it records that something believed was overturned
              If none fits, use "analysis".

why_captured  One sentence, in the page's own language, saying **why keeping this is worth it** ——
              what a reader would lose if it were deleted.  Not a summary.  Not "for reference".
              Name the specific thing: the mistake it prevents, the decision it explains, the
              measurement it holds.

description   One sentence, in the page's own language, saying what the page contains.  Under 200
              characters.  No "This page describes"; just say the thing.

Write the two sentences in the same language the page is written in.
Output nothing but the JSON object.

--- page ---
"""


def missing(fm_block):
    """Which of FILL this page does not already have.  A key a person wrote is never touched."""
    return [k for k in FILL if not re.search(rf"^{k}:", fm_block, re.M)]


def blocked(fm_block):
    """Does the transmission gate stop this page from being sent."""
    return bool(NO_LLM_RE.search(fm_block))


def body_of(text):
    m = re.match(r"\A﻿?\s*---[ \t]*\r?\n.*?\r?\n(?:---|\.\.\.)[ \t]*\r?\n", text, re.S)
    return (text[m.end():] if m else text)[:BODY_CHARS]


def key_of(path, text):
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"{os.path.basename(path)}|{h}|{PROMPT_VERSION}|{MODEL}"


def parse_reply(raw):
    """The JSON object out of the reply, or None.  A doc_type outside the vocabulary is dropped."""
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    out = {}
    dt = str(d.get("doc_type", "")).strip().strip('"\'').lower()
    #  ⚠ **The vocabulary is checked, not trusted.**  `doc_type` lands in a BITMAP column that the
    #     indexer reads with `(\S+)`; a value nobody filters on is a column that silently matches
    #     nothing, which is worse than an absent key because it looks filled.
    if dt in DOC_TYPES:
        out["doc_type"] = dt
    for k in ("why_captured", "description"):
        v = str(d.get(k, "") or "").strip().replace("\n", " ")
        if v:
            out[k] = v[:400]
    return out or None


def insert_keys(text, vals):
    """Put the keys into the frontmatter, after `type:` when there is one.

    ⚠ `doc_type` goes in **unquoted** and near the head.  The indexer reads it with
       `re.search(r"^doc_type:\\s*(\\S+)", raw[:1200])` —— quotes are swallowed into the value and
       the filter then matches zero rows, and a key past 1,200 characters is not seen at all.
       The other two are quoted, because a sentence with a colon in it is not valid bare YAML.
    """
    m = re.match(r"\A(﻿?\s*---[ \t]*\r?\n)(.*?)(\r?\n(?:---|\.\.\.)[ \t]*\r?\n)", text, re.S)
    if not m:
        return None
    head, fm, tail = m.group(1), m.group(2), m.group(3)
    lines = fm.split("\n")
    at = next((i + 1 for i, l in enumerate(lines) if l.startswith("type:")), len(lines))
    add = []
    if "doc_type" in vals:
        add.append(f'doc_type: {vals["doc_type"]}')
    for k in ("description", "why_captured"):
        if k in vals:
            add.append('%s: "%s"' % (k, vals[k].replace('"', "'")))
    if not add:
        return None
    add.append('filled_by: "process:openwiki_enrich.py/%s"' % PROMPT_VERSION)
    lines[at:at] = add
    return head + "\n".join(lines) + tail + text[m.end():]


def load_cache():
    out = {}
    if os.path.exists(CACHE):
        for line in open(CACHE, encoding="utf-8"):
            try:
                r = json.loads(line)
                out[r["k"]] = r["v"]
            except Exception:
                pass
    return out


def pages(wiki):
    out = []
    for root, dirs, files in os.walk(wiki):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "references"]
        for f in files:
            if f.endswith(".md") and f != "index.md":
                out.append(os.path.join(root, f))
    return sorted(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--wiki", default=os.environ.get(
        "OPENWIKI_DIR", os.path.expanduser("~/github/HwangTaehyun/openwiki")))
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()

    a.wiki = os.path.expanduser(a.wiki)
    if not git_ok(a.wiki):
        print(f"❌ the bundle is not a git repository: {a.wiki}\n"
              f"   This edits pages in place, and `git revert` is the only way back.")
        return 1

    todo, gated, complete = [], 0, 0
    for p in pages(a.wiki):
        blk = _fm_block(p)
        if not blk:
            continue                        # no frontmatter — not an OKF page, leave it alone
        if blocked(blk):
            gated += 1
            continue
        need = missing(blk)
        if need:
            todo.append((p, need))
        else:
            complete += 1
    print(f"  {len(todo)} page(s) missing metadata · {complete} already complete · "
          f"{gated} held back by the transmission gate")
    if not todo:
        return 0
    if a.limit:
        todo = todo[:a.limit]

    cache = load_cache()
    hit = sum(1 for p, _ in todo if key_of(p, open(p, encoding="utf-8").read()) in cache)
    print(f"  {hit} cached · {len(todo) - hit} to ask ({MODEL}, {a.workers} workers)")
    if a.dry_run:
        for p, need in todo[:5]:
            print(f"      {os.path.relpath(p, a.wiki)}  ← {', '.join(need)}")
        print("  (dry run)")
        return 0

    os.makedirs(KAL_HOME, exist_ok=True)
    fh = open(CACHE, "a", encoding="utf-8")
    t0, done, wrote, failed = time.time(), 0, 0, 0

    def ask(item):
        p, need = item
        text = open(p, encoding="utf-8", errors="replace").read()
        k = key_of(p, text)
        if k in cache:
            return p, need, cache[k], True
        raw = claude_run(MODEL, PROMPT + body_of(text), timeout=120)
        return p, need, parse_reply(raw), False

    with cf.ThreadPoolExecutor(a.workers) as ex:
        for f in cf.as_completed({ex.submit(ask, it): it for it in todo}):
            p, need, vals, cached = f.result()
            done += 1
            if not vals:
                failed += 1
            else:
                if not cached:
                    fh.write(json.dumps({"k": key_of(p, open(p, encoding="utf-8").read()),
                                         "v": vals}, ensure_ascii=False) + "\n")
                    fh.flush()
                keep = {k: v for k, v in vals.items() if k in need}
                text = open(p, encoding="utf-8", errors="replace").read()
                new = insert_keys(text, keep) if keep else None
                if new:
                    open(p, "w", encoding="utf-8").write(new)
                    wrote += 1
            if done % 20 == 0 or done == len(todo):
                print(f"    {done}/{len(todo)}  {wrote} written · {failed} failed  "
                      f"{(time.time()-t0)/60:.1f} min")
    fh.close()
    print(f"  ✅ {wrote} page(s) filled · {failed} failed · {(time.time()-t0)/60:.1f} min")
    return 0


def _selftest():
    import tempfile
    import shutil
    ok = []
    d = tempfile.mkdtemp()
    try:
        # ① missing() sees only what is absent, and never reports a key a person wrote
        assert missing('title: "a"\ntype: note\n') == list(FILL)
        assert missing('doc_type: analysis\ndescription: "x"\nwhy_captured: "y"\n') == []
        assert missing('doc_type: analysis\n') == ["why_captured", "description"]
        ok.append("missing() reports absent keys only")

        # ② the transmission gate is read through the one shared regex —— including the comment
        #    form that used to open it silently
        assert blocked("no_llm: true\n")
        assert blocked("no_llm: true # private\n")
        assert not blocked("no_llm: false\n")
        assert not blocked('title: "no_llm is discussed here"\n')
        ok.append("a gated page is never sent (comment form included)")

        # ③ a doc_type outside the vocabulary is **dropped, not written**.  A value nobody filters
        #    on looks filled and matches nothing.
        assert parse_reply('{"doc_type":"analysis","why_captured":"w","description":"d"}')["doc_type"] == "analysis"
        assert "doc_type" not in (parse_reply('{"doc_type":"memo","description":"d"}') or {})
        assert parse_reply("not json at all") is None
        assert parse_reply("") is None
        assert parse_reply('prose {"doc_type":"retro"} more') == {"doc_type": "retro"}
        ok.append("only vocabulary doc_types are written; junk replies are dropped")

        # ④ what is written has to survive the indexer.  doc_type bare, inside the first 1,200
        #    characters, and the file still parses as frontmatter.
        page = '---\ntitle: "a"\ntype: note\n---\n본문\n'
        got = insert_keys(page, {"doc_type": "analysis", "why_captured": "이유", "description": "설명"})
        assert re.search(r"^doc_type: analysis$", got, re.M), got
        assert got.index("doc_type") < 1200
        import schema_v3 as S
        assert re.search(r"^doc_type:[ \t]*(\S+)", got[:1200], re.M).group(1) == "analysis", \
            "the indexer's own regex did not read it back bare"
        assert S.doc_meta(got)[2] is False, "an unrelated page must not become gated"
        assert 'filled_by: "process:openwiki_enrich.py/' in got, "provenance was not recorded"
        ok.append("written keys read back the way the indexer reads them (bare · in the window)")

        # ⑤ a page with no frontmatter is left alone rather than corrupted
        assert insert_keys("no frontmatter here\n", {"doc_type": "analysis"}) is None
        ok.append("a page without frontmatter is not touched")

        # ⑥ the cache key moves when the prompt version, the model or the content moves
        t1, t2 = "aaa", "aab"
        assert key_of("/x/a.md", t1) != key_of("/x/a.md", t2)
        assert key_of("/x/a.md", t1) == key_of("/x/a.md", t1)
        assert PROMPT_VERSION in key_of("/x/a.md", t1) and MODEL in key_of("/x/a.md", t1)
        ok.append("the cache key carries content · prompt version · model")

        # ⑦ pages() skips index.md, references/ and dot-directories
        for sub in ("", "references", ".obsidian", "personal"):
            os.makedirs(os.path.join(d, sub), exist_ok=True)
        for rel in ("a.md", "index.md", "references/r.md", ".obsidian/o.md", "personal/p.md"):
            open(os.path.join(d, rel), "w").write("x")
        got = {os.path.relpath(p, d) for p in pages(d)}
        assert got == {"a.md", "personal/p.md"}, got
        ok.append("index.md, references/ and dot-directories are never sent")
    finally:
        shutil.rmtree(d, ignore_errors=True)
    for line in ok:
        print(f"  ✅ {line}")
    print(f"  ✅ openwiki_enrich —— {len(ok)} self-check(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
