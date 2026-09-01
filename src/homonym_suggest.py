#!/usr/bin/env python3
"""Find homonym candidates and show them to a person.  **It never splits automatically.**

The opposite direction to `alias_suggest.py`:

    alias_suggest    different names that look like one thing   → aliases.yml
    homonym_suggest  one name that looks like different things  → homonyms.yml

Why a person has to confirm
  A wrong split is as bad as a wrong join.  Whether `role` means a duty or an IAM role can
  only be settled by a person reading the documents.  Splitting automatically quietly poisons
  (docs/ENTITY_RESOLVE.md §6).  A wrong candidate in a list is passed over; a wrong split is not.

How candidates are chosen (all deterministic, no randomness)
  ① senses      what the summarising LLM emitted as structure.  It already carries `label` and
                `cue`, so it can be used verbatim.  **The best candidates.**
  ② prose signal  a description saying "refers to two distinct…" while carrying no senses.
                Entities summarised under an older prompt (before t2) land here.  The cue has
                to be written by a person.
  ③ document clusters  the source documents split into two non-overlapping groups.  Neither
                senses nor a prose signal, but the neighbourhood looks suspicious.  **The
                weakest signal**, so it is off by default (`--weak`).

The output is YAML that can be pasted straight into `homonyms.yml`.

    python src/homonym_suggest.py              # ①②
    python src/homonym_suggest.py --weak       # ①②③
    python src/homonym_suggest.py --yaml       # only the YAML to paste
"""
import argparse, collections, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lancedb                                                    # noqa: E402
from entity_resolve import load_homonyms, merge_key               # noqa: E402

# The path convention must match the rest of the repository —— `KAL_HOME` + `KAL_PATH`.
# Inventing a new name `KAL_DB` once left it empty in the container, so it fell back to
# `~/.kal/db`, which held no tables, and it returned 500.
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
DB = os.environ.get("KAL_PATH", os.path.join(KAL_HOME, "db"))

#  ⚠ The Korean alternatives below **stay.**  These match what the summarising LLM wrote,
#     and it writes in the conversation's language (distill_sessions' prompt), so a
#     Korean-language vault produces Korean descriptions.  Dropping them would make this
#     signal silently find nothing on exactly the vault it was measured against.
# Phrasings in which a summary says "several senses" itself.  Spellings vary, so it is caught
# broadly (Korean phrasings included) —— this list found 17 measured cases.
PROSE = re.compile(
    r"two distinct|two different|two meanings|both a |refers to (?:two|both)"
    r"|여러 의미|두 가지 의미|다른 의미|서로 다른 (?:두|시스템|개념)"
    r"|distinct (?:systems|concepts|meanings|entities)", re.I)

# Words excluded from cue candidates —— they appear in every sense (Korean stopwords included).
STOP = {"the", "a", "an", "and", "or", "of", "in", "for", "to", "is", "it",
        "this", "that", "with", "as", "by", "on", "at", "from", "system",
        "data", "file", "files", "use", "used", "using", "관리", "저장",
        "시스템", "사용", "구성", "지원", "제공", "위한", "통해", "대한"}


def entities():
    t = lancedb.connect(DB).open_table("lr_entities")
    return t.to_arrow().to_pylist()


def already_split():
    """Names that already have a rule in homonyms.yml.  Not proposed again."""
    return set(load_homonyms())


def from_senses(rows):
    """① what the summarising LLM already structured."""
    out = []
    for r in rows:
        try:
            sn = json.loads(r.get("senses") or "[]")
        except Exception:
            continue
        if len(sn) >= 2:
            out.append((r, sn, "senses"))
    return out


def from_prose(rows, have_senses):
    """② it says so in prose but carries no senses.

    An entity summarised under an older prompt.  A machine cannot build the `cue`, so a person
    has to read the fragments and write it —— hence an empty `when` is emitted with a comment saying so.
    """
    out = []
    for r in rows:
        if r["entity_id"] in have_senses:
            continue
        if PROSE.search(r.get("description") or ""):
            out.append((r, [], "prose"))
    return out


def from_clusters(rows, seen_ids, min_docs=6):
    """③ the source documents split cleanly into two groups.

    It looks at the source documents of the events, and when the document set divides into
    **two non-overlapping clumps** it may be polysemy.  A weak signal —— far more often it is
    one thing handled in different contexts.  So it is off by default.
    """
    out = []
    for r in rows:
        if r["entity_id"] in seen_ids or len(r.get("doc_ids") or []) < min_docs:
            continue
        try:
            ev = json.loads(r.get("events") or "[]")
        except Exception:
            continue
        # Pull characteristic words out of the event text and group them by document
        by_doc = collections.defaultdict(set)
        for e in ev:
            d = e.get("doc_id")
            if d is None:
                continue
            by_doc[d] |= {w for w in re.findall(r"[\w-]{3,}", (e.get("text") or "").lower())
                          if w not in STOP}
        if len(by_doc) < 4:
            continue
        docs = list(by_doc)
        # Two clumps if more than half the documents share no word with the first one
        base = by_doc[docs[0]]
        far = [d for d in docs[1:] if not (by_doc[d] & base)]
        if len(far) >= len(docs) * 0.4:
            out.append((r, [], "cluster"))
    return out


def cue_from_events(row, limit=6):
    """Pull cue candidates from the event text —— material shown to a person to choose from."""
    try:
        ev = json.loads(row.get("events") or "[]")
    except Exception:
        return []
    words = collections.Counter()
    for e in ev:
        for w in re.findall(r"[\w-]{3,}", (e.get("text") or "").lower()):
            if w not in STOP and not w.isdigit():
                words[w] += 1
    name_key = merge_key(row.get("name") or "")
    return [w for w, _ in words.most_common(30) if merge_key(w) != name_key][:limit]


def emit_yaml(cands, only_yaml=False):
    print("# ── candidates to paste into homonyms.yml ──")
    print("# ⚠ Do not use them as they are.  **Read the fragments and confirm.**  A wrong split")
    print("#   is as bad as a wrong join.  A cue must be a word that appears in that sense alone.")
    for r, sn, why in cands:
        print(f"\n# {r['name']}  (degree {r.get('degree', 0)} · "
              f"{len(r.get('doc_ids') or [])} document(s) · grounds: {why})")
        if why != "senses":
            hint = cue_from_events(r)
            print(f"#   summary: {(r.get('description') or '')[:120]}")
            print(f"#   frequent words in the events: {hint}")
            print(f"#   ↓ fill the cues in **yourself**.  An empty when takes everything else.")
        print(f"{r['name']}:")
        if sn:
            for x in sn:
                cues = ", ".join(f'"{c}"' for c in x["cue"])
                print(f"  - name: {x['label']} {r['name']}")
                print(f"    when: [{cues}]")
        else:
            print(f"  - name: <sense 1> {r['name']}")
            print(f"    when: []      # ← fill this in")
            print(f"  - name: <sense 2> {r['name']}")
            print(f"    when: []")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weak", action="store_true", help="include the document-cluster signal (many wrong pairs)")
    ap.add_argument("--yaml", action="store_true", help="print the YAML only")
    a = ap.parse_args()

    rows = entities()
    done = already_split()
    rows = [r for r in rows if merge_key(r.get("name") or "") not in done]

    a1 = from_senses(rows)
    have = {r["entity_id"] for r, _, _ in a1}
    a2 = from_prose(rows, have)
    seen = have | {r["entity_id"] for r, _, _ in a2}
    a3 = from_clusters(rows, seen) if a.weak else []

    cands = a1 + a2 + a3
    cands.sort(key=lambda t: -(t[0].get("degree") or 0))

    if not a.yaml:
        print(f"  {len(rows)} entity(ies) ({len(done)} already split, excluded)")
        print(f"  ① senses  {len(a1)}   ← the LLM gave label and cue.  The best")
        print(f"  ② prose   {len(a2)}   ← it says so but has no cue.  A person must fill it in")
        print(f"  ③ cluster {len(a3)}   ← a weak signal" + ("" if a.weak else "  (enable with --weak)"))
        if not cands:
            print("\n  no candidates.")
            return
        print()
        for r, sn, why in cands[:40]:
            tag = {"senses": "★", "prose": "·", "cluster": "?"}[why]
            lab = " / ".join(x["label"] for x in sn) if sn else ""
            print(f"  {tag} {r['name'][:30]:<30} deg {r.get('degree',0):>3} "
                  f"docs {len(r.get('doc_ids') or []):>2}  {lab}")
        print()
    emit_yaml(cands, a.yaml)


if __name__ == "__main__":
    main()
