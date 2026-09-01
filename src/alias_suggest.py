#!/usr/bin/env python3
"""Find alias candidates and show them to a person.  **It never merges automatically.**

Why a person has to confirm
  `taehyun` and its Hangul spelling use different writing systems and can never be caught by
  string comparison.  Judging by embedding similarity does not reach the required precision ——
  there are 2,315 pairs at cosine ≥0.93 and all of them are distinct (docs/ENTITY_RESOLVE.md
  §6).  Some pairs cannot be settled even by a person reading the documents.

  So this only **ranks candidates and shows them**.  Confirmation is a person writing into
  aliases.yml.  A wrong automatic merge quietly poisons the graph; a wrong candidate in a list
  is something a person simply passes over.

How candidates are chosen (all deterministic, no randomness)
  ① romanised folding   Hangul is transliterated by Revised Romanisation, then vowels and
                        consonants are collapsed.  Both spellings land on the same key
  ② abbreviation        'PKM' vs 'Personal Knowledge Management' initials
  ③ containment         'taehyun' ⊂ 'taehyun hwang'
  ④ matching type       a person pairs only with a person.  Different types are dropped.

  Any one of ①–③ plus ④ makes a candidate.  Embeddings are not used —— §6 has already shown
  that approach does not work.

Usage:
    python alias_suggest.py                 print the candidates as a table
    python alias_suggest.py --json          for machines
    python alias_suggest.py --min-docs 2    only pairs where both sides have N or more documents
"""
import argparse
import collections
import itertools
import json
import os
import re
import sys


# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from entity_resolve import load_aliases, merge_key, _norm  # noqa: E402

DB = os.environ.get("KAL_PATH", os.path.join(KAL_HOME, "db"))

# ── Revised Romanisation (Hangul syllables → Latin) ──────────────────────
# Perfect RR is not the goal.  fold() below collapses vowels anyway, so it only has to bring
# spelling variants to the same place.
# ⚠ Some positions are blank, which is why this is not built by splitting a string —— an earlier
#   version was `"  k k ...".split(" ")` and the two leading spaces made two empty elements, so
#   every final consonant shifted by one.  A ㄴ was read as ㄱ and taehyeon became taehyeok.
_CHO = ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "",
        "j", "jj", "ch", "k", "t", "p", "h"]
_JUNG = ["a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae",
         "oe", "yo", "u", "wo", "we", "wi", "yu", "eu", "ui", "i"]
_JONG = ["", "k", "k", "k", "n", "n", "n", "t", "l", "k", "m", "l", "l", "l",
         "p", "l", "m", "p", "p", "t", "t", "ng", "t", "t", "k", "t", "p", "t"]
assert len(_CHO) == 19 and len(_JUNG) == 21 and len(_JONG) == 28


def romanize(s: str) -> str:
    out = []
    for ch in s:
        c = ord(ch)
        if 0xAC00 <= c <= 0xD7A3:
            i = c - 0xAC00
            out.append(_CHO[i // 588] + _JUNG[(i % 588) // 28] + _JONG[i % 28])
        else:
            out.append(ch)
    return "".join(out)


# The same name gets written differently by different people: taehyun / taehyeon / taehyon.
# The folding below brings those variants to one place.  **Order matters —— longest first.**
#
# ⚠ 'woo' has to come before 'oo'.  Behind it, jinwoo becomes jinwu and stops matching jinu.
#   That is exactly the pair the documentation uses as its example, and it was not caught.
#   (2026-08-18, caught by the self-check)
_FOLD = [("woo", "u"), ("wu", "u"), ("eo", "u"), ("eu", "u"), ("oo", "u"),
         ("ae", "e"), ("oe", "e"), ("wo", "o"),
         ("kk", "k"), ("tt", "t"), ("pp", "p"), ("ss", "s"), ("jj", "j"),
         ("ch", "c"), ("k", "g"), ("t", "d"), ("p", "b")]


def squash(s: str) -> str:
    """Collapse doubled letters into one.  It must be applied **identically to both sides** when comparing fused spellings."""
    return re.sub(r"(.)\1+", r"\1", s)


def fold(s: str) -> str:
    s = romanize(_norm(s))
    for a, b in _FOLD:
        s = s.replace(a, b)
    return squash(s)


def tokens(name: str):
    """A name as a comparable token set.  It absorbs name order (family-first vs given-first)."""
    parts = re.split(r"[\s_\-./]+", (name or "").strip().lower())
    return {fold(p) for p in parts if fold(p)}


HANGUL = re.compile(r"[가-힣]")

# Rules with usable precision.  The rest (containment) appears only under --weak.
STRONG = ("romanised match", "fused spelling", "abbreviation")


def acronym_of(name: str) -> str:
    """Initials.  **3 letters or more only** —— 2 letters match by coincidence far too often.

    Measured: allowing length 2 paired 'AI' with APPROVER_IDS, Acoustic Interference and
    Algorithmic improvements.  All of them wrong.
    """
    parts = [p for p in re.split(r"[\s_\-./]+", (name or "").strip()) if p]
    a = "".join(p[0] for p in parts).lower() if len(parts) >= 2 else ""
    return a if len(a) >= 3 else ""


def why(a: str, b: str):
    """Why the two names are a candidate.  None if they are not."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return None
    fa, fb = fold(a), fold(b)
    # Folding exists to absorb **the variation that arises when Hangul is romanised**.  With
    # Latin on both sides there is no basis for folding —— measured, 'ABI' and 'api' were joined
    cross = bool(HANGUL.search(a)) != bool(HANGUL.search(b))
    if fa == fb and cross:
        return "romanised match"
    if fa == fb and not cross and _norm(a) != _norm(b):
        return None      # two Latin names matching only after folding is not evidence
    # A Hangul name has no spaces and is one token, while a romanisation splits family and
    # given name, in the opposite order.  The fused side is matched against permuted joins of
    # the other side's tokens.  Up to 3 tokens (6 permutations) —— beyond that it is not a name.
    for one, many in ((ta, tb), (tb, ta)):
        if len(one) == 1 and 2 <= len(many) <= 3:
            fused = next(iter(one))
            # ⚠ The fused side went through squash; if the joined side does not, they never
            #   match.  A three-syllable name squashes to one form while the join produced
            #   another, and they disagreed (caught by the self-check).
            if any(squash("".join(p)) == fused for p in itertools.permutations(sorted(many))):
                return "fused spelling"
    if ta & tb and (ta <= tb or tb <= ta):
        return "token containment"
    if acronym_of(a) and acronym_of(a) == _norm(b):
        return "abbreviation"
    if acronym_of(b) and acronym_of(b) == _norm(a):
        return "abbreviation"
    if len(fa) >= 4 and len(fb) >= 4 and (fa in fb or fb in fa):
        return "substring"
    return None


def load_entities():
    import lancedb
    db = lancedb.connect(DB)
    return db.open_table("lr_entities").search().limit(999999).to_list()


def suggest(entities, min_docs=1, weak=False):
    """→ the candidate list.

    ⚠ Performance —— an exhaustive comparison is 8.4 million pairs even within a type (3,443
    concepts → 5.9 million).  At first fold() was recomputed per pair and it took **5m 13s**.
    Two changes brought it down:
      ① fold, tokens and the abbreviation are computed **once** per entity
      ② the strong rules (romanised, fused, abbreviation) are dictionary lookups, so O(n).  The
         exhaustive pass runs only for the containment rule, and that is not the default.
    """
    known = load_aliases()
    by_type = collections.defaultdict(list)
    for e in entities:
        n = (e.get("name") or "").strip()
        if not n or len(e.get("doc_ids") or []) < min_docs:
            continue
        by_type[e.get("type") or "other"].append(e)

    out, seen = [], set()

    def add(typ, a, b, reason):
        na, nb = a["name"].strip(), b["name"].strip()
        if merge_key(na) == merge_key(nb):
            return                                  # they already merge
        key = (typ, *sorted((na, nb)))
        if key in seen:
            return
        seen.add(key)
        out.append({
            "type": typ, "reason": reason,
            "a": na, "a_docs": len(a.get("doc_ids") or []),
            "b": nb, "b_docs": len(b.get("doc_ids") or []),
            "already_aliased": bool(known.get(_norm(na)) or known.get(_norm(nb))),
        })

    for typ, group in by_type.items():
        # Pairs are made within one type only.  A person and a tool cannot be the same thing.
        group = sorted(group, key=lambda x: x["name"])
        pre = []
        for e in group:
            n = e["name"].strip()
            pre.append((e, n, fold(n), tokens(n), acronym_of(n), _norm(n),
                        bool(HANGUL.search(n))))

        # ① romanised match —— same folded key.  One side must be Hangul.
        byfold = collections.defaultdict(list)
        for rec in pre:
            byfold[rec[2]].append(rec)
        for bucket in byfold.values():
            if len(bucket) < 2:
                continue
            for x, y in itertools.combinations(bucket, 2):
                if x[6] != y[6]:
                    add(typ, x[0], y[0], "romanised match")

        # ② abbreviation —— initials looked up as a normalised name (a dictionary lookup, O(n))
        bynorm = collections.defaultdict(list)
        for rec in pre:
            bynorm[rec[5]].append(rec)
        for rec in pre:
            if rec[4]:
                for other in bynorm.get(rec[4], ()):
                    if other[0] is not rec[0]:
                        add(typ, rec[0], other[0], "abbreviation")

        # ③ fused spelling —— a single token looked up as a permuted join of several tokens
        # ⚠ As a dict, single tokens sharing a fold key overwrite each other so only the last
        #   survives and those pairs disappear (absent from the current corpus, but a structural defect).
        fused = collections.defaultdict(list)
        for rec in pre:
            if len(rec[3]) == 1:
                fused[rec[2]].append(rec)
        for rec in pre:
            if not (2 <= len(rec[3]) <= 3):
                continue
            for perm in itertools.permutations(sorted(rec[3])):
                for other in fused.get(squash("".join(perm)), ()):   # the same folding as why()
                    if other[0] is not rec[0]:
                        add(typ, rec[0], other[0], "fused spelling")

        # ④ containment —— the only exhaustive pass.  Low precision, so it is not on by default.
        if weak:
            for x, y in itertools.combinations(pre, 2):
                # why()'s Latin-to-Latin refusal is honoured here too.  Without it, pairs that
                # match only after folding come back (ABI/api and similar).
                # (the adversarial review of 2026-08-18 measured 7 such pairs)
                if x[2] == y[2] and x[6] == y[6] and x[5] != y[5]:
                    continue
                if x[3] & y[3] and (x[3] <= y[3] or y[3] <= x[3]):
                    add(typ, x[0], y[0], "token containment")
                elif len(x[2]) >= 4 and len(y[2]) >= 4 and (x[2] in y[2] or y[2] in x[2]):
                    add(typ, x[0], y[0], "substring")

    # Pairs with more documents first —— so the ones that actually affect the graph are seen first
    out.sort(key=lambda r: -(r["a_docs"] + r["b_docs"]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--min-docs", type=int, default=1,
                    help="only pairs where both sides have at least this many documents (default 1)")
    ap.add_argument("--limit", type=int, default=40)
    # The containment rule has low precision —— 'Obsidian' and 'Obsidian Web Clipper' are different things.
    # Measured: of 457 pairs in total only 66 came from strong rules and most of the rest were wrong.
    # So it is off by default and has to be asked for with --weak.
    ap.add_argument("--weak", action="store_true",
                    help="include the containment rule (low precision — many wrong pairs)")
    a = ap.parse_args()

    rows = suggest(load_entities(), a.min_docs, weak=a.weak)
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return

    if not rows:
        print("  no candidates.")
        return
    print(f"  {len(rows)} alias candidate pair(s) — **nothing is merged automatically.** "
          f"Write only the correct ones into aliases.yml.\n")
    print(f"  {'reason':<18}{'A':<26}{'docs':>5}   {'B':<26}{'docs':>5}  type")
    print(f"  {'-'*12}{'-'*26}{'-'*5}   {'-'*26}{'-'*5}  {'-'*12}")
    for r in rows[:a.limit]:
        mark = " *" if r["already_aliased"] else ""
        print(f"  {r['reason']:<12}{r['a'][:25]:<26}{r['a_docs']:>5}   "
              f"{r['b'][:25]:<26}{r['b_docs']:>5}  {r['type']}{mark}")
    if len(rows) > a.limit:
        print(f"  … and {len(rows)-a.limit} more pair(s) (see more with --limit)")
    print("\n  * = one side is already in aliases.yml")
    print("  To apply, edit aliases.yml then:  python refresh_kg.py --force")


if __name__ == "__main__":
    # Self-check —— a silent change to the rules makes candidates vanish or flood
    #  ⚠ These fixtures **stay in Korean.**  Romanised folding exists for exactly this case,
    #     and two Latin spellings would leave the whole rule untested while still passing.
    assert fold("태현") == fold("taehyun"), (fold("태현"), fold("taehyun"))
    assert why("taehyun", "태현") == "romanised match"
    assert why("황태현", "taehyun hwang") is not None, "a different name order must still match"
    assert why("PKM", "Personal Knowledge Management") == "abbreviation"
    assert why("BM25", "LanceDB") is None, "an unrelated pair must not match"
    assert why("Obsidian", "Obsidian Plugin") is not None
    # Does the fast implementation give **the same answer** as the slow one.  Moving to buckets
    # took it from 5m 13s to 1.3s, and candidates could vanish silently in that change.
    # The two are compared on a small sample (this self-check runs without a DB, so it lives here).
    _sample = [
        # ⚠ A name already in the alias file gets the same merge_key and drops out of the
        #   candidates.  That would leave the rule unchecked, so **names absent from the file** are used.
        {"name": "jinwoo", "type": "person", "doc_ids": [1] * 15},      # ↔ its Hangul spelling (romanised)
        {"name": "진우", "type": "person", "doc_ids": [1] * 3},
        {"name": "홍길동", "type": "person", "doc_ids": [1]},            # ↔ gildong hong (fused spelling)
        {"name": "gildong hong", "type": "person", "doc_ids": [1]},
        {"name": "PKM", "type": "concept", "doc_ids": [1] * 10},
        {"name": "Personal Knowledge Management", "type": "concept", "doc_ids": [1] * 15},
        {"name": "Obsidian", "type": "tool", "doc_ids": [1] * 64},
        {"name": "Obsidian Web Clipper", "type": "tool", "doc_ids": [1] * 11},
        {"name": "R&D", "type": "concept", "doc_ids": [1]},
        {"name": "ABI", "type": "concept", "doc_ids": [1]},
        {"name": "api", "type": "concept", "doc_ids": [1] * 7},
    ]
    _new = {(r["type"], *sorted((r["a"], r["b"])))
            for r in suggest(_sample, 1, weak=False) if r["reason"] in STRONG}
    _by = collections.defaultdict(list)
    for _e in _sample:
        _by[_e["type"]].append(_e)
    _old = set()
    for _t, _g in _by.items():
        for _a, _b in itertools.combinations(sorted(_g, key=lambda x: x["name"]), 2):
            _na, _nb = _a["name"].strip(), _b["name"].strip()
            if merge_key(_na) == merge_key(_nb):
                continue
            if why(_na, _nb) in STRONG:
                _old.add((_t, *sorted((_na, _nb))))
    assert _new == _old, ("the fast implementation disagrees with the slow one",
                          "missing", sorted(_old - _new), "extra", sorted(_new - _old))
    assert ("concept", "ABI", "api") not in _new, "Latin-to-Latin folding is not evidence"
    assert ("tool", "Obsidian", "Obsidian Web Clipper") not in _new, "containment is not a strong rule"

    if "--selftest" in sys.argv:
        print(f"  ✅ self-check passed — 5 rules · fast and slow agree ({len(_new)} pairs)")
        raise SystemExit(0)
    main()
