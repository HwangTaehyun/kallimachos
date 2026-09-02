#!/usr/bin/env python3
"""Markdown → an openwiki-personal bundle (OKF v0.2 + three extension keys).

Two entry shapes, one code path:

    # the session corpus (Claude + Codex), the default
    openwiki_emit.py --wiki ~/github/HwangTaehyun/openwiki

    # any other folder of Obsidian notes, for migration
    openwiki_emit.py --from ~/vault/wiki --into personal/notes

The format contract lives in the bundle's own `SPEC.md`; this file implements it and must not
drift from it.  What that means concretely:

  * exactly three extension keys —— `no_llm`, `doc_type`, `why_captured` (`okf_convert.EXT_OPENWIKI`)
  * `status` is always stated.  Omitting it asserts `stable` (OKF §5.4), which would be a lie
    about a machine-distilled page nobody has read.
  * session provenance rides in `sources[].resource` as `claude-session://` / `codex-session://`,
    so the bundle needs no `origin` key to say which agent produced a page.

⚠ **Deleting is the dangerous half, not writing.**  `promote_distilled.py` once cleared a
   destination with `shutil.rmtree` and took a folder from **201 files to 3** —— 200 attachments
   and hand-written notes that were never its to remove (measured 2026-08-28).  Its shrink guard
   counted only top-level `*.md` and therefore never fired.  This file inherits both lessons:
   it removes only pages it can prove it wrote, and it refuses to run outside a git repository,
   because `git revert` is the only way back.
"""
import os, re, sys, json, glob, shutil, hashlib, argparse, subprocess, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from okf_convert import parse_fm, to_okf, build_link_index, EXT_OPENWIKI   # noqa: E402
from schema_v3 import NO_LLM_RE   # the transmission gate lives in one place
from ingest_sessions import find_leaks                                     # noqa: E402

KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
WIKI = os.environ.get("OPENWIKI_DIR", os.path.expanduser("~/github/HwangTaehyun/openwiki"))
DISTILLED = os.path.join(KAL_HOME, "distilled")

MANIFEST = ".page-manifest.json"
SHRINK_RATIO = 0.8

#  A page this tool wrote carries a session URI in `sources[]`.  That is derived from real
#  provenance rather than a marker planted for the purpose.
#
#  ⚠ **It is matched inside the parsed frontmatter, never as a substring of the file.**  The
#     first version ran this regex over the first 4000 bytes with `re.M`, so a page that merely
#     *documented* the format —— a line like ``resource: "claude-session://<id>"`` inside a code
#     fence —— was classified as ours and deleted.  `SPEC.md` and `INSTRUCTIONS.md` are exactly
#     such pages, and one `--into` at the bundle root would have taken them.  That is the same
#     201-files-to-3 failure this module's docstring claims to have learned from, reintroduced
#     one layer up.  (deep review 2026-09-02, security lens —— reproduced, not inferred)
#  ⚠ The scheme is the whole guard.  Relaxing this to `^\s*resource:` —— which a mutation test
#     showed leaves every self-check green —— classifies **any** OKF page as ours, and `resource`
#     is a recommended OKF field, so that is every third-party page in a touched directory.
#     The 201-files-to-3 class, one more layer up.  A self-check now drops a foreign OKF page
#     into the destination and asserts it survives.
#  ⚠ The `-` prefix matters.  `sources:` entries are equally valid as
#     `  - resource: …` (the item starting on the same line) or as
#     `  - id: …` / `    resource: …`.  This tool emits the second, so a regex
#     anchored on `^\s*resource:` matched our own pages and would have missed any
#     bundle written the other way —— a self-check written for this guard caught it
#     before it mattered (2026-09-02).
OWNED_LINE = re.compile(r"^[ \t]*(?:-[ \t]+)?resource:[ \t]*\"?(?:claude|codex)-session://", re.M)


def _fm_scalar_free(fm):
    """The frontmatter with **block-scalar bodies removed** —— what ownership may be judged on.

    ⚠ `OWNED_LINE` is a regex over text, not a YAML parser, and it allows leading whitespace
       because `sources:` entries are indented.  So this hand-written page was classified as
       ours and **deleted**:

           description: |
             resource: claude-session://example

       The provenance line has to be a mapping entry, not the *content* of one.  Dropping the
       body of every `|`/`>` scalar is the smallest thing that separates them, and it keeps this
       a text pass —— pulling a YAML parser in here would change what "frontmatter" means for
       every other caller.  (codex review 2026-09-02, blocker #6 —— reproduced)
    """
    if not fm:
        return fm
    out, skip_to = [], None
    for ln in fm.split("\n"):
        indent = len(ln) - len(ln.lstrip(" \t"))
        if skip_to is not None:
            if ln.strip() and indent > skip_to:
                continue                      # inside the scalar's body
            skip_to = None
        out.append(ln)
        if re.match(r"^[ \t]*[^\s:#][^:]*:[ \t]*[|>][-+0-9]*[ \t]*$", ln):
            skip_to = indent
    return "\n".join(out)


def owned(d):
    """Pages under `d` whose **frontmatter** carries session provenance —— the ones we may replace.

    ⚠ `os.walk`, not `glob(recursive=True)`.  glob follows directory symlinks, so a link inside
       the bundle pointing at the vault made this function reach through it and `os.remove` the
       real note —— while `foreign()` (which already used os.walk) could not see it, so the
       "leaving N file(s) this tool did not write" reassurance stayed silent about the deletion.
       The two must walk the same tree or the count is a lie.
    """
    out = []
    #  ⚠ **This directory only —— it must not recurse.**  The caller passes each directory a planned
    #     page lands in (`touched_dirs`), and every nested directory this run writes to is in that
    #     set on its own.  Recursing therefore adds nothing this run produced and sweeps in whole
    #     subtrees it did not.  Reproduced 2026-09-02: one root-level vault page lands at
    #     `personal/foo.md`, so `personal/` becomes a touched directory, and the recursive walk
    #     found all 982 pages under `personal/sessions/**` —— `just openwiki` would have erased
    #     path A while processing path B.  (codex review, blocker #1)
    for root, _dirs, files in ([(d, [], os.listdir(d))] if os.path.isdir(d) else []):
        for name in files:
            if name == "index.md" or not name.endswith(".md"):
                continue
            f = os.path.join(root, name)
            if not os.path.isfile(f):
                continue
            #  ⚠ There used to be a `parse_fm` branch above this one, walking `fm["sources"]` as
            #     a list.  It was **dead** —— `parse_fm` in this codebase flattens nested YAML and
            #     returns `''` for `sources:`, so `isinstance(..., list)` was always False and
            #     every real page was in fact classified down here.  A mutation that emptied the
            #     loop left all self-checks green, which is the definition of a branch no test can
            #     distinguish.  (deep review 2026-09-02, guard-testability lens)
            blk = _fm_scalar_free(_fm_block(f))
            if blk and OWNED_LINE.search(blk):
                out.append(f)
    return out


def _fm_block(path):
    """The raw frontmatter block of a file, or ''.  Body text is never searched for ownership."""
    try:
        raw = open(path, encoding="utf-8", errors="ignore").read(8000)
    except OSError:
        return ""
    m = re.match(r"\A\ufeff?\s*---[ \t]*\r?\n(.*?)\r?\n(?:---|\.\.\.)[ \t]*\r?\n", raw, re.S)
    return m.group(1) if m else ""


def has_no_llm(path):
    """Does this published page carry the transmission block?  Frontmatter only."""
    return bool(NO_LLM_RE.search(_fm_block(path)))


def git_ok(root):
    """Is `root` somewhere `git revert` can actually undo a deletion?

    ⚠ Being *inside* a repository is not enough.  A bundle listed in `.gitignore` passes
       `rev-parse` and is still untracked, so `git status` stays clean while pages are removed and
       nothing can be restored —— the one guarantee this module's docstring rests on.  Reproduced
       2026-09-02: `git init repo`, `.gitignore` = `bundle/`, `--wiki repo/bundle` wrote four
       pages and `git status` showed only the .gitignore.
    """
    if subprocess.run(["git", "-C", root, "rev-parse", "--git-dir"],
                      capture_output=True).returncode != 0:
        return False
    #  `check-ignore` exits 0 when the path IS ignored —— which is the case we must refuse.
    return subprocess.run(["git", "-C", root, "check-ignore", "-q", "."],
                          capture_output=True).returncode != 0


def foreign(d):
    """Everything else under `d` —— never touched.  Counted so a run can say what it left alone."""
    own = set(owned(d))
    out = []
    #  Same scope as `owned` above —— the two must walk the same tree or the count is a lie.
    for root, _dirs, files in ([(d, [], os.listdir(d))] if os.path.isdir(d) else []):
        for f in files:
            p = os.path.join(root, f)
            if p not in own and os.path.basename(p) not in (MANIFEST, "index.md"):
                out.append(p)
    return out


#  ⚠ **This value becomes a path component.**  `--into` is containment-checked, but the default
#     destination is built from the document's own `session_agent`, which is not —— so
#     `session_agent: ../../../../tmp/PRECIOUS` wrote and deleted outside the git-validated
#     bundle.  Whitelisting the characters is what makes that check unnecessary rather than
#     duplicated.  (codex review 2026-09-02, blocker #2 —— reproduced)
AGENT_OK = re.compile(r"[^a-z0-9_-]")


def agent_of(fm):
    a = AGENT_OK.sub("", (fm.get("session_agent") or "claude").strip().lower())
    return a or "claude"


def slug_of(path):
    return os.path.basename(path)[:-3] if path.endswith(".md") else os.path.basename(path)


#  Index files are the one place OKF forbids frontmatter:
#
#  > Index files contain **no frontmatter**, with one exception: a bundle-root `index.md` MAY
#  > carry an `okf_version` key (§12).  The body uses one or more sections, each grouping
#  > concepts under a heading:  `* [Title 1](relative-url-1) - short description`
#  > —— references/okf-SPEC-v0.2.md §8, §12
#
#  ⚠ The first version of this function wrote a full `type`/`title`/`description`/`status` block
#     into every index —— 3 files, silently non-conformant, in a bundle whose own SPEC.md claims
#     OKF v0.2.  Nothing rejects it (§11 forbids consumers from refusing over unknown fields), so
#     it would have stayed wrong indefinitely.
def render_index(rel_dir, entries, root=False):
    """A directory's index.md.  Deterministic —— sorted, so an unchanged run rewrites the same bytes."""
    lines = []
    if root:
        #  The only frontmatter OKF allows in an index, and only at the bundle root.
        lines += ["---", 'okf_version: "0.2"', "---", ""]
    lines += ["# %s" % (rel_dir.rstrip("/").split("/")[-1] or "openwiki"), ""]
    if not entries:
        lines.append("_(비어 있음)_")
    for title, rel, desc in sorted(entries, key=lambda e: (e[0] or "", e[1])):
        lines.append("* [%s](/%s)%s" % (title or slug_of(rel), rel.lstrip("/").replace(os.sep, "/"),
                                        (" - " + desc) if desc else ""))
    return "\n".join(lines) + "\n"


def convert_one(src, rel_out, idx, unresolved):
    """One source file → (okf text, title, description).  Returns None when it has no frontmatter."""
    #  `errors="replace"` —— one latin-1 byte anywhere used to abort the whole run with a
    #  bare UnicodeDecodeError naming no file.  `owned()` already had this discipline.
    fm, body = parse_fm(open(src, encoding="utf-8", errors="replace").read())
    if not fm:
        return None
    text = to_okf(fm, body, rel_out, idx, unresolved, extensions=EXT_OPENWIKI)
    m = re.search(r'^description:\s*(.+)$', text, re.M)
    desc = (m.group(1).strip().strip('"') if m else "")
    return text, (fm.get("title") or slug_of(src)), desc


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--wiki", default=WIKI, help="the openwiki bundle root")
    ap.add_argument("--from", dest="src", default=DISTILLED,
                    help="folder of markdown to convert (default: ~/.kal/distilled)")
    ap.add_argument("--into", default=None,
                    help="destination inside the bundle.  Default: personal/sessions/<agent>/ "
                         "chosen per document from its session_agent")
    ap.add_argument("--exclude", action="append", default=[],
                    help="skip source paths containing this fragment (repeatable).  Needed to "
                         "keep an already-migrated subtree — raw/conversations/sessions — out of "
                         "a run over its parent.")
    ap.add_argument("--force", action="store_true", help="proceed past the shrink guard")
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()

    #  ⚠ **`--into` used to be joined straight onto the bundle root.**  An absolute value
    #     discarded the root entirely and `../` walked out of it, so writes —— and then the
    #     `os.remove` loop —— landed outside the directory `git_ok()` had just validated.  The
    #     module's whole safety story is "git revert is the only way back", and it was void
    #     exactly where the files went.  An absolute value also hung forever in the index walk
    #     (`os.path.dirname("/") == "/"`), *after* the deletions.
    #     (deep review 2026-09-02, security + completeness lenses —— both reproduced)
    if a.into:
        #  ⚠ An explicit `os.path.isabs` check used to sit here.  A mutation test showed it was
        #     **completely redundant** with the containment check below —— `os.path.join` lets an
        #     absolute value replace the root, and `commonpath` then rejects it anyway, so
        #     removing the isabs arm left every self-check green.  Same shape as the duplicated
        #     `role: developer` guard removed in ingest_codex_sessions on the same day: a branch
        #     no test can distinguish is a branch that rots unnoticed.
        probe = os.path.realpath(os.path.join(a.wiki, a.into))
        root = os.path.realpath(a.wiki)
        if os.path.commonpath([probe, root]) != root:
            print(f"❌ --into escapes the bundle: {a.into} → {probe}")
            return 1
    if not os.path.isdir(a.src):
        print(f"❌ no such source folder: {a.src}")
        return 1
    #  ⚠ **The source must not sit inside the bundle.**  `openwiki-adopt` points VAULT_DIR at the
    #     bundle so every surface reads what was indexed —— and from that moment `just openwiki-vault`
    #     (whose vault argument defaults to VAULT_DIR) would convert the bundle *into itself*: every
    #     page re-slugged and re-emitted beside the original, doubling the corpus with documents the
    #     indexer cannot tell apart.  The pipeline's own success is what arms this, which is why it
    #     is a guard and not a note.
    if os.path.commonpath([os.path.realpath(a.src), os.path.realpath(a.wiki)]) == os.path.realpath(a.wiki):
        print(f"❌ --from is inside the bundle: {a.src}\n"
              f"   Converting the bundle into itself would duplicate every page.\n"
              f"   After `just openwiki-adopt`, pass the original vault explicitly:\n"
              f"     just openwiki-vault {a.wiki} <path-to-the-obsidian-vault>")
        return 1
    if not git_ok(a.wiki):
        print(f"❌ the bundle is not a git repository: {a.wiki}\n"
              f"   This replaces pages, and `git revert` is the only way back.\n"
              f"     git -C {a.wiki} init && git -C {a.wiki} add -A && git -C {a.wiki} commit -m init")
        return 1

    #  ⚠ Not recursive before.  Measured on the real vault: `wiki/` migrated **3 of 79** and
    #     `raw/` migrated **0 of 297** (every file nested), printing a cheerful "✅ 1 page(s)".
    #     A migration that silently moves 4% is worse than one that fails.
    files = sorted(glob.glob(os.path.join(a.src, "**", "*.md"), recursive=True))
    #  ⚠ A source `index.md` must not become a page.  OKF §8 forbids frontmatter in an index, and
    #     this tool generates its own for every directory —— copying the vault's in would both
    #     violate the spec and be overwritten moments later by the generated one.
    n_ix = sum(1 for f in files if os.path.basename(f) == "index.md")
    files = [f for f in files if os.path.basename(f) != "index.md"]
    if a.exclude:
        before = len(files)
        files = [f for f in files if not any(x in f for x in a.exclude)]
        print(f"  ⓘ excluded {before - len(files)} file(s) matching {a.exclude}")
    if n_ix:
        print(f"  ⓘ skipped {n_ix} source index.md —— this tool generates its own (§8)")
    if not files:
        print(f"❌ no .md under {a.src}")
        return 1

    #  Wikilinks resolve against the *source* folder, the way okf_convert does for the vault.
    idx = build_link_index(files, a.src)
    unresolved = set()

    #  ── plan first, write later.  Nothing is removed before the whole run is known good ──
    plan, skipped = [], 0
    for f in files:
        try:
            fm, _ = parse_fm(open(f, encoding="utf-8", errors="replace").read())
        except OSError as e:
            print(f"  ⚠ unreadable, skipped: {f} ({e})")
            skipped += 1
            continue
        if not fm:
            skipped += 1
            continue
        sub = a.into or os.path.join("personal", "sessions", agent_of(fm))
        #  Keep the source's own subdirectories, so a nested vault tree does not collapse into
        #  one flat folder where two `index.md`-adjacent names collide.
        inner = os.path.relpath(os.path.dirname(f), a.src)
        rel = os.path.join(sub, "" if inner == "." else inner, os.path.basename(f))
        got = convert_one(f, rel, idx, unresolved)
        if not got:
            skipped += 1
            continue
        text, title, desc = got
        plan.append((rel, text, title, desc))

    #  ── the shrink guard, counting what we would actually replace ──
    touched_dirs = sorted({os.path.dirname(os.path.join(a.wiki, rel)) for rel, *_ in plan})
    existing = sum(len(owned(d)) for d in touched_dirs if os.path.isdir(d))
    if existing and len(plan) < existing * SHRINK_RATIO and not a.force:
        print(f"❌ {len(plan)} page(s) to write but {existing} already there —— "
              f"{existing - len(plan)} would disappear.\n"
              f"   Check that {a.src} is intact.  If this is intended, pass --force")
        return 1

    left = sum(len(foreign(d)) for d in touched_dirs if os.path.isdir(d))
    if left:
        print(f"  ⓘ leaving {left} file(s) this tool did not write")

    if a.dry_run:
        print(f"  (dry run) {len(plan)} page(s) → {a.wiki} · {skipped} skipped (no frontmatter)")
        for rel, *_ in plan[:5]:
            print(f"      {rel}")
        return 0

    #  ⚠ **The transmission block has to survive a re-emit.**  A published page that a person
    #     marked `no_llm: true` by hand is `owned()`, so it was deleted and rewritten from
    #     ~/.kal/distilled —— which never carries the key, because `distill_sessions.write()`
    #     does not emit it.  The user's "do not send this document to an LLM" therefore lasted
    #     exactly one run, and nothing said so.  (deep review 2026-09-02, security lens)
    for rel, text, title, desc in list(plan):
        dst = os.path.join(a.wiki, rel)
        if os.path.exists(dst) and has_no_llm(dst) and not re.search(r"^no_llm:", text, re.M):
            i = plan.index((rel, text, title, desc))
            #  Second line, straight after `type:` —— the position `to_okf` puts it in.
            lines = text.split("\n")
            at = 2 if len(lines) > 2 and lines[1].startswith("type:") else 1
            lines.insert(at, "no_llm: true")
            plan[i] = (rel, "\n".join(lines), title, desc)
            print(f"  ⓘ carried `no_llm: true` forward from the published {rel}")

    #  ⚠ **The one write that leaves ~/.kal (0700) for a shareable git repository.**  Both
    #     ingesters verify before writing; this boundary did not, so anything `SECRETS` fails to
    #     match rode ingest → distill → bundle uncontested.  Names and counts only, never values.
    leak = find_leaks("".join(txt for _rel, txt, *_ in plan))
    if leak:
        print(f"\n❌ masking verification failed — {sum(leak.values())} secret(s) in the pages "
              f"about to be published: {leak}")
        print("   Nothing is written and it stops here.  Strengthen SECRETS in ingest_sessions.py,")
        print("   re-run the ingest and distil, then try again.")
        return 1

    #  ── write ──
    for d in touched_dirs:
        os.makedirs(d, exist_ok=True)
        for stale in owned(d):
            os.remove(stale)
    for rel, text, _t, _d in plan:
        dst = os.path.join(a.wiki, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        open(dst, "w", encoding="utf-8").write(text)

    #  ── indexes and manifest ──
    #  ⚠ **Built from the whole bundle, not from this run's `plan`.**  They used to be rebuilt
    #     from `plan` alone, so a second run with a different `--into` dropped the first run's
    #     pages from `.page-manifest.json` and from the root index —— the 299 session pages stayed
    #     on disk but became unreachable from the root, which is the exact property the comment
    #     below claims to enforce.  Reproduced: manifest went 2 pages → 1.
    #     (deep review 2026-09-02, completeness lens)
    by_dir = {}
    for f in sorted(glob.glob(os.path.join(a.wiki, "**", "*.md"), recursive=True)):
        rel = os.path.relpath(f, a.wiki)
        if os.path.basename(rel) == "index.md" or rel.split(os.sep)[0] == "references":
            continue
        if os.sep not in rel:                       # SPEC.md / INSTRUCTIONS.md at the root
            continue
        fmx, _b = parse_fm(open(f, encoding="utf-8", errors="replace").read())
        d = re.search(r'^description:\s*(.+)$', open(f, encoding="utf-8", errors="replace").read(), re.M)
        by_dir.setdefault(os.path.dirname(rel), []).append(
            ((fmx.get("title") or slug_of(rel)), rel,
             (d.group(1).strip().strip('"') if d else "")))
    for d, entries in sorted(by_dir.items()):
        open(os.path.join(a.wiki, d, "index.md"), "w", encoding="utf-8").write(render_index(d, entries))
    #  a parent index listing its children, so no directory is unreachable from the root
    #  every ancestor directory, up to and including the bundle root —— a directory reachable from
    #  nowhere is a directory nobody finds.  §8 calls this progressive disclosure.
    parents = set()
    for d in by_dir:
        while os.path.dirname(d):
            d = os.path.dirname(d)
            parents.add(d)
    parents.add("")                                    # the bundle root
    for p in sorted(parents):
        kids = [(os.path.basename(d) or d, os.path.join(d, "index.md").replace(os.sep, "/"),
                 "%d page(s)" % len(v))
                for d, v in sorted(by_dir.items()) if os.path.dirname(d) == p]
        kids += [(os.path.basename(c), os.path.join(c, "index.md").replace(os.sep, "/"), "")
                 for c in sorted(parents)
                 if c and os.path.dirname(c) == p and c not in by_dir]
        if kids:
            open(os.path.join(a.wiki, p, "index.md"), "w", encoding="utf-8").write(
                render_index(p, kids, root=(p == "")))

    #  ── the manifest.  Deterministic and sorted, so a no-change run leaves the file untouched ──
    man = {"generated_by": "process:openwiki_emit",
           "generated_at": datetime.datetime.now(datetime.timezone.utc)
                           .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
           "pages": sorted(({"path": "/" + rel.replace(os.sep, "/"), "title": title,
                             "sha256": hashlib.sha256(
                                 open(os.path.join(a.wiki, rel), "rb").read()).hexdigest()}
                            for entries in by_dir.values() for title, rel, _d in entries),
                           key=lambda x: x["path"])}
    open(os.path.join(a.wiki, MANIFEST), "w", encoding="utf-8").write(
        json.dumps(man, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    print(f"  ✅ {len(plan)} page(s) → {a.wiki}")
    for d, v in sorted(by_dir.items()):
        print(f"      {d}/  {len(v)}")
    print(f"  ✅ index.md × {len(by_dir) + len(parents)} · {MANIFEST}")
    if skipped:
        print(f"  ⓘ {skipped} file(s) skipped —— no frontmatter to convert")
    if unresolved:
        print(f"  ⚠ {len(unresolved)} wikilink(s) did not resolve (kept as text)")
    return 0


def _selftest():
    """Pins the two things whose failure is silent: ownership and the shrink guard."""
    import tempfile
    ok = []
    with tempfile.TemporaryDirectory() as d:
        wiki, src = os.path.join(d, "w"), os.path.join(d, "s")
        os.makedirs(src)
        subprocess.run(["git", "init", "-q", wiki], check=True)
        subprocess.run(["git", "-C", wiki, "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", wiki, "config", "user.name", "t"], check=True)

        def mk(name, agent="claude", extra=""):
            open(os.path.join(src, name), "w").write(
                f'---\ntitle: "{name[:-3]}"\ntype: conversation\ncaptured: 2026-09-02\n'
                f'session_id: id-{name[:-3]}\nsession_agent: {agent}\n'
                f'distilled_by: distill_sessions.py (LLM, needs review afterwards)\n'
                f'doc_type: analysis\nwhy_captured: "왜"\n{extra}---\n본문입니다.\n')
        for i in range(4):
            mk(f"a{i}.md", "claude" if i % 2 else "codex")

        sys.argv = ["x", "--wiki", wiki, "--from", src]
        assert main() == 0
        cl = os.path.join(wiki, "personal/sessions/claude")
        cx = os.path.join(wiki, "personal/sessions/codex")
        assert len(owned(cl)) == 2 and len(owned(cx)) == 2, "agents were not split"
        assert os.path.exists(os.path.join(cl, "index.md")), "no index.md"
        assert os.path.exists(os.path.join(wiki, MANIFEST)), "no manifest"
        ok.append("routes by agent · writes an index per directory · writes the manifest")

        #  ① Someone else's file in the destination survives a re-run.
        #     `promote_distilled` took a folder from 201 files to 3 by not doing this.
        mine = os.path.join(cl, "hand-written.md")
        open(mine, "w").write("---\ntitle: mine\ntype: note\n---\nA person wrote this.\n")
        att = os.path.join(cl, "diagram.png")
        open(att, "wb").write(b"\x89PNG")
        sys.argv = ["x", "--wiki", wiki, "--from", src]
        assert main() == 0
        assert os.path.exists(mine), "a hand-written page was deleted"
        assert os.path.exists(att), "an attachment was deleted"
        ok.append("a hand-written page and an attachment survive a re-run")

        #  ② The shrink guard fires when the source shrinks, and --force gets past it.
        for f in glob.glob(os.path.join(src, "*.md"))[:3]:
            os.remove(f)
        sys.argv = ["x", "--wiki", wiki, "--from", src]
        assert main() == 1, "the shrink guard did not fire when the source lost 3 of 4"
        sys.argv = ["x", "--wiki", wiki, "--from", src, "--force"]
        assert main() == 0, "--force did not get past the guard"
        ok.append("the shrink guard fires on a shrinking source, and --force overrides it")

        #  ③ Outside a git repository it refuses, because git revert is the only way back.
        nogit = os.path.join(d, "nogit")
        os.makedirs(nogit)
        sys.argv = ["x", "--wiki", nogit, "--from", src]
        assert main() == 1, "it ran outside a git repository"
        ok.append("refuses to run outside a git repository")

        #  ④ Exactly three extension keys reach the bundle.
        page = [f for f in glob.glob(os.path.join(cl, "*.md")) if os.path.basename(f) != "index.md"][0]
        txt = open(page).read()
        for k in ("doc_type", "why_captured"):
            assert re.search(rf"^{k}:", txt, re.M), f"{k} is missing"
        for k in ("origin", "distilled_from", "session_agent", "session_id"):
            assert not re.search(rf"^{k}:", txt, re.M), f"{k} leaked into the bundle"
        assert re.search(r"^status: draft$", txt, re.M), "an unreviewed page did not say draft"
        assert re.search(r"resource: \"(claude|codex)-session://", txt), "no session provenance"
        ok.append("three extensions, session provenance in sources, status stated")

        #  ⑤ An index carries no frontmatter —— except the bundle root, which may declare okf_version.
        #     (§8, §12).  A first version wrote a full block into every index and nothing rejected it.
        for ix in glob.glob(os.path.join(wiki, "**", "index.md"), recursive=True):
            body = open(ix, encoding="utf-8").read()
            at_root = os.path.dirname(ix) == wiki
            if at_root:
                assert body.startswith('---\nokf_version: "0.2"\n---\n'), \
                    "the bundle root index must declare okf_version and nothing else"
            else:
                assert not body.startswith("---"), f"{ix} carries frontmatter, which §8 forbids"
            assert "* [" in body or "(비어 있음)" in body, f"{ix} lists nothing"
        assert os.path.exists(os.path.join(wiki, "index.md")), "the bundle root has no index"
        ok.append("indexes carry no frontmatter, and the root declares okf_version (§8·§12)")

        #  ⑥ Ownership is decided in the frontmatter, never in the body.  A page that merely
        #     *documents* the format used to be deleted —— SPEC.md and INSTRUCTIONS.md are
        #     exactly such pages.  (reproduced by the security lens, 2026-09-02)
        doc = os.path.join(cl, "HOWTO.md")
        open(doc, "w").write(
            '---\ntitle: "How the format works"\ntype: note\n---\n'
            'Each emitted page carries `resource: "claude-session://<id>"` in its sources.\n')
        assert doc not in owned(cl), "a page documenting the format was classified as ours"
        #  ⚠ And a **third-party OKF page** —— one with a perfectly ordinary `resource:` and no
        #     session scheme —— is not ours either.  `resource` is a recommended OKF field, so
        #     relaxing the scheme requirement would classify every OKF page in the directory as
        #     ours and delete it.  A mutation to `^\s*resource:` used to leave every check green.
        foreign_okf = os.path.join(cl, "someone-elses-okf-page.md")
        open(foreign_okf, "w").write(
            '---\ntype: "Concept"\ntitle: "Theirs"\nresource: "/personal/whatever.md"\n'
            'sources:\n  - id: s1\n    resource: "https://example.org/"\n---\nBody.\n')
        assert foreign_okf not in owned(cl), "a third-party OKF page was classified as ours"
        sys.argv = ["x", "--wiki", wiki, "--from", src, "--force"]
        assert main() == 0
        assert os.path.exists(doc), "a page that only documents the format was deleted"
        assert os.path.exists(foreign_okf), "a third-party OKF page was deleted"
        ok.append("ownership needs the session scheme —— a doc page and a foreign OKF page survive")

        #  ⑫ Inside a repository is not the same as recoverable.  A bundle in .gitignore passes
        #     `rev-parse` and stays untracked, so `git revert` restores nothing.
        ign = os.path.join(d, "repo")
        subprocess.run(["git", "init", "-q", ign], check=True)
        os.makedirs(os.path.join(ign, "bundle"))
        open(os.path.join(ign, ".gitignore"), "w").write("bundle/\n")
        sys.argv = ["x", "--wiki", os.path.join(ign, "bundle"), "--from", src, "--force"]
        assert main() == 1, "a gitignored bundle was accepted —— git revert could not undo it"
        ok.append("a bundle inside .gitignore is refused, not just one outside a repository")

        #  ⑦ `--into` may not leave the bundle.  The git check validated the root; writes and
        #     deletions used to land wherever `--into` pointed, absolute or `../`.
        outside = os.path.join(d, "PRECIOUS")
        os.makedirs(outside, exist_ok=True)
        open(os.path.join(outside, "keep.md"), "w").write("do not touch\n")
        for bad in (os.path.join(d, "PRECIOUS"), "../PRECIOUS", "../../etc"):
            sys.argv = ["x", "--wiki", wiki, "--from", src, "--into", bad, "--force"]
            assert main() == 1, f"--into {bad} was accepted"
        assert open(os.path.join(outside, "keep.md")).read() == "do not touch\n"
        ok.append("--into cannot escape the bundle (absolute or ../)")

        #  ⑦b `--from` may not sit inside the bundle.  `openwiki-adopt` sets VAULT_DIR to the
        #      bundle, and `openwiki-vault` defaults its vault argument to VAULT_DIR —— so after a
        #      successful adopt the obvious next run would convert the bundle into itself.
        before = len(glob.glob(os.path.join(wiki, "**", "*.md"), recursive=True))
        for inside in (wiki, os.path.join(wiki, "personal")):
            sys.argv = ["x", "--wiki", wiki, "--from", inside, "--into", "personal/x", "--force"]
            assert main() == 1, f"--from {inside} was accepted"
        after = len(glob.glob(os.path.join(wiki, "**", "*.md"), recursive=True))
        assert before == after, f"a refused self-migration still wrote ({before} → {after})"
        ok.append("--from cannot be inside the bundle (the adopt → re-run self-migration)")

        #  ⑦c **A run must not delete pages in a *sub*directory it did not write to.**  Reproduced
        #      2026-09-02 (codex review, blocker #1): one root-level vault page lands at
        #      `personal/foo.md`, making `personal/` a touched directory, and the recursive sweep
        #      then removed every session page under `personal/sessions/**` —— `just openwiki`
        #      erasing path A while processing path B.  This is the promote_distilled 201→3 shape.
        deep = os.path.join(wiki, "personal", "sessions", "claude")
        os.makedirs(deep, exist_ok=True)
        keep = os.path.join(deep, "keep-me.md")
        open(keep, "w").write('---\ntitle: "s"\ntype: note\nsources:\n'
                              '  - resource: "claude-session://abc"\n---\n본문\n')
        rootsrc = os.path.join(d, "rootsrc")
        os.makedirs(rootsrc, exist_ok=True)
        open(os.path.join(rootsrc, "foo.md"), "w").write('---\ntitle: "v"\ntype: note\n---\n본문\n')
        sys.argv = ["x", "--wiki", wiki, "--from", rootsrc, "--into", "personal", "--force"]
        assert main() == 0
        assert os.path.exists(keep), \
            "a run into personal/ deleted a session page under personal/sessions/"
        ok.append("a run deletes only in the directories it writes to, never in their subtrees")

        #  ⑦d A hand-written page whose **block scalar** happens to contain a provenance line is
        #      not ours.  Reproduced 2026-09-02 (codex review, blocker #6): `description: |` with
        #      `resource: claude-session://…` beneath it was classified as owned and deleted.
        hand = os.path.join(wiki, "personal", "handwritten.md")
        open(hand, "w").write('---\ntitle: "mine"\ntype: note\ndescription: |\n'
                              '  resource: claude-session://example\n---\n본문\n')
        assert hand not in owned(os.path.dirname(hand)), "a block scalar made a page look owned"
        real = os.path.join(wiki, "personal", "sessions", "claude", "real.md")
        os.makedirs(os.path.dirname(real), exist_ok=True)
        open(real, "w").write('---\ntitle: "s"\ntype: note\nsources:\n'
                              '  - resource: "claude-session://abc"\n---\n본문\n')
        assert real in owned(os.path.dirname(real)), "a genuine session page stopped being owned"
        ok.append("ownership reads mapping entries, not block-scalar contents (both directions)")

        #  ⑦e `session_agent` becomes a path component and is **not** covered by the `--into`
        #      containment check.  Reproduced: it normalised to /tmp/PRECIOUS/x.md.
        assert agent_of({"session_agent": "../../../../tmp/PRECIOUS"}) == "tmpprecious"
        assert agent_of({"session_agent": "codex"}) == "codex"
        assert agent_of({"session_agent": "  /  "}) == "claude", "an empty agent must fall back"
        ok.append("session_agent cannot become a path —— it is whitelisted, not escaped")

        #  ⑧ A nested source tree migrates whole.  Non-recursive globbing moved 3 of 79 real
        #     vault files and printed a success line.
        nest = os.path.join(d, "nested")
        os.makedirs(os.path.join(nest, "a", "b"))
        for q, name in ((nest, "top.md"), (os.path.join(nest, "a"), "mid.md"),
                        (os.path.join(nest, "a", "b"), "deep.md")):
            open(os.path.join(q, name), "w").write(
                f'---\ntitle: "{name[:-3]}"\ntype: note\n---\n본문\n')
        sys.argv = ["x", "--wiki", wiki, "--from", nest, "--into", "personal/notes", "--force"]
        assert main() == 0
        got = glob.glob(os.path.join(wiki, "personal/notes", "**", "*.md"), recursive=True)
        got = [g for g in got if os.path.basename(g) != "index.md"]
        assert len(got) == 3, f"a nested tree migrated {len(got)} of 3"
        ok.append("a nested source tree migrates whole, keeping its subdirectories")

        #  ⑬ A source index.md never becomes a page (§8 forbids its frontmatter), and --exclude
        #     keeps an already-migrated subtree out of a run over its parent.
        open(os.path.join(nest, "index.md"), "w").write(
            '---\ntitle: "theirs"\ntype: index\n---\nlisting\n')
        os.makedirs(os.path.join(nest, "skipme"), exist_ok=True)
        open(os.path.join(nest, "skipme", "no.md"), "w").write(
            '---\ntitle: "no"\ntype: note\n---\n본문\n')
        sys.argv = ["x", "--wiki", wiki, "--from", nest, "--into", "personal/notes",
                    "--exclude", "/skipme/", "--force"]
        assert main() == 0
        assert not os.path.exists(os.path.join(wiki, "personal/notes/skipme/no.md")), \
            "--exclude did not keep the subtree out"
        ix = open(os.path.join(wiki, "personal/notes/index.md")).read()
        assert not ix.startswith("---"), "a source index.md overwrote the generated one"
        assert "theirs" not in ix, "the source index's own title leaked into the bundle"
        ok.append("a source index.md is not copied (§8) and --exclude keeps a subtree out")

        #  ⑨ The manifest and the root index cover the WHOLE bundle, not just this run.  A second
        #     run with a different --into used to drop the first run's pages from both.
        man = json.load(open(os.path.join(wiki, MANIFEST)))
        paths = {q["path"] for q in man["pages"]}
        assert any(q.startswith("/personal/notes/") for q in paths), "the notes run is missing"
        assert any(q.startswith("/personal/sessions/") for q in paths), \
            "the earlier sessions run was dropped from the manifest by a later --into run"
        #  The root lists `personal`; `personal/index.md` lists both directories under it.
        root_ix = open(os.path.join(wiki, "index.md")).read()
        assert "personal" in root_ix, "the root index does not reach personal/"
        mid_ix = open(os.path.join(wiki, "personal", "index.md")).read()
        assert "sessions" in mid_ix and "notes" in mid_ix, \
            "personal/index.md lost a directory a previous run created:\n" + mid_ix
        ok.append("manifest and root index span the whole bundle across runs")

        #  ⑩ A published page hand-marked `no_llm: true` keeps it through a re-emit.  It used to
        #     be deleted and rewritten from a source that never carries the key —— the user's
        #     "do not send this" survived exactly one run.
        page = [f for f in glob.glob(os.path.join(cx, "*.md"))
                if os.path.basename(f) != "index.md"][0]
        body = open(page).read()
        open(page, "w").write(body.replace("\ntype:", "\nno_llm: true\ntype:", 1))
        assert has_no_llm(page)
        sys.argv = ["x", "--wiki", wiki, "--from", src, "--force"]
        assert main() == 0
        assert has_no_llm(page), "the no_llm gate was silently dropped by a re-emit"
        assert re.search(r"^no_llm: true$", open(page).read(), re.M), "no_llm was quoted or moved"
        ok.append("a hand-set no_llm: true survives a re-emit")

        #  ⑪ Nothing is published without the leak check —— this is the one write that leaves
        #     ~/.kal (0700) for a git repository.
        bad = os.path.join(src, "leak.md")
        open(bad, "w").write('---\ntitle: "L"\ntype: note\n---\nkey sk-ant-api03-'
                             + "A" * 30 + "\n")
        sys.argv = ["x", "--wiki", wiki, "--from", src, "--force"]
        assert main() == 1, "a page carrying a secret was published"
        assert not os.path.exists(os.path.join(cl, "leak.md")), "it wrote before checking"
        os.remove(bad)
        ok.append("a secret in a page stops the publication before anything is written")

    for line in ok:
        print(f"  ✅ {line}")
    print("  ── every self-check above ran (read the list, do not count) ──")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
