#!/usr/bin/env python3
"""Entity resolution — the reference document is docs/ENTITY_RESOLVE.md

It gathers names that point at one thing, and splits one name that points at several.
**Both directions** live here (it was `entity_merge` until 2026-08-20 —— that name told half of it).

    joins    aliases.yml    several names → one node   load_aliases · build_canon · merge_key
    splits   homonyms.yml   one name → several nodes   load_homonyms · split_sense

Both read **rules a person confirmed**.  Automatic judgement does not reach the required
precision (all 2,315 pairs at cosine ≥0.93 were distinct), and a wrong split is as bad as a
wrong join.  Machines only propose candidates —— `alias_suggest.py` and `homonym_suggest.py`.

Why a separate module
  Name resolution is used in two places: lr_extract.group_nodes() (which fixes the node set and
  feeds the profile summary) and schema_v3.build_graph() (the final graph).  If they diverge,
  relations point at entities that do not exist — HIGH-1 of the 2026-08-17 review was of that
  family (id/name disagreement in 46% of relations).  One copy of the rules makes divergence
  impossible.

What it absorbs
  NFKC + lowercasing + removal of non-alphanumerics.  'Hermes Agent' == 'hermes-agent'

What it does not absorb (docs/ENTITY_RESOLVE.md §3)
  ① normalisation yields an empty string   names made only of symbols or emoji would all fuse into one
  ② only one side is path-shaped           'Hermes' (the agent) vs '~/.hermes' (its config directory)
"""
import os
import re
import unicodedata

# Notation denoting a path or the filesystem.  When only one side has this shape, the referents differ.
PATHY = re.compile(r"^[~./]|/$|^\.")
#  ⚠ **Known limitation, measured 2026-09-01.**  This allowlist keeps ASCII alphanumerics and
#     Hangul syllables and strips everything else, so scripts outside those two collapse:
#         'café' → 'caf'   ·   'Zoë' → 'zo'   ·   'naïve' → 'nave'
#         Greek, Cyrillic and Han normalise to '' entirely, which means "not a merge candidate"
#         and they simply never merge (they are not fused together —— an empty key stands alone,
#         see build_canon).  So nothing is wrongly joined; some things are wrongly kept apart,
#         and two accented names can collide once their accents are stripped.
#     It is not widened here because the key is what entity ids are built from: changing it
#     reshapes every id in an existing graph.  That belongs in its own change, with a re-index.
#     Hangul is listed explicitly because `\w` would also keep underscores and every other
#     script, which would let symbol-only names through the empty-key guard.
_STRIP = re.compile(r"[^0-9a-z가-힣]+")


# ── Aliases a person writes ──────────────────────────────────────────────
# Normalisation cannot catch everything: `taehyun` and its Hangul spelling use entirely different
# writing systems, and `PKM` against `personal knowledge management` is an abbreviation.  Judging
# by embedding similarity does not reach the required precision (docs/ENTITY_RESOLVE.md §6: all
# 2,315 pairs at cosine ≥0.93 were wrong).  So **only what a person confirmed** is accepted here.
ALIAS_PATH = os.environ.get(
    "KAL_ALIASES",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "aliases.yml"))

_ALIAS_CACHE = None
_ALIAS_LITERALS: dict[str, set] = {}   # representative → the strings a person actually wrote


HOMONYM_PATH = os.environ.get(
    "KAL_HOMONYMS",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "homonyms.yml"))

_HOM_CACHE = None


def load_homonyms(path=None):
    """→ {normalised name: [(new name, [cues…]), …]}.  An empty dict when the file is absent.

    **The opposite direction** to `aliases` —— that one joins several names into one; this one
    splits one name into several.

    Why it is needed: `vault` is both an Obsidian store and a 1Password vault, and fusing them
    into one node gives a degree-76 node mixing entirely unrelated neighbours.  The summarising
    LLM already writes "refers to two distinct systems" (17 measured cases), but that judgement
    stays in prose and never reaches the graph.
    """
    global _HOM_CACHE
    if _HOM_CACHE is not None and path is None:
        return _HOM_CACHE
    p = path or HOMONYM_PATH
    out = {}
    if os.path.exists(p):
        import yaml
        raw = yaml.safe_load(open(p, encoding="utf-8")) or {}
        for name, senses in raw.items():
            key = _norm(name)
            if not key or not senses:
                continue
            out[key] = [(x.get("name") or name,
                         [str(c).lower() for c in (x.get("when") or [])])
                        for x in senses]
    if path is None:
        _HOM_CACHE = out
    return out


def split_sense(name, text, doc=""):
    """Which sense this fragment belongs to → the name to use.

    Cues are matched case-insensitively as substrings against **the fragment description plus
    the source document's path**.  The path is included because, measured, it often already
    says which sense it is, as in `raw/conversations/sessions/1password-…`.

    The first match in order wins —— the file says to put narrower cues first.
    An entry with an empty `when` takes **everything else** (the default).  When nothing matches
    the original name is used as it is —— nothing is dropped quietly.
    """
    senses = load_homonyms().get(_norm(name))
    if not senses:
        return name
    hay = f"{text} {doc}".lower()
    for new, cues in senses:
        if not cues:                       # the default entry
            return new
        if any(c in hay for c in cues):
            return new
    return name


def load_aliases(path=None):
    """→ {normalised variant key: the representative display name}.  An empty dict when the file is absent.

    Format (aliases.yml):
        Taehyun Hwang:        # the spelling to use as the representative
          - taehyun
          - 태현
          - 황태현

    The representative itself is registered as a variant too —— writing only the representative works.
    """
    global _ALIAS_CACHE
    if _ALIAS_CACHE is not None and path is None:
        return _ALIAS_CACHE
    p = path or ALIAS_PATH
    out = {}
    if os.path.exists(p):
        import yaml
        raw = yaml.safe_load(open(p, encoding="utf-8")) or {}
        for canon, variants in raw.items():
            for name in [canon, *(variants or [])]:
                kv = _norm(name)
                if not kv:
                    continue
                # A name written under two groups lets the later one win silently —— and then a
                # representative the user did not intend gets attached.  Say so.
                if kv in out and out[kv] != canon:
                    import sys as _sys
                    print(f"  ⚠ aliases.yml: {name!r} appears under both '{out[kv]}' and '{canon}' "
                          f"— the later one is used", file=_sys.stderr)
                out[kv] = canon
    if path is None:
        _ALIAS_CACHE = out
    return out


def alias_literals(path=None):
    """representative → the strings a person wrote **verbatim**.

    Why the raw strings are needed —— normalisation makes `~/.hermes` and `Hermes` the same key.
    So asking "did a person write this" with a normalised key always answers yes.  Keeping the
    path-shape guard alive requires knowing whether a person wrote a path shape, and that only survives in the raw strings.
    """
    global _ALIAS_LITERALS
    if _ALIAS_LITERALS and path is None:
        return _ALIAS_LITERALS
    p = path or ALIAS_PATH
    out = {}
    if os.path.exists(p):
        import yaml
        raw = yaml.safe_load(open(p, encoding="utf-8")) or {}
        for canon, variants in raw.items():
            out.setdefault(canon, set()).update([canon, *(variants or [])])
    if path is None:
        _ALIAS_LITERALS = out
    return out


def _norm(s: str) -> str:
    """Pure normalisation for alias lookup —— it does not apply aliases (that would recurse forever)."""
    s = unicodedata.normalize("NFKC", (s or "")).lower()
    return _STRIP.sub("", s)


def merge_key(s: str) -> str:
    """The normalisation key.  An empty string means 'do not treat this as a merge candidate'.

    When the name is in the alias file it returns **the representative's key** —— which is how a
    Hangul spelling and `taehyun` land in the same bucket.
    """
    k = _norm(s)
    canon = load_aliases().get(k)
    return _norm(canon) if canon else k


def build_canon(names, stats=None):
    """lowercase name → representative lowercase name.

    names   a de-duplicated list of lowercase names
    stats   name -> (relation touches, document count).  Used to rank representative selection.
            Without it, only name length and alphabetical order decide.

    Representative selection: most relations → most documents → alphabetical.
    The final alphabetical tiebreak is what stops the representative changing between runs —
    an unstable entity_id makes the build unreproducible.

    The return holds **every** name (an unmerged one points at itself).
    That lets callers write `canon[k]` instead of `canon.get(k, k)`.
    """
    stats = stats or {}
    buckets = {}
    for n in names:
        buckets.setdefault(merge_key(n), []).append(n)

    A = load_aliases()
    canon = {}
    merged_groups = 0
    for key, group in buckets.items():
        # Is this a group a person wrote in the alias file.  If so, that judgement beats the
        # automatic rules below —— those (empty key, path shape) are a conservative default of
        # "no confidence, so do not merge", not a prohibition.  Once a person has checked, there
        declared = A.get(key)
        if declared is None:
            if len(group) == 1 or not key:
                # a single-member group, or a key that normalises to empty (symbols, emoji) → each stands alone
                for n in group:
                    canon[n] = n
                continue
            pathy = [bool(PATHY.search(n)) for n in group]
            if any(pathy) and not all(pathy):
                # only one side is path-shaped → a tool/concept vs its directory.  Not merged
                for n in group:
                    canon[n] = n
                continue
            rep = min(group, key=lambda n: (-stats.get(n, (0, 0))[0],
                                            -stats.get(n, (0, 0))[1], n))
        else:
            # The representative uses the spelling the person wrote.  Re-picking by degree would
            # make "taehyun" the representative even when the user wrote "Taehyun Hwang".
            rep = declared
            # ⚠ Declaring an alias **must not switch off the path-shape guard.**
            #   What the person approved is the names they wrote, not a name that happened to
            #   land in the same bucket through normalisation.  Measured: writing only
            #   `Hermes: [hermes-agent]` merged `~/.hermes` (the config directory) into the tool `Hermes`.
            #   (adversarial review 2026-08-18, correctness lens)
            # Did the person **write a path shape**.  If not, path shapes are not merged ——
            # asking with a normalised key makes `~/.hermes` into `hermes` and the answer always "yes".
            human_pathy = any(PATHY.search(str(v))
                              for v in alias_literals().get(declared, ()))
            if not human_pathy:
                for n in list(group):
                    if PATHY.search(n):
                        canon[n] = n        # a path shape the person never approved
                        group = [g for g in group if g != n]
            if not group:
                canon.setdefault(rep, rep)
                continue
            if len(group) == 1 and group[0] == rep:
                canon[rep] = rep
                continue
        for n in group:
            canon[n] = rep
        canon.setdefault(rep, rep)
        merged_groups += 1
    return canon, merged_groups


def display_name(key, forms):
    """Choose the name shown in the graph.

    key    the representative key build_canon returned (a person's spelling, or lowercase)
    forms  {original spelling: occurrences}

    A spelling written in aliases.yml **wins**.  Otherwise the most frequent spelling, and
    alphabetical order on a tie (reproducibility).

    ⚠ The lookup must go through merge_key(key).  The alias map is stored under normalised
      keys, while key can carry the capitals a person wrote, so looking up by key directly
      **always** misses.  That is exactly why the most frequent spelling 'taehyun' appeared
      instead of 'Taehyun Hwang' (2026-08-18).
    """
    declared = load_aliases().get(merge_key(key))
    if declared:
        return declared
    if forms:
        return min(forms, key=lambda n: (-forms[n], n))
    return key


def name_stats(entities, relationships, name_of=lambda e: e.get("name", ""),
               docs_of=lambda e: e.get("docs", ()),
               ends_of=lambda r: (r.get("source", ""), r.get("target", ""))):
    """(relation touches, document count) for representative selection.  Aggregated by lowercase name."""
    touch = {}
    for r in relationships:
        s, t = ends_of(r)
        for n in (s, t):
            n = (n or "").strip().lower()
            if n:
                touch[n] = touch.get(n, 0) + 1
    out = {}
    for e in entities:
        n = (name_of(e) or "").strip().lower()
        if not n:
            continue
        prev = out.get(n, (0, 0))
        out[n] = (touch.get(n, 0), max(prev[1], len(list(docs_of(e)))))
    for n, c in touch.items():
        out.setdefault(n, (c, 0))
    return out


if __name__ == "__main__":
    # Self-check — do the reference document's §2 and §3 cases come out as written
    assert merge_key("Hermes Agent") == merge_key("hermes-agent") == "hermesagent"
    assert merge_key("AI 2027") == merge_key("AI-2027") == "ai2027"
    assert merge_key("환경변수") == merge_key("환경 변수") == "환경변수"
    assert merge_key("✅/△/❌") == "" and merge_key("🔴/🟠") == ""

    stats = {"hermes agent": (68, 18), "hermes-agent": (26, 14),
             "hermes": (74, 22), "~/.hermes": (3, 1),
             "docs/": (8, 4), "src/": (1, 1)}
    canon, n = build_canon(list(stats), stats)
    assert canon["hermes-agent"] == "hermes agent", canon["hermes-agent"]
    assert canon["~/.hermes"] == "~/.hermes", "one side path-shaped → merging is forbidden"
    assert canon["hermes"] == "hermes"
    # 'raw' is not path notation (no leading ~/ or ./ and no trailing /) → a mixed group with 'raw/'
    c2, _ = build_canon(["✅/△/❌", "🔴/🟠"])
    # ⚠ **Asserting which side becomes the representative guards nothing.**  Both have the key ""
    #   so they land in one bucket, and once merged the smaller code point wins —— that is, even
    #   when merged, the first assertion below is still true.  Only that line existed once, and
    #   deleting the guard still passed (mutation test, 2026-08-21).  Watch the side that changes.
    assert c2["🔴/🟠"] == "🔴/🟠", ("empty keys were merged", c2)
    assert c2["✅/△/❌"] == "✅/△/❌", c2

    # Tie reproducibility — a different input order must give the same representative
    a, _ = build_canon(["Beta", "beta"], {"Beta": (5, 1), "beta": (5, 1)})
    b, _ = build_canon(["beta", "Beta"], {"Beta": (5, 1), "beta": (5, 1)})
    assert a == b and a["Beta"] == "Beta", (a, b)
    # ── Aliases ──
    #  ⚠ The fixtures below **stay in Korean on purpose.**  What aliases.yml exists for is
    #     exactly the case normalisation cannot reach —— two writing systems naming one person ——
    #     and replacing them with two Latin spellings would leave the cross-script path untested
    #     while still passing.  The prose around them is English; the data is the subject.
    import tempfile, textwrap
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(textwrap.dedent("""
            Taehyun Hwang:
              - taehyun
              - 태현
              - 황태현
            PKM:
              - personal knowledge management
        """))
        tmp = fh.name
    # This is module top level, so no global declaration is needed (and here it would be a syntax error)
    _ALIAS_CACHE = load_aliases(tmp)
    _ALIAS_LITERALS = alias_literals(tmp)
    try:
        # different writing systems, the same key
        assert merge_key("태현") == merge_key("taehyun") == merge_key("Taehyun Hwang")
        assert merge_key("PKM") == merge_key("Personal Knowledge Management")
        # anything not in the aliases is left alone
        assert merge_key("Jinwoo") != merge_key("진우")
        # the representative is the spelling the person wrote
        c3, _ = build_canon(["taehyun", "태현", "황태현"],
                            {"taehyun": (43, 20), "태현": (8, 3), "황태현": (1, 1)})
        assert c3["태현"] == "Taehyun Hwang", c3
        assert c3["taehyun"] == "Taehyun Hwang", c3
        # does an alias override the empty-key rule (a person checked, so merge)
        assert merge_key("Personal knowledge management") == merge_key("pkm")

        # display name —— the spelling a person wrote must beat the most frequent one.
        # It must be found even when key carries capitals (lookup key normalisation).
        forms = {"taehyun": 15, "태현": 3, "황태현": 1}
        assert display_name("Taehyun Hwang", forms) == "Taehyun Hwang", \
            display_name("Taehyun Hwang", forms)
        # not in the aliases → the most frequent spelling
        assert display_name("obsidian", {"Obsidian": 9, "obsidian": 2}) == "Obsidian"
        # a tie falls back to alphabetical (changing per run would destabilise entity_id)
        assert display_name("x", {"Beta": 3, "Alpha": 3}) == "Alpha"
    finally:
        os.unlink(tmp)
    # Declaring an alias must leave the path-shape guard alive —— what a person approved is the
    # names they wrote, not a name that happened to land in the same bucket through normalisation.
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False,
                                     encoding="utf-8") as fh:
        fh.write("Hermes:\n  - hermes-agent\n")
        tmp3 = fh.name
    _ALIAS_CACHE = load_aliases(tmp3)
    _ALIAS_LITERALS = alias_literals(tmp3)
    try:
        st3 = {"hermes": (74, 22), "hermes-agent": (26, 14),
               "hermes agent": (68, 18), "~/.hermes": (3, 1)}
        c4, _ = build_canon(sorted(st3), st3)
        assert c4["hermes-agent"] == "Hermes", c4
        assert c4["~/.hermes"] == "~/.hermes", ("a path shape was merged", c4)
    finally:
        _ALIAS_CACHE = None
        _ALIAS_LITERALS = {}
        os.unlink(tmp3)
    # When a person **writes the path shape explicitly**, it is merged
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False,
                                     encoding="utf-8") as fh:
        fh.write("Hermes:\n  - hermes-agent\n  - ~/.hermes\n")
        tmp4 = fh.name
    _ALIAS_CACHE = load_aliases(tmp4)
    _ALIAS_LITERALS = alias_literals(tmp4)
    try:
        c5, _ = build_canon(sorted(st3), st3)
        assert c5["~/.hermes"] == "Hermes", ("written explicitly and still not merged", c5)
    finally:
        _ALIAS_CACHE = None
        _ALIAS_LITERALS = {}
        os.unlink(tmp4)
    print(f"  ✅ self-check passed · {n} merge group(s) · 5 alias cases · 3 display-name cases · 2 path-shape guards")
