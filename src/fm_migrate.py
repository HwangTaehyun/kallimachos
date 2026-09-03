#!/usr/bin/env python3
"""Plant **generation provenance** into the vault's frontmatter (taken from OKF §5.1 provenance).

Why —— the vault currently lacks two axes:
  ① "what made this document, and when" ——> `generated_by` · `generated_at`
  ② "a handle pointing at the source of each claim" ——> the `sN:` prefix on `sources` entries

`verified` is **deliberately not written.**  A trust marker saying a person reviewed something,
created by a machine, makes that whole grade false.  It has to appear when a person writes it.

## Why not nested YAML

OKF writes `generated: {by, at}` and `sources: [{id, ...}]` **nested**.
But this pipeline's frontmatter parser (`okf_convert.parse_fm`) reads only two shapes,
`key: scalar` and `key: [a, b]`.  Used nested as is:

    generated:          ->  generated becomes empty and
      by: x                 `by` and `at` **pollute the top level**
    sources: [{id: s1, ref: x}]
                        ->  splits on commas and **breaks** into `['{id: s1', 'ref: x}']`

Switching to yaml.safe_load is not safe either.  Measured (377 documents, 2026-08-19):
351 agree · 4 where YAML is right (block-style tags) · **21 where YAML fails to parse**
(strings containing a colon) · and **2 where YAML is wrong** ——
`id: 001` -> `1` (the zeros lost), `duration: 52:32` -> `3152` (YAML 1.1 sexagesimal).

So the vault holds **the flat shape** the parser reads exactly, and the nesting is built by
`okf_convert.py` **on export**.  The vault need not be OKF-shaped ——
it only needs the information.

## The mapping

    distilled_by        ->  generated_by     (the name of the LLM distillation script)
    captured            ->  generated_at     (its timestamp)
    sources: ["x"]      ->  sources: ["s1:x"]

`generated_at` is **not** taken from `updated`.  `updated` is when a person edited the
document, not when a machine generated it.  What is unknown is left empty.

Usage:
    python fm_migrate.py --selftest
    python fm_migrate.py                 # dry-run —— shows only what would change
    python fm_migrate.py --apply
"""
import vault_path
import argparse
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

VAULT = vault_path.vault()

#  The one fence, in its split form —— this module rebuilds the delimiters, so it needs the
#  three groups.  It used to carry its own narrow copy, which a BOM or a leading blank line
#  slipped past silently.  See src/frontmatter.py.
from frontmatter import FM_RE_PARTS as FM_RE
KEY_RE = re.compile(r"^([A-Za-z_][\w-]*):\s*(.*)$")
# Is the sN: prefix already there —— a re-run must not produce s1:s1:x
ID_PREFIXED = re.compile(r"^s(\d+):")   # group 1 = the number (id_sources takes the maximum)


def split_fm(text):
    """(the leading '---\\n', the frontmatter lines without the body, the trailing '\\n---\\n', the body).  None when absent."""
    m = FM_RE.match(text)
    if not m:
        return None
    return m.group(1), m.group(2).split("\n"), m.group(3), text[m.end():]


def read_keys(lines):
    """Take only `key: value` from the frontmatter lines.  The same rule as parse_fm."""
    out = {}
    for ln in lines:
        k = KEY_RE.match(ln)
        if k:
            out[k.group(1)] = k.group(2).strip()
    return out


def quote(v):
    """So a value mixing colons and quotes does not break.  parse_fm strips the outer quotes."""
    v = str(v)
    return '"%s"' % v.replace('"', "'") if (":" in v or v.startswith(("[", "{", '"'))) else v


def id_sources(val):
    """`["a", "b"]` -> `["s1:a", "s2:b"]`.  Already prefixed, it is left alone (idempotent)."""
    inner = val.strip()
    if not (inner.startswith("[") and inner.endswith("]")):
        return None                       # not a list: left untouched
    items = [x.strip().strip('"\'') for x in inner[1:-1].split(",") if x.strip()]
    if not items:
        return None
    #  ⚠ `n` **starts from the highest number already present**.  It used to start at 0 and
    #    count only the unprefixed ones: append a source to an already-migrated document
    #    (`["s1:a"]`) and `["s1:a", "b"]` → `["s1:a", "s1:b"]` —— **two `s1`s**.
    #    `okf_convert.parse_fm` reading that yields two `- id: "s1"` entries, and the whole
    #    feature of pointing each claim at a source **points two different documents at one
    #    name**.  Worse, it is stably idempotent, so it never fixes itself.
    #    (reproduced by r4-silent, 2026-08-21)
    out = []
    n = max((int(m.group(1)) for it in items if (m := ID_PREFIXED.match(it))),
            default=0)
    for it in items:
        if ID_PREFIXED.match(it):
            out.append(it)                # already handled
        else:
            n += 1
            out.append("s%d:%s" % (n, it))
    new = "[%s]" % ", ".join('"%s"' % x for x in out)
    return None if new == inner else new


def migrate(text):
    """(the new text, a list of what changed).  Nothing to change gives (the original, [])."""
    parts = split_fm(text)
    if not parts:
        return text, []
    head, lines, tail, body = parts
    fm = read_keys(lines)
    changes, out = [], list(lines)

    # ① assign ids to sources —— replaced in place (line order preserved)
    if "sources" in fm:
        new = id_sources(fm["sources"])
        if new:
            for i, ln in enumerate(out):
                k = KEY_RE.match(ln)
                if k and k.group(1) == "sources":
                    out[i] = "sources: %s" % new
                    changes.append("sources -> ids assigned")
                    break

    # ② generated_by / generated_at —— written **only when both have an origin**
    add = []
    if "generated_by" not in fm and fm.get("distilled_by"):
        add.append("generated_by: %s" % quote(fm["distilled_by"]))
        changes.append("generated_by <- distilled_by")
    if "generated_at" not in fm and fm.get("captured"):
        add.append("generated_at: %s" % quote(fm["captured"]))
        changes.append("generated_at <- captured")
    out.extend(add)

    if not changes:
        return text, []
    return head + "\n".join(out) + tail + body, changes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=VAULT)
    ap.add_argument("--apply", action="store_true", help="actually write (the default is a dry-run)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    from schema_v3 import is_skipped
    files = [f for f in sorted(glob.glob(f"{a.vault}/**/*.md", recursive=True))
             if not is_skipped(f)]
    hit, tally = 0, {}
    for f in files:
        t = open(f, encoding="utf-8", errors="ignore").read()
        new, ch = migrate(t)
        if not ch:
            continue
        hit += 1
        for c in ch:
            tally[c] = tally.get(c, 0) + 1
        if a.apply:
            open(f, "w", encoding="utf-8").write(new)

    print("  %s —— %d of %d document(s) affected" %
          ("applied" if a.apply else "dry-run (nothing written)", hit, len(files)))
    for k, v in sorted(tally.items()):
        print("    %-28s %d" % (k, v))
    if not a.apply:
        print("\n  To actually write it: python src/fm_migrate.py --apply")
        print("  To undo             : git -C %s checkout -- ." % a.vault)


def selftest():
    # The body does not change by a single character
    t = ('---\ntitle: "T"\ncaptured: 2026-06-25\n'
         'distilled_by: distill_sessions.py (LLM, needs review afterwards)\n---\nThe body.\n')
    new, ch = migrate(t)
    assert new.endswith("---\nThe body.\n"), "the body changed"
    # A value with no colon gets no quotes —— the contract is **the round trip**, not the spelling
    assert "generated_by: distill_sessions.py (LLM, needs review afterwards)" in new, new
    assert "generated_at: 2026-06-25" in new, new
    # A value containing a colon must be quoted or parse_fm breaks
    colon = migrate('---\ntitle: "T"\ncaptured: 2026-06-25\n'
                    'distilled_by: "tool: v2"\n---\nbody\n')[0]
    from okf_convert import parse_fm as _pf
    assert _pf(colon)[0]["generated_by"] == "tool: v2", _pf(colon)[0]

    # Idempotent —— two runs give the same thing
    again, ch2 = migrate(new)
    assert ch2 == [] and again == new, "it is not idempotent"

    # ids are added to sources, and a re-run adds no duplicate prefix
    s = '---\ntitle: "T"\nsources: ["a", "b"]\n---\nbody\n'
    n1, _ = migrate(s)
    assert 'sources: ["s1:a", "s2:b"]' in n1, n1
    n2, c2 = migrate(n1)
    assert c2 == [] and n2 == n1, "sources is not idempotent"

    # With no origin it creates nothing —— what is unknown is not invented
    bare = '---\ntitle: "T"\nupdated: 2026-05-14\n---\nbody\n'
    assert migrate(bare) == (bare, []), "updated was invented as generated_at"

    # With no frontmatter it is left alone
    assert migrate("only a title\n") == ("only a title\n", [])

    # Does the existing parser read the new keys exactly —— that is this design's premise
    from okf_convert import parse_fm
    fm, _ = parse_fm(n1)
    assert fm["sources"] == ["s1:a", "s2:b"], fm["sources"]
    fm2, _ = parse_fm(new)
    assert fm2["generated_at"] == "2026-06-25", fm2
    assert fm2["generated_by"].startswith("distill_sessions.py"), fm2
    # There must be no top-level pollution (the reason nested YAML is not used)
    assert "by" not in fm2 and "at" not in fm2, "the top level was polluted"    # ── are the ids **free of collisions** ──
    #    Appending a source to an already-migrated document is an ordinary thing to do.  Then
    #    `["s1:a", "b"]` → `["s1:a", "s1:b"]` gave **two s1s**.
    #    `okf_convert.parse_fm` reading that yields two `- id: "s1"` entries, and the whole
    #    feature of pointing each claim at a source points two different documents at one name.
    #    It is stably idempotent, so it never fixes itself either.  (r4-silent)
    for _inp, _want in (('["a"]',                  '["s1:a"]'),
                        ('["s1:a", "b"]',          '["s1:a", "s2:b"]'),
                        ('["s1:a", "s2:b", "c"]',  '["s1:a", "s2:b", "s3:c"]'),
                        ('["s3:a", "b"]',          '["s3:a", "s4:b"]'),
                        ('["s1:a"]',               None)):          # None when there is nothing to do
        _got = id_sources(_inp)
        assert _got == _want, f"the id numbering disagrees: {_inp} → {_got} (expected {_want})"
    #    The same number must never appear twice in the result —— an invariant, regardless of the case above
    for _inp in ('["s1:a", "b", "c"]', '["s2:a", "s5:b", "c", "d"]'):
        _out = id_sources(_inp) or _inp
        _ids = re.findall(r'"s(\d+):', _out)
        assert len(_ids) == len(set(_ids)), f"ids collide: {_inp} → {_out}"



    print("  ✅ fm_migrate self-check — body intact · idempotent · nothing created without an origin · parser round trip")


if __name__ == "__main__":
    main()
