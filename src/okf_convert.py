#!/usr/bin/env python3
"""Convert an LLM-Wiki vault into an **OKF v0.2 bundle**.

What changes
  ① frontmatter  our vocabulary → OKF's (type/title/description/tags/sources/generated/status)
  ② links        [[wikilink]] → [label](/bundle/relative/path.md)
  ③ reserved files  index.md (declaring okf_version) · log.md

What does not change —— **the body prose**.  Only the link syntax is rewritten:
  Measured (376 documents): 99 documents change body text, 32,523 characters = 2.39% of the body.
  All of it is `[[x]]` → `[x](/path.md)` substitution; the prose itself is untouched.
  So this conversion's effect has a ceiling.  Measured (2026-08-19, 376 documents):
      frontmatter   170,222 characters (11.1%)
      body        1,360,390 characters (88.9%)
  Knowledge-graph extraction reads the body, so this conversion alone barely moves the KG.
  The real difference comes from turning "one page = one session" into "one page = one concept".
  That is not a conversion but **a regeneration from the sessions** (okf_regen.py).

  Chunk boundaries do shift by the frontmatter's length, so the extraction cache misses 100%.
  That is: **the cost is a full re-extraction and the signal is 11%.**  Comparative experiments belong on a sample.

OKF grounds
  https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md
  v0.2 · pub. date unverified · retrieved 2026-08-19
  · only type is required.  Everything else is optional and a consumer must not reject its absence (§11)
  · links are standard markdown.  **The kind of relation is carried in prose** —— the spec gives no types
  · the path field accepts absolute URLs, bundle-relative (leading /) and relative paths (§6.2)

Usage:
    python okf_convert.py                          # everything → ~/.kal/exp/okf-vault
    python okf_convert.py --out DIR --only N        # N documents only (for sampling experiments)
    python okf_convert.py --selftest
"""
import vault_path
import argparse
import datetime
import glob
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
VAULT = vault_path.vault()
OKF_VERSION = "0.2"

# Documents are selected by **the same rule** as indexing and extraction.  Change one side and the comparison stops holding.
from schema_v3 import is_skipped, clean as index_clean  # noqa: E402

# Our type → an OKF type.  OKF asks only for "a short string denoting the concept kind" (§4).
# A capitalised noun phrase is the idiom of the spec's examples ("BigQuery Table", "Metric").
TYPE_MAP = {
    "conversation": "Session Record",
    "concept": "Concept",
    "entity": "Entity",
    "source": "Source Summary",
    "topic": "Topic",
    "transcript": "Transcript",
    "article": "Article",
    "readme": "Readme",
    "overview": "Overview",
}
# maturity → OKF status (§5.4: draft | stable | deprecated)
STATUS_MAP = {"seedling": "draft", "budding": "draft", "evergreen": "stable"}


def parse_fm(text):
    """(frontmatter dict, body).  Why no YAML parser —— many values are one-line strings mixing
    colons and quotes, and they break on a round trip.  What we use is only `key: scalar` and
    `key: [a, b]`, so only those two are read."""
    m = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}, text
    fm, body = {}, text[m.end():]
    for line in m.group(1).splitlines():
        k = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if not k:
            continue
        key, val = k.group(1), k.group(2).strip()
        if val.startswith("[") and val.endswith("]"):
            fm[key] = [x.strip().strip('"\'') for x in val[1:-1].split(",") if x.strip()]
        else:
            fm[key] = val.strip('"\'')
    return fm, body


def first_sentence(body, limit=200):
    """An OKF description is 'a one-sentence summary' (§4).  One sentence is taken from the body's first paragraph."""
    t = re.sub(r"^#.*$", "", body, flags=re.M)          # drop heading lines
    # Bullets are followed by **whitespace**: `- `, `* `, `+ `.  Without requiring that space, a
    # paragraph starting in bold (`**Andrej Karpathy** proposed …`) is mistaken for a list and
    # deleted wholesale, so the description is taken from a list far below (measured 2026-08-19).
    t = re.sub(r"^\s*(?:[-*+]\s|\d+\.\s|[|>]).*$", "", t, flags=re.M)  # drop lists, tables and quotes
    t = re.sub(r"```.*?```", "", t, flags=re.S)         # drop code fences
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return ""
    # A minimum length of 10 characters skipped a 6-character first sentence and swallowed two.
    # A sentence can be that short (Korean especially).  In exchange, a full stop followed by a
    # digit is not treated as terminal, so decimals like 3.14 do not split.
    #   The self-check pins the lower bound with a 9-character sentence, so raising this back to
    #   10 fails there.  Mutation-verified 2026-09-01.
    m = re.search(r"^(.{4,%d}?[.。!?](?![\d]))(?=\s|$)" % limit, t)
    return (m.group(1) if m else t[:limit]).strip()


def yaml_str(s):
    """One-line scalars, safely.  Colons, quotes and # are common, so it is always double-quoted."""
    return '"%s"' % str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def under(path, root):
    """Is path root itself, or below it.

    A string prefix comparison (`ro.startswith(rv + os.sep)`) is **not enough.**  macOS's APFS
    ignores case while `realpath` preserves the case that was typed, so
    `--out .../MyVault/wiki` returns False even though it is under `--vault .../myvault`.
    In round 6 that one letter deleted real files.  It compares by inode.
    """
    try:
        st_root = os.stat(root)
    except OSError:
        return False
    cur = path
    while True:
        try:
            if os.path.samestat(os.stat(cur), st_root):
                return True
        except OSError:
            pass                # a path that does not exist yet —— keep walking up to the parent
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


def is_bundle(d):
    """Is this a bundle this converter made.  The only line of defence before rmtree'ing `--out`.

    Judging by file **name** is wrong —— the vault's `wiki/` also holds index.md and log.md and
    would pass.  Only `okf_version` inside the content is a marker unique to the converter.
    """
    idx = os.path.join(d, "index.md")
    if not (os.path.exists(idx) and os.path.exists(os.path.join(d, "log.md"))):
        return False
    try:
        head = open(idx, encoding="utf-8", errors="ignore").read(4096)
    except OSError:
        return False        # unreadable means "not a bundle" —— the safe direction (do not delete)
    # The marker counts **only inside the frontmatter**.  It has been pierced twice:
    #   round 5 —— `"okf_version" in head` passed on a line of prose.
    #   round 6 —— the `^okf_version:\s` anchor passed on an example inside a ```yaml fence.
    # `promote_distilled.py:40` auto-updates vault/wiki/index.md and this very document carries
    # that block as an example.  parse_fm never looks outside the frontmatter at all.
    return bool(parse_fm(head)[0].get("okf_version"))


def build_link_index(files, root):
    """An index for resolving [[targets]] into bundle paths.

    Obsidian links by filename alone (extension excluded).  When the same name exists in several
    folders Obsidian picks the nearest, while this picks **the shorter path** —— it has to be
    deterministic so two runs produce the same bundle."""
    idx = {}
    for f in files:
        rel = "/" + os.path.relpath(f, root).replace(os.sep, "/")
        stem = os.path.basename(f)[:-3]
        for key in (stem, stem.lower()):
            cur = idx.get(key)
            if cur is None or len(rel) < len(cur):
                idx[key] = rel
    return idx


WIKILINK = re.compile(r"\[\[([^\]\|#]+)(?:#([^\]\|]+))?(?:\|([^\]]+))?\]\]")


def convert_links(body, idx, unresolved):
    """[[target]] · [[target|label]] · [[target#anchor]] → [label](/path.md#anchor)

    An unresolved target stays a link too —— OKF states that a bundle must not be rejected for a
    broken cross-link (§11).  Deleting it would erase 'a signal a person put there'."""
    def sub(m):
        target, anchor, label = m.group(1).strip(), m.group(2), m.group(3)
        path = idx.get(target) or idx.get(target.lower())
        if path is None:
            unresolved.add(target)
            path = "/unresolved/%s.md" % re.sub(r"[^\w-]+", "-", target).strip("-").lower()
        frag = "#" + re.sub(r"\s+", "-", anchor.strip()) if anchor else ""
        return "[%s](%s%s)" % (label or target, path, frag)
    return WIKILINK.sub(sub, body)


#  The producer keys this converter may emit beyond OKF v0.2.
#
#  ⚠ Two different contracts live here.  The **vault experiment** (`--out ~/.kal/exp/okf-vault`)
#     keeps `origin` and `distilled_from` because they were already in that output.  The
#     **openwiki bundle** publishes exactly three, which is what its SPEC.md declares, so
#     `openwiki_emit.py` passes the strict tuple.  Neither is more correct in the abstract —— what
#     matters is that a bundle claiming "OKF plus three keys" does not quietly carry five.
#     `origin: claude-session` is fully derivable from `sources[].resource`'s scheme, and
#     `distilled_from` is already withheld from LLMs by `kal_mcp.DENY_FM`.
EXT_ALL = ("doc_type", "origin", "why_captured", "distilled_from")
EXT_OPENWIKI = ("doc_type", "why_captured")     # + `no_llm`, which is emitted at the head


def actor(by):
    """A `generated.by` / `verified[].by` value in OKF's actor convention (§7).

    > `<producer>/<version>` for agents and tools … `human:<id>` for a person …
    > `process:<id>` for an automated process.
    > Consumers that classify trust (§5.3) **key off the `human:` prefix**.
    > —— references/okf-SPEC-v0.2.md §7

    ⚠ This used to emit the bare script name (`distill_sessions.py`).  That is not any of the
       three shapes, so a consumer grading trust could not classify it —— and the one thing the
       spec makes mandatory is that human-authored content carry `human:`.  Emitting an
       unclassifiable actor for machine output is the safer direction of the two, but it is
       still wrong, and a bundle that says it targets 0.2 should not need excusing.
    """
    s = (by or "").split("(")[0].strip()
    if not s:
        return "process:unknown"
    if s.startswith(("human:", "process:")) or "/" in s:
        return s
    #  A .py file is a process this repository runs; anything else is left alone but prefixed,
    #  because an unprefixed value classifies as nothing at all.
    return "process:" + (s[:-3] if s.endswith(".py") else s)


def to_okf(fm, body, rel_path, idx, unresolved, extensions=EXT_ALL):
    """One document as OKF frontmatter plus a converted body."""
    out = []
    # ── Required ──
    raw_type = (fm.get("type") or "").strip().lower()
    out.append("type: %s" % yaml_str(TYPE_MAP.get(raw_type, raw_type.title() or "Document")))
    #  no_llm goes near the head of the frontmatter, and it is emitted **unquoted**.
    #
    #  ⚠ This comment used to cite `lr_extract.py:152`'s `NO_LLM_MARK` and a 1,200-character
    #     scan window.  Both were wrong, and the review that found it is the reason this note
    #     exists (2026-09-02, fact-checker + consistency lenses):
    #       · `lr_extract.py:152` is `MODEL = "haiku"`.  `NO_LLM_MARK` sits at :297 and is
    #         **never called** —— lr_extract gates at :351 on `doc_meta(t)[2]`, and its own
    #         comment at :345 says "the judgement happens in doc_meta and nowhere else".
    #       · `schema_v3.doc_meta` matches `no_llm` against the **whole** frontmatter block with
    #         no character limit.  Measured: a 4,500-character frontmatter with `no_llm` last
    #         still reads True.  The `raw[:1200]` window is real but belongs to **`doc_type`**
    #         (schema_v3.py:563) —— which this converter emits *last*.
    #     So head placement is defence in depth, not the load-bearing rule it claimed to be.
    #     What *is* load-bearing is the absence of quotes: `no_llm: "true"` fails the regex.
    if fm.get("no_llm"):
        out.append("no_llm: %s" % str(fm["no_llm"]).strip().strip('"\''))
    # ── Recommended ──
    if fm.get("title"):
        out.append("title: %s" % yaml_str(fm["title"]))
    desc = first_sentence(body)
    if desc:
        out.append("description: %s" % yaml_str(desc))
    #  ⚠ `resource` used to be `"/" + rel_path` —— the page pointing at **itself**.  §4.1:
    #     "`resource`: A URI that uniquely identifies the underlying asset the concept describes.
    #     **Absent for concepts that describe abstract ideas.**"  A distilled session document
    #     describes a conversation, and the conversation is already named in `sources[]`; the
    #     page's own path is not an asset it describes.  A self-reference is not merely useless,
    #     it tells a consumer the asset *is* the page, which defeats §5.1's provenance chain.
    #     Emitted only when the source document actually names an external asset.
    #     (deep review 2026-09-02, domain lens)
    ext_res = (fm.get("resource") or fm.get("source") or "").strip().strip("\"'")
    if ext_res:
        out.append("resource: %s" % yaml_str(ext_res))
    tags = fm.get("tags")
    if isinstance(tags, list) and tags:
        out.append("tags: [%s]" % ", ".join(yaml_str(t) for t in tags))

    # ── provenance (§5.1) ──
    srcs = []
    if fm.get("session_id"):
        sid = fm["session_id"]
        #  ⚠ The scheme used to be the literal `claude-session://`.  Once Codex sessions entered
        #     the same corpus (2026-09-02) that made every Codex document claim a Claude origin ——
        #     wrong provenance, and silently so, because the string is well-formed either way.
        #     `session_agent` is written by `distill_sessions.write()`; a document from before it
        #     existed has no such field and is Claude's, which is what the default says.
        agent = (fm.get("session_agent") or "claude").strip().lower()
        srcs.append({
            "id": "session-" + sid[:8],
            # An external URL works, and so does a bundle-relative path.  Session logs live outside the bundle, so a scheme is used.
            "resource": "%s-session://%s" % (agent, sid),
            "title": fm.get("session_project") or ("%s session" % agent),
            #  ⚠ `author` used to say `process:distill_sessions`.  OKF §5.1: "`author`: Who or
            #     what produced **the source**."  The source is the session —— produced by a person
            #     and an assistant —— while distill_sessions produced the *page*, which is already
            #     recorded in `generated.by`.  Writing the same value in both collapsed the
            #     distinction §5.1 exists to preserve ("judge a concept by judging its sources")
            #     and laundered the human origin out of the record.  Omitted rather than guessed.
            #  ⚠ `last_modified` is dropped for the same reason it was wrong: it was a copy of
            #     `captured`, which §5.1 says is "distinct from generated.at", and it was emitted
            #     date-only against §5's "explicit UTC offset" rule.  A session file's real mtime
            #     is available but is the mtime of a *log*, not of the source conversation.
            "last_modified": "",
        })
    # The vault's sources take the form `sN:slug` —— an id prefix `fm_migrate.py` added.
    # Nested YAML would be split on commas and broken by parse_fm, so the id rides as a prefix
    # inside a flat string and is separated here into OKF's {id, resource, title}.
    # Older documents without the prefix are accepted as they are.
    for s in (fm.get("sources") or []):
        if not (isinstance(s, str) and s):
            continue
        m = re.match(r"^(s\d+):(.+)$", s)
        sid, ref = (m.group(1), m.group(2)) if m else (s, s)
        srcs.append({"id": sid, "resource": "/wiki/sources/%s.md" % ref, "title": ref})
    if srcs:
        out.append("sources:")
        for s in srcs:
            out.append("  - id: %s" % yaml_str(s["id"]))
            for k in ("resource", "title", "author", "last_modified"):
                if s.get(k):
                    out.append("    %s: %s" % (k, yaml_str(s[k])))

    # ── trust (§5.2) ──
    # generated.at uses captured (a date) because we have no timestamp.  Absent, it is omitted ——
    # inventing one would make the staleness verdict false.
    # The formal fields come first.  `fm_migrate.py` planted generated_by/generated_at in the
    # vault (2026-08-19), and the older distilled_by/captured are a fallback for documents that
    # still carry them.  `created` is **the day a person made the document** and therefore not a
    # generation time, so it is used only as a last resort when there is no other clue.
    by = fm.get("generated_by") or fm.get("distilled_by")
    at = fm.get("generated_at") or fm.get("captured") or fm.get("created")
    if by or at:
        out.append("generated:")
        out.append("  by: %s" % yaml_str(actor(by)))
        if at:
            out.append("  at: %s" % yaml_str("%sT00:00:00Z" % at))
    # verified is **not written.**  This vault holds no human review record, and writing one
    # that does not exist would make OKF's whole trust grading false.

    # ── lifecycle (§5.4) ──
    st = STATUS_MAP.get((fm.get("maturity") or "").strip().lower())
    #  ⚠ **Omitting `status` does not mean "unknown".**  The spec is explicit:
    #     "Absent `status` ⇒ `stable`." (references/okf-SPEC-v0.2.md:422, pinned in the openwiki
    #     repository).  So a machine-distilled document that nobody has reviewed, emitted without
    #     the field, was publishing itself as **stable** —— the strongest lifecycle claim OKF has,
    #     asserted about a page whose own frontmatter says "LLM, needs review afterwards".
    #     A document is called draft when it was generated and carries no human review; a
    #     hand-written note with no maturity keeps saying nothing, and takes the spec's default.
    if not st:
        machine = bool(fm.get("generated_by") or fm.get("distilled_by"))
        if machine and not fm.get("verified"):
            st = "draft"
    if st:
        out.append("status: %s" % st)

    # Our vocabulary with no OKF counterpart but too useful to drop —— unknown keys are allowed (§11)
    #
    # ⚠ `no_llm` **must** be in here.  The frontmatter is rewritten as an allowlist, so a key
    #   missing from the list disappears silently.  The gate is `schema_v3.doc_meta`, read by
    #   `lr_extract.py:351` —— **the only per-document block on LLM transmission** —— and with no
    #   matching string in the converted output a user's "do not send this note" is ignored,
    #   without even appearing in the 'N excluded from transmission' log.
    #   ⚠ `no_llm` is **not** in `extensions`; it is emitted separately above, unquoted.  Adding
    #      it to the list would route it through `yaml_str` and quote it, which breaks the gate.
    #   (deep review 2026-08-19 security lens; the consumer corrected 2026-09-02)
    #  ⚠ `doc_type` is emitted **unquoted**, like `no_llm`.  The indexer reads it with
    #     `re.search(r"^doc_type:\s*(\S+)", raw[:1200])` (schema_v3.py) and `(\S+)` swallows the
    #     quotes, so `doc_type: "analysis"` indexes as the six-character string `"analysis"`
    #     —— quotes included —— into a BITMAP column.  Every filter on it then matches zero rows,
    #     with no error anywhere.  The vault's own writer (distill_sessions) has always emitted it
    #     bare; quoting it here was a regression this converter introduced.
    #     (deep review 2026-09-02, completeness lens —— measured 299/301 pages affected)
    BARE = ("doc_type",)
    for k in extensions:
        if fm.get(k):
            v = str(fm[k]).strip().strip("\"'")
            out.append("%s: %s" % (k, v if k in BARE and v and " " not in v else yaml_str(fm[k])))

    return "---\n%s\n---\n%s" % ("\n".join(out), convert_links(body, idx, unresolved))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(KAL_HOME, "exp/okf-vault"))
    ap.add_argument("--vault", default=VAULT)
    ap.add_argument("--only", type=int, default=0, help="the first N only (for sampling experiments)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    files = []
    for f in sorted(glob.glob(f"{a.vault}/**/*.md", recursive=True)):
        if is_skipped(f):
            continue
        body, _ = index_clean(open(f, encoding="utf-8", errors="ignore").read())
        if len(body) < 60:
            continue
        files.append(f)
    if a.only:
        files = files[:a.only]
    idx = build_link_index(files, a.vault)

    # ③ One typo in --out could delete the vault original.
    #    It only deletes when an existing bundle marker is present, or the path is new and empty.
    #
    #    ⚠ While the marker was "index.md + log.md exist", **the real vault's wiki/ held both**
    #    and passed the guard.  One `--out .../super-brain/wiki` would delete 79 md files.  The
    #    guard let through exactly the scenario it existed to stop, while giving false comfort.
    #    (deep review 2026-08-19, round 4, security lens)
    #
    #    So the marker is narrowed to **something only this converter writes**: okf_version inside index.md.
    # A second line of defence —— the path is checked before the content.  If `--out` is the
    # vault itself or below it, it is refused whatever marker is present.  That cuts off the accident scenario itself.
    ro, rv = os.path.realpath(a.out), os.path.realpath(a.vault)
    if under(ro, rv):
        raise SystemExit(
            f"--out is inside the vault: {a.out}\n"
            f"  vault: {rv}\n"
            f"  Build the bundle outside the vault (the default is ~/.kal/exp/okf-vault).")
    if os.path.isdir(a.out):
        looks_like_bundle = is_bundle(a.out)
        if not looks_like_bundle and os.listdir(a.out):
            raise SystemExit(
                f"--out does not look like an OKF bundle and is not empty either: {a.out}\n"
                f"  Overwriting would destroy what is in it.  Give an empty path, or clear it yourself.")
        shutil.rmtree(a.out)
    os.makedirs(a.out, exist_ok=True)
    # A full copy of the vault goes in here.  Relying on the parent (~/.kal at 700) alone means
    # the permissions do not survive a tar/rsync/backup restore.  It protects itself.
    os.chmod(a.out, 0o700)
    # chmod covers the root only.  Subdirectories and files follow the umask, so under 022 they
    # become 0755/0644, and tar/rsync preserve per-entry modes, leaving the restored copy
    # world-readable.  (deep review 2026-08-19, round 4)
    os.umask(0o077)

    unresolved, links, n = set(), 0, 0
    entries = []
    for f in files:
        rel = os.path.relpath(f, a.vault).replace(os.sep, "/")
        text = open(f, encoding="utf-8", errors="ignore").read()
        fm, body = parse_fm(text)
        links += len(WIKILINK.findall(body))
        out = to_okf(fm, body, rel, idx, unresolved)
        dst = os.path.join(a.out, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        open(dst, "w", encoding="utf-8").write(out)
        entries.append((rel, fm.get("title") or os.path.basename(rel)[:-3]))
        n += 1

    # Reserved files (§8–§9).  index.md has frontmatter only at the bundle root.
    today = datetime.date.today().isoformat()
    by_dir = {}
    for rel, title in entries:
        by_dir.setdefault(rel.split("/")[0] if "/" in rel else "(root)", []).append((rel, title))
    idx_lines = ['---', 'okf_version: "%s"' % OKF_VERSION, "---", "",
                 "# Knowledge Bundle", "",
                 "An LLM-Wiki vault converted to OKF v%s.  Original: `%s`" % (OKF_VERSION, a.vault), ""]
    for d in sorted(by_dir):
        idx_lines.append("## %s" % d)
        for rel, title in sorted(by_dir[d]):
            idx_lines.append("- [%s](/%s)" % (title, rel))
        idx_lines.append("")
    open(os.path.join(a.out, "index.md"), "w", encoding="utf-8").write("\n".join(idx_lines))
    open(os.path.join(a.out, "log.md"), "w", encoding="utf-8").write(
        "# Log\n\n## %s\n\n- okf_convert.py converted %d document(s).\n" % (today, n))

    print(f"  → {a.out}")
    print(f"     {n} document(s) · {links} link(s) converted · {len(unresolved)} unresolved target(s)")
    if unresolved:
        print("     unresolved examples: " + ", ".join(sorted(unresolved)[:5]))


def selftest():
    idx = {"target": "/wiki/concepts/target.md", "Target": "/wiki/concepts/target.md"}
    un = set()
    got = convert_links("see [[target]] and [[target|a label]] and [[target#part]] and [[missing]].", idx, un)
    assert "[target](/wiki/concepts/target.md)" in got, got
    assert "[a label](/wiki/concepts/target.md)" in got, got
    assert "[target](/wiki/concepts/target.md#part)" in got, got
    assert "/unresolved/" in got and "missing" in un, got
    fm, body = parse_fm('---\ntitle: "A: B"\ntags: [x, y]\nmaturity: seedling\n---\n## Overview\n\nIt starts. Second.\n')
    assert fm["title"] == "A: B" and fm["tags"] == ["x", "y"], fm
    okf = to_okf(fm, body, "wiki/a.md", idx, un)
    assert okf.startswith("---\ntype: ")             # type is the first required field
    assert 'description: "It starts."' in okf, okf
    assert first_sentence("Pi is 3.14 and that is that. Next.") == "Pi is 3.14 and that is that.", \
        first_sentence("Pi is 3.14 and that is that. Next.")
    # A paragraph starting in bold must not be mistaken for a list
    bold = "# Title\n\n**Andrej Karpathy** proposed the pattern. A second sentence.\n\n- a list item\n"
    assert first_sentence(bold) == "**Andrej Karpathy** proposed the pattern.", first_sentence(bold)
    # A real list must still be filtered out
    assert first_sentence("- only a list.\n- and another.\n") == "", first_sentence("- only a list.\n")
    assert "status: draft" in okf, okf
    assert "verified" not in okf, "a review record that does not exist must not be invented"
    # The guard: the vault's wiki/ holds index.md + log.md.  Judging by name alone mistakes it
    # for a bundle and rmtree's it.  In round 4 that really could have erased 79 md files.
    import tempfile as _tf
    with _tf.TemporaryDirectory() as _d:
        _w = os.path.join(_d, "wiki"); os.makedirs(_w)
        open(os.path.join(_w, "index.md"), "w").write("# Wiki\n\n[[a]]\n")
        open(os.path.join(_w, "log.md"), "w").write("# Log\n")
        assert not is_bundle(_w), "the vault's wiki/ is mistaken for a bundle —— rmtree risk"
        # A mention in prose must not pierce it (in round 5 that really did delete 79 notes)
        open(os.path.join(_w, "index.md"), "w").write(
            "- [[okf-experiment]] — the converter declares okf_version in index.md.\n")
        assert not is_bundle(_w), "a prose mention of okf_version pierces the guard"
        # An example inside a code fence must not pierce it either (round 6)
        open(os.path.join(_w, "index.md"), "w").write(
            '# Example\n\n```yaml\nokf_version: "0.2"\n```\n')
        assert not is_bundle(_w), "an okf_version example inside a code fence pierces the guard"
        # A real bundle must still be recognised
        open(os.path.join(_w, "index.md"), "w").write('---\nokf_version: "0.2"\n---\n')
        assert is_bundle(_w), "a real bundle is not recognised"

    # The second line of defence —— a path differing only in case must still count as inside the vault.
    # With a string prefix comparison, files really were deleted (round 6).
    with _tf.TemporaryDirectory() as _d:
        _v = os.path.join(_d, "myvault"); os.makedirs(os.path.join(_v, "wiki"))
        assert under(os.path.join(_v, "wiki"), _v), "a path below the vault is not recognised"
        assert under(_v, _v), "the vault itself is not recognised"
        assert under(os.path.join(_d, "MyVault", "wiki"), _v) or \
            os.stat(_d).st_dev != os.stat(_v).st_dev or \
            not os.path.exists(os.path.join(_d, "MyVault")), \
            "a path differing only in case is judged outside the vault"
        assert not under(os.path.join(_d, "other"), _v), "an unrelated path is called inside the vault"
        assert not is_bundle(os.path.join(_d, "nonexistent")), "a path that does not exist is called a bundle"

    # The sN: prefix in sources must be split off.  Without it the id swallows the whole
    # reference and resource becomes `/wiki/sources/s1:slug.md`, **a path that does not exist**.
    _fm4, _b4 = parse_fm('---\ntitle: "T"\ntype: concept\n'
                         'sources: ["s1:alpha", "s2:beta", "legacy"]\n---\nThe body.\n')
    _okf4 = to_okf(_fm4, _b4, "x.md", {}, set())
    assert '- id: "s1"' in _okf4 and '- id: "s2"' in _okf4, _okf4
    assert '"/wiki/sources/alpha.md"' in _okf4, _okf4
    assert "s1:alpha" not in _okf4, "the prefix was not split off"
    assert '- id: "legacy"' in _okf4, "an older document without the prefix is rejected"

    # Coupled to the consumer.  If lr_extract changes its scan window or regex, this breaks here.
    try:
        import lr_extract as _lx
        #  The **live** consumer, not the vestigial NO_LLM_MARK constant.
        from schema_v3 import doc_meta as _dm
        assert _dm('---\nno_llm: true\n---\nb\n')[2], "the live gate no longer reads no_llm"
    except ImportError:
        pass

    # no_llm must survive —— the only per-document block on LLM transmission
    fm2, body2 = parse_fm('---\ntitle: "T"\ntype: concept\nno_llm: true\n---\nThe body.\n')
    okf2 = to_okf(fm2, body2, "wiki/b.md", idx, un)
    import re as _re
    # Even with a long frontmatter it must stay inside the consumer's scan window (t[:1200] at lr_extract:152)
    _long = ('---\ntitle: "T"\ntype: concept\nno_llm: true\n'
             'why_captured: "%s"\n---\nThe body.\n' % ("long " * 800))
    _fm3, _b3 = parse_fm(_long)
    _okf3 = to_okf(_fm3, _b3, "x.md", {}, set())   # unresolved is a set —— passing a string raises AttributeError the moment a wikilink appears
    assert len(_okf3) > 1200, "the padding is too short, so this check blocks nothing (%d characters total)" % len(_okf3)
    _hit = _re.search(r"^no_llm:\s*true\s*$", _okf3[:1200], _re.M | _re.I)
    assert _hit, "no_llm was pushed outside the 1200-character scan window by a long frontmatter"

    assert _re.search(r"^no_llm:\s*true\s*$", okf2, _re.M | _re.I), \
        "must be in the form schema_v3.doc_meta matches: " + okf2
    # Deterministic: two runs must be identical
    assert to_okf(*parse_fm('---\ntitle: "A: B"\ntags: [x, y]\nmaturity: seedling\n---\n## Overview\n\nIt starts. Second.\n'),
                  "wiki/a.md", idx, set()) == okf
    #  ── the session scheme follows the agent ────────────────────────────────────────────
    #  A hardcoded `claude-session://` made every Codex document claim a Claude origin, and did
    #  it with a well-formed string, so nothing downstream could notice.
    _base = {"type": "conversation", "title": "T", "session_id": "abc-123",
             "distilled_by": "distill_sessions.py (LLM, needs review afterwards)",
             "captured": "2026-09-02"}
    for _agent, _want in (("codex", "codex-session://abc-123"),
                          ("claude", "claude-session://abc-123"),
                          (None, "claude-session://abc-123")):     # absent ⇒ Claude, the older corpus
        _fm = dict(_base) | ({"session_agent": _agent} if _agent else {})
        _o = to_okf(_fm, "Body.", "x.md", {}, set())
        assert _want in _o, f"session_agent={_agent!r} produced the wrong scheme:\n{_o}"

    #  ── status is stated, because omitting it asserts `stable` ───────────────────────────
    #  references/okf-SPEC-v0.2.md:422 —— "Absent `status` ⇒ `stable`."  A machine-distilled,
    #  unreviewed page must not publish itself under OKF's strongest lifecycle claim.
    assert _re.search(r"^status: draft$", to_okf(dict(_base), "Body.", "x.md", {}, set()), _re.M), \
        "a generated, unverified document did not declare itself draft"
    #  A hand-written note with neither maturity nor a generator says nothing and takes the default.
    assert not _re.search(r"^status:", to_okf({"type": "note", "title": "T"}, "Body.", "x.md", {}, set()), _re.M), \
        "status was invented for a document that carries no evidence of its lifecycle"
    #  A human review outranks the machine default.
    assert not _re.search(r"^status: draft$",
                          to_okf(dict(_base) | {"verified": "human:taehyun"}, "B.", "x.md", {}, set()), _re.M), \
        "a reviewed document was still called draft"

    #  ── the openwiki bundle carries exactly three extension keys ─────────────────────────
    #  Its SPEC.md says "OKF v0.2 plus three".  Emitting five would make that document false.
    _rich = dict(_base) | {"doc_type": "analysis", "why_captured": "why", "origin": "claude-session",
                           "distilled_from": "2 messages, 9,786 chars", "no_llm": "true"}
    _strict = to_okf(_rich, "Body.", "x.md", {}, set(), extensions=EXT_OPENWIKI)
    for _k in ("no_llm", "doc_type", "why_captured"):
        assert _re.search(rf"^{_k}:", _strict, _re.M), f"{_k} is missing from the openwiki output"
    for _k in ("origin", "distilled_from"):
        assert not _re.search(rf"^{_k}:", _strict, _re.M), \
            f"{_k} leaked into the openwiki output —— its SPEC.md declares three extensions"
    #  and the vault experiment keeps all four, unchanged
    _rich_all = to_okf(_rich, "Body.", "x.md", {}, set())
    assert _re.search(r"^origin:", _rich_all, _re.M), "the vault output lost a key it used to have"

    #  ── generated.by follows OKF's actor convention (§7) ─────────────────────────────────
    #  A bare script name is none of the three shapes, so a trust-grading consumer cannot
    #  classify it; and `human:` is the one prefix the spec makes mandatory.
    assert actor("distill_sessions.py (LLM, needs review afterwards)") == "process:distill_sessions"
    assert actor("human:taehyun") == "human:taehyun", "a human actor must survive untouched"
    assert actor("openwiki/0.3.3") == "openwiki/0.3.3", "a <producer>/<version> actor must survive"
    assert actor("") == "process:unknown"
    assert _re.search(r'^  by: "process:distill_sessions"$',
                      to_okf(dict(_base), "B.", "x.md", {}, set()), _re.M), \
        "generated.by did not reach the output in actor form"

    #  ── doc_type is emitted bare, because the indexer's `(\S+)` swallows quotes ───────────
    #  `doc_type: "analysis"` indexed as the six-char string `"analysis"` into a BITMAP column,
    #  so every filter on it matched zero rows with no error.  Measured on 299/301 pages.
    _dt = to_okf(dict(_base) | {"doc_type": "analysis"}, "B.", "x.md", {}, set(),
                 extensions=EXT_OPENWIKI)
    assert _re.search(r"^doc_type: analysis$", _dt, _re.M), \
        "doc_type must be unquoted —— schema_v3 reads it with (\\S+) and would keep the quotes:\n" + _dt
    #  the indexer's own regex, run here so the two cannot drift apart
    assert _re.search(r"^doc_type:\s*(\S+)", _dt, _re.M).group(1) == "analysis", \
        "the indexer's regex does not recover a bare value"
    #  a value with a space still has to be quoted, or the YAML is wrong
    assert '"two words"' in to_okf(dict(_base) | {"doc_type": "two words"}, "B.", "x.md", {},
                                   set(), extensions=EXT_OPENWIKI)

    #  ── sources[].author is about the SOURCE, not about us (§5.1) ─────────────────────────
    assert not _re.search(r"^\s+author:", to_okf(dict(_base), "B.", "x.md", {}, set()), _re.M), \
        "sources[].author attributes the session to our own distiller (§5.1)"
    #  ── resource must not point at the page itself (§4.1) ─────────────────────────────────
    _self = to_okf(dict(_base), "B.", "personal/sessions/codex/x.md", {}, set())
    assert not _re.search(r"^resource: .*personal/sessions", _self, _re.M), \
        "resource points at the page itself; §4.1 wants the asset the concept describes"
    #  …but a document that names a real external asset keeps it
    assert 'resource: "https://ai-2027.com/"' in to_okf(
        dict(_base) | {"source": "https://ai-2027.com/"}, "B.", "x.md", {}, set()), \
        "an external source URL was dropped instead of becoming `resource`"

    print("  ✅ okf_convert self-check — 4 link forms · frontmatter round trip · determinism\n"
          "     · session scheme follows the agent · status is stated, never left to default\n"
          "     · openwiki output carries exactly three extensions, the vault output keeps four\n"
          "     · generated.by is an OKF actor (§7)\n"
          "     · doc_type bare (the indexer's own regex) · no sources[].author · no self-resource")


if __name__ == "__main__":
    main()
