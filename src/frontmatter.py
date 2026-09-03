#!/usr/bin/env python3
"""The one frontmatter fence.

There were **twelve copies of this regex across seven files**, in two variants that disagreed:
a wide one that tolerates a BOM, leading blank lines, CRLF and a `...` terminator, and a narrow
`\\A---\\n(.*?)\\n---\\n` that does not.  `NO_LLM_RE` was once six copies for the same reason, and
collapsing it to one is why the transmission gate stopped drifting.

What the narrow one cost, measured 2026-09-04 on real files through the production read path:

    spelling      body  >=60  title     frontmatter in body   indexed
    plain LF        11  False  A page   False                 no
    BOM             90  True   (none)   True                  yes
    blank 1st line  89  True   (none)   True                  yes
    ... terminator  89  True   (none)   True                  yes

`clean()` returns `t.strip()` — the **whole file** — when the fence misses, so on those three
spellings the entire frontmatter became the body.  That bypasses `FM_KEEP`, the allowlist whose
own comment says it decides "what is indexed locally"; a `secret_key_id` in the frontmatter
reached `chunks` verbatim and its terms reached the BM25 inverted index (verified end to end by
indexing into a scratch DB and reading the tables back).  The document was also indexed at all,
despite a three-character body, because the frontmatter carried it over the 60-character floor.

⚠ CRLF is **not** among them, and the difference is worth keeping: production reads through
   `open(path, encoding="utf-8")`, and Python's universal newlines turn `\\r\\n` into `\\n` before
   the regex sees it.  Passing a CRLF string straight to `clean()` reproduces a defect that the
   file path does not have —— both of us did exactly that while reviewing this.

⚠ **Changing this fence is a rebuild, not a sync.**  `content_hash` is `sha256(raw)` on both
   sides (`status.py:164`, `schema_v3.py`), so a document whose *parsed* form changes while its
   bytes do not is neither added nor modified —— the incremental path cannot see it, and its stale
   entry (frontmatter embedded in the chunk text, empty title) survives until someone edits the
   file or a full `index` runs.  Generally: **anything that changes what `clean()` computes is
   invisible to `sync` and requires a rebuild.**  Measured against the live corpus when this fence
   was widened, the delta was zero —— 1,114 of 1,116 matched under both spellings and 0 changed ——
   so no migration was needed then.  The sentence is here for the next fence or `FM_KEEP` change,
   when it will not be.  (adversarial review 2026-09-04)

⚠ **Nothing needs `\r?\n` today, and it stays anyway.**  Every caller reads through
   `open(..., encoding="utf-8")`, so universal newlines have already removed the `\r`.  This
   paragraph used to name `openwiki_enrich` as the one that needed it, on the theory that it
   matches against LLM replies —— **that is false**: its `body_of`/`insert_keys` take the file
   text (`openwiki_enrich.py:258,277`), and the model's reply goes only to `parse_reply`, which
   extracts JSON and never touches a fence.  (adversarial review 2026-09-04, verified)

   It is kept as defence in depth —— a byte-mode reader or a network-sourced string would need
   it and costs nothing to tolerate.  The honest reason is written here **because a comment that
   names a caller which does not exist is not protection**: the next person checks that caller,
   finds it does not do what the comment says, and now has grounds to delete the tolerance *and*
   distrust the paragraph.  This repository has paid for that shape twice already.
"""
import re


#  The block itself, as one group.  This is the form eleven of the twelve sites want.
FM_RE = re.compile(r"\A﻿?\s*---[ \t]*\r?\n(.*?)\r?\n(?:---|\.\.\.)[ \t]*\r?\n", re.S)

#  The same fence split into (opening, block, closing), for the one caller that rewrites the block
#  in place and has to put the delimiters back exactly as it found them.  Built from the same
#  pieces as FM_RE so the two cannot drift —— that drift is the whole reason this file exists.
_OPEN, _BLOCK, _CLOSE = r"\A﻿?\s*---[ \t]*\r?\n", r"(?:.*?)", r"\r?\n(?:---|\.\.\.)[ \t]*\r?\n"

#  Just the opening delimiter —— "this text was **trying** to carry frontmatter", which is what
#  `doc_meta` asks before failing the transmission gate closed on an unparsable block.  Sharing
#  the opener means a fence that stops matching here also stops being recognised as an attempt,
#  instead of the two answers drifting apart.
#
#  ⚠ This was briefly defined **twice** —— a botched edit left one line with a literal BOM and a
#     second with `\ufeff`.  The later one silently won, so a mutation against the first had no
#     effect and the mutation sweep reported "the check does not fire" when it does.  A duplicate
#     assignment is invisible to the reader and to the mutation.
FM_OPEN_RE = re.compile(r"\A\ufeff?\s*---")

FM_RE_PARTS = re.compile(rf"({_OPEN})({_BLOCK})({_CLOSE})", re.S)


def _selftest():
    FM = "type: note\ntitle: A page"
    cases = {
        "plain":     f"---\n{FM}\n---\nbody\n",
        "BOM":       f"﻿---\n{FM}\n---\nbody\n",
        "blank 1st": f"\n---\n{FM}\n---\nbody\n",
        "dots":      f"---\n{FM}\n...\nbody\n",
        "CRLF":      f"---\r\n{FM}\r\n---\r\nbody\r\n".replace("\n", "\n"),
        "trailing␠": f"--- \n{FM}\n--- \nbody\n",
    }
    for name, raw in cases.items():
        m = FM_RE.match(raw)
        assert m, f"the fence missed {name}"
        assert "title: A page" in m.group(1), f"{name}: the block is not what was captured"
        assert raw[m.end():].strip() == "body", f"{name}: the body slice is wrong ({raw[m.end():]!r})"
        p = FM_RE_PARTS.match(raw)
        assert p and p.group(1) + p.group(2) + p.group(3) == raw[:m.end()], \
            f"{name}: the split form does not reassemble to what FM_RE matched"
    #  ⚠ Both directions.  A fence that matches everything would pass every case above, and the
    #     narrow one it replaces at least refused a file with no frontmatter.
    for name, raw in {"no fence": "just a body\n",
                      "fence later": "text\n---\na: b\n---\n",
                      "unclosed": f"---\n{FM}\nbody with no close\n"}.items():
        assert not FM_RE.match(raw), f"the fence matched {name}, which has none"
    print("  ✅ one frontmatter fence —— BOM · blank first line · CRLF · `...` · trailing spaces, "
          "and it still refuses a file with no block")


if __name__ == "__main__":
    _selftest()
