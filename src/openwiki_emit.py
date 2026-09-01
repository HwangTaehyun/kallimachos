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

KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
WIKI = os.environ.get("OPENWIKI_DIR", os.path.expanduser("~/github/HwangTaehyun/openwiki"))
DISTILLED = os.path.join(KAL_HOME, "distilled")

#  A page this tool wrote carries a session URI in its sources.  That is **derived from real
#  provenance** rather than a marker planted for the purpose —— a marker can be copied onto a
#  hand-written page by accident, and then deleting "our own" files takes it too.
OWNED = re.compile(r"^\s*resource:\s*\"?(?:claude|codex)-session://", re.M)
#  For a migration run there is no session URI, so the tool stamps the *directory* instead:
#  everything under the target subdirectory is ours because we created that subdirectory.
MANIFEST = ".page-manifest.json"
SHRINK_RATIO = 0.8


def git_ok(root):
    return subprocess.run(["git", "-C", root, "rev-parse", "--git-dir"],
                          capture_output=True).returncode == 0


def owned(d):
    """Pages under `d` that carry session provenance —— the ones this tool may replace."""
    out = []
    for f in glob.glob(os.path.join(d, "**", "*.md"), recursive=True):
        if os.path.basename(f) == "index.md":
            continue
        try:
            if OWNED.search(open(f, encoding="utf-8", errors="ignore").read(4000)):
                out.append(f)
        except OSError:
            pass
    return out


def foreign(d):
    """Everything else under `d` —— never touched.  Counted so a run can say what it left alone."""
    own = set(owned(d))
    out = []
    for root, _dirs, files in os.walk(d):
        for f in files:
            p = os.path.join(root, f)
            if p not in own and os.path.basename(p) not in (MANIFEST, "index.md"):
                out.append(p)
    return out


def agent_of(fm):
    return (fm.get("session_agent") or "claude").strip().lower()


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
    fm, body = parse_fm(open(src, encoding="utf-8").read())
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
    ap.add_argument("--force", action="store_true", help="proceed past the shrink guard")
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()

    if not os.path.isdir(a.src):
        print(f"❌ no such source folder: {a.src}")
        return 1
    if not git_ok(a.wiki):
        print(f"❌ the bundle is not a git repository: {a.wiki}\n"
              f"   This replaces pages, and `git revert` is the only way back.\n"
              f"     git -C {a.wiki} init && git -C {a.wiki} add -A && git -C {a.wiki} commit -m init")
        return 1

    files = sorted(glob.glob(os.path.join(a.src, "*.md")))
    if not files:
        print(f"❌ no .md under {a.src}")
        return 1

    #  Wikilinks resolve against the *source* folder, the way okf_convert does for the vault.
    idx = build_link_index(files, a.src)
    unresolved = set()

    #  ── plan first, write later.  Nothing is removed before the whole run is known good ──
    plan, skipped = [], 0
    for f in files:
        fm, _ = parse_fm(open(f, encoding="utf-8").read())
        if not fm:
            skipped += 1
            continue
        sub = a.into or os.path.join("personal", "sessions", agent_of(fm))
        rel = os.path.join(sub, os.path.basename(f))
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

    #  ── write ──
    for d in touched_dirs:
        os.makedirs(d, exist_ok=True)
        for old in owned(d):
            os.remove(old)
    for rel, text, _t, _d in plan:
        dst = os.path.join(a.wiki, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        open(dst, "w", encoding="utf-8").write(text)

    #  ── indexes, one per directory that holds pages, plus the roots above them ──
    by_dir = {}
    for rel, _text, title, desc in plan:
        by_dir.setdefault(os.path.dirname(rel), []).append((title, rel, desc))
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
                             "sha256": hashlib.sha256(text.encode()).hexdigest()}
                            for rel, text, title, _d in plan), key=lambda x: x["path"])}
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

    for line in ok:
        print(f"  ✅ {line}")
    print("  ── every self-check above ran (read the list, do not count) ──")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
