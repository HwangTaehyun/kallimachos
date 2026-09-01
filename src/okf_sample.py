#!/usr/bin/env python3
"""Build **the 4 sample-vault conditions** of the OKF comparison experiment.

Why it exists —— without this script the experiment was not reproducible.  Selecting the
60-document sample and injecting `x_pad` were done by hand inside a session, and following the
documented procedure literally hit a wall because it assumed `sample-base` already existed.

What it builds

    sample-base    a systematic sample of 60 documents from the original    the baseline
    sample-rerun   **byte-identical** to base                               pure LLM non-determinism
    sample-shift   base + meaningless padding (in the frontmatter)          frontmatter-length perturbation
    sample-okf     the same 60 documents from okf-vault                     the OKF format

Sampling method: **systematic** —— the sort key is the vault-root-relative path in `sorted()`'s
default string order, and every `len(docs)//N`-th document is taken.
It is **not stratified** —— doc_type proportions are not matched.

Note: shift's padding does not isolate "boundary shifting".  Measured (deep review), only 13 of
the 60 documents actually changed chunk count; for the rest only the chunk **contents** change.
So this condition is named **'frontmatter-length perturbation'**, not 'boundaries'.

Usage:
    python okf_sample.py                 # build all 4 conditions
    python okf_sample.py --n 60
    python okf_sample.py --selftest
"""
import vault_path
import argparse
import glob
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
VAULT = vault_path.vault()
EXP = os.path.join(KAL_HOME, "exp")

# Meaningless padding placed in the frontmatter.  Its length is matched to the OKF conversion's
# (OKF grew the sample from 130 chunks to 141 = about 440 characters per document).
PAD_LINE = 'x_pad: "' + ("padding " * 55).strip() + '"\n'


def harden():
    """Bind every directory and file created afterwards to owner-only.

    What goes in here is **unconverted vault source** (more sensitive than an okf bundle).
    chmod covers the root only, so blocking the subtree requires the umask.
    (deep review 2026-08-19, round 5 — round 4 fixed only okf_convert)
    """
    os.umask(0o077)


def pick(vault, n):
    """A systematic sample.  Sorted, then evenly spaced —— deterministic, so two runs give the same sample."""
    from schema_v3 import is_skipped, clean
    docs = []
    for f in sorted(glob.glob(f"{vault}/**/*.md", recursive=True)):
        if is_skipped(f):
            continue
        body, _ = clean(open(f, encoding="utf-8", errors="ignore").read())
        if len(body) < 60:          # the same discard threshold as indexing and extraction
            continue
        docs.append(os.path.relpath(f, vault))
    step = max(1, len(docs) // n)
    return docs, docs[::step][:n]


def pad(text):
    """Insert a meaningless key after the frontmatter's first line.  With no frontmatter, unchanged."""
    return ("---\n" + PAD_LINE + text[4:]) if text.startswith("---\n") else text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=VAULT)
    ap.add_argument("--okf", default=os.path.join(EXP, "okf-vault"),
                    help="the bundle okf_convert.py produced")
    ap.add_argument("--out", default=EXP)
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    if not os.path.isdir(a.okf):
        raise SystemExit(f"no OKF bundle: {a.okf}\n  Run okf_convert.py first.")

    total, sample = pick(a.vault, a.n)
    dirs = {k: os.path.join(a.out, f"sample-{k}") for k in
            ("base", "rerun", "shift", "okf")}
    harden()
    for d in dirs.values():
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o700)          # this is a vault copy.  Make it protect itself

    missing, padded = [], 0
    for rel in sample:
        src = os.path.join(a.vault, rel)
        okf_src = os.path.join(a.okf, rel)
        if not os.path.exists(okf_src):
            missing.append(rel)
            continue
        text = open(src, encoding="utf-8", errors="ignore").read()
        for k, content in (("base", text), ("rerun", text), ("shift", pad(text)),
                           ("okf", open(okf_src, encoding="utf-8", errors="ignore").read())):
            dst = os.path.join(dirs[k], rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            open(dst, "w", encoding="utf-8").write(content)
        if pad(text) != text:
            padded += 1

    n = len(sample) - len(missing)
    print(f"  {len(total)} documents total → a systematic sample of {n} (every {max(1, len(total)//a.n)})")
    print(f"    {padded}/{n} document(s) padded  (documents with no frontmatter are left alone)")
    if missing:
        print(f"    ⚠ {len(missing)} excluded, absent from the OKF bundle: {missing[:3]}")
    for k, d in dirs.items():
        print(f"    sample-{k:<6} {sum(1 for _ in glob.glob(d + '/**/*.md', recursive=True))} document(s)  {d}")
    print("\n  Next: run each condition in isolation (KAL_HOME must be kept separate)")
    print("    for v in base rerun shift okf; do")
    print("      mkdir -p ~/.kal/exp/h-sample-$v")
    print("      KAL_VAULT=~/.kal/exp/sample-$v KAL_HOME=~/.kal/exp/h-sample-$v \\")
    print("        python src/lr_extract.py")
    print("    done")


def selftest():
    t = '---\ntitle: "A"\n---\nbody\n'
    p = pad(t)
    assert p.startswith("---\nx_pad:"), p
    assert p.endswith("body\n") and 'title: "A"' in p, "the body and existing keys must be preserved"
    assert len(p) - len(t) > 400, f"the padding is too short: {len(p) - len(t)}"
    # With no frontmatter it is left alone (touching it would change the condition for that document alone)
    assert pad("body only\n") == "body only\n"
    # Determinism —— the same input gives the same sample
    assert pad(t) == pad(t)
    # The outputs must be 0600/0700.  **The self-check must not set the umask itself** ——
    # that would let it pass with harden() deleted from the production path (which really
    # happened in round 5).  It starts from a loose umask and checks after calling harden().
    import tempfile as _tf, stat as _st
    _old = os.umask(0o022)
    try:
        harden()
        with _tf.TemporaryDirectory() as _d:
            _p = os.path.join(_d, "sub"); os.makedirs(_p)
            _f = os.path.join(_p, "x.md"); open(_f, "w").write("x")
            assert _st.S_IMODE(os.stat(_f).st_mode) == 0o600, "the file is world-readable"
            assert _st.S_IMODE(os.stat(_p).st_mode) == 0o700, "the directory is world-readable"
    finally:
        os.umask(_old)
    # "Does harden() work" and "does main() call it" are different propositions.
    # The block above checks only the first.  Lose the call site and the outputs quietly go back to 0644.
    # Comment lines are stripped before checking —— a mutation that commented it out as
    # `# harden()` simply passed (2026-08-19, round 6).  That is the common case in real debugging.
    import inspect as _in
    _live = "\n".join(l for l in _in.getsource(main).splitlines()
                       if not l.lstrip().startswith("#"))
    assert "harden()" in _live, "main() does not call harden() (including when commented out)"

    print("  ✅ okf_sample self-check — padding preserved · length · no frontmatter · determinism")


if __name__ == "__main__":
    main()
