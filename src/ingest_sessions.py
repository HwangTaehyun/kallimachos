#!/usr/bin/env python3
"""Claude session logs → searchable documents.

(gold-in/gold-out).  Applied verbatim to 471 sessions it breaks both the skill's reason for
The brain-ingest skill is a curation tool where a person judges one document at a time
existing and the vault's principles.  So vault files stay untouched and these load into **a separate namespace**.

The pipeline:
  1. parse the jsonl — assistant/user bodies only.  attachment, system, mode are dropped
  2. **mask secrets** — sk-ant / sk-proj / AKIA / xox / ghp and others become [REDACTED:type]
  3. remove noise — tool output dumps, duplicates and very short lines
  4. one session = one document (session_docs)

Masking is applied before indexing, so no secret reaches chunks, vectors or the KG.
"""
import os, re, sys, json, glob, hashlib, collections, argparse


# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
SESS = os.path.expanduser("~/.claude/projects")
OUT = os.environ.get("KAL_SESSIONS", os.path.join(KAL_HOME, "sessions"))

# ── Secret patterns.  All of them substituted before indexing ──
SECRETS = [
    ("ANTHROPIC_KEY", re.compile(r"sk-ant-api\d{2}-[A-Za-z0-9_\-]{20,}")),
    ("OPENAI_KEY",    re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}")),
    ("OPENAI_LEGACY", re.compile(r"\bsk-[A-Za-z0-9]{48}\b")),
    ("GITHUB_TOKEN",  re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("AWS_KEY",       re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("AWS_SECRET",    re.compile(r"(?i)(aws_secret_access_key\s*[=:]\s*)[A-Za-z0-9/+=]{40}")),
    ("SLACK_TOKEN",   re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("SLACK_WEBHOOK", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+")),
    ("BEARER",        re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{30,}")),
    # A complete PEM block.  Tried first so the body goes with it.
    ("PRIVATE_KEY",   re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,4000}?-----END [A-Z ]*PRIVATE KEY-----")),
    # A block cut off without END.  Session logs frequently truncate tool output mid-stream,
    # leaving the header and part of the body.  The pattern above requires END and missed it (found by measurement).
    ("PRIVATE_KEY_TRUNC", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,4000}")),
    ("JWT",           re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),

    # ── Added by the adversarial review of 2026-08-18.  24 of 30 synthetic samples passed
    #    straight through the list above.  Below are the ones that could realistically appear in this vault.
    #
    # Claude Code's own credential format.  The ANTHROPIC_KEY above requires a literal `api\d{2}`
    # and misses it —— ~/.claude/.credentials.json gets cat'd in sessions often.
    ("ANTHROPIC_OAUTH", re.compile(r"sk-ant-(?:oat|sid|admin)\d{2}-[A-Za-z0-9_\-]{20,}")),
    ("OPENAI_SVCACCT",  re.compile(r"\bsk-svcacct-[A-Za-z0-9_\-]{20,}")),
    ("GITHUB_PAT_FG",   re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("SLACK_APP",       re.compile(r"\bxox[ed]-[A-Za-z0-9\-]{10,}\b")),
    ("SLACK_APP_LEVEL", re.compile(r"\bxapp-\d-[A-Za-z0-9\-]{10,}\b")),
    ("AWS_STS",         re.compile(r"\bASIA[0-9A-Z]{16}\b")),
    ("GOOGLE_API_KEY",  re.compile(r"\bAIza[A-Za-z0-9_\-]{35}\b")),
    ("STRIPE_KEY",      re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{20,}\b")),
    ("GITLAB_PAT",      re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b")),
    ("NPM_TOKEN",       re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("HF_TOKEN",        re.compile(r"\bhf_[A-Za-z0-9]{34,}\b")),
    ("TELEGRAM_TOKEN",  re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_\-]{32,}\b")),
    ("DISCORD_WEBHOOK", re.compile(r"https://(?:discord|discordapp)\.com/api/webhooks/\d+/[A-Za-z0-9_\-]+")),
    # A password embedded in a connection string.  Measured: one survived even in a
    # session_docs.json that had been masked (postgresql://user:pw@host).  Nothing above catches it.
    ("DB_URI",          re.compile(r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|amqps)://[^\s:/@]+:[^\s@/]{6,}@")),
    # The last net —— a high-entropy value attached to a name like KEY/TOKEN/SECRET/PASSWORD.
    # Listing every shape is impossible, so this catches by **context** rather than shape.
    # A false positive only means something gets masked, so the direction is safe.
    #   Why `(?<![A-Za-z0-9])` is needed —— without it, the 'Password' in an **item name** like
    #   "1Password:domain-key-acls" matches (3 measured false positives).  Requiring a digit or
    #   a capital on the value side serves the same purpose —— an identifier of lowercase and
    #   hyphens alone, like 'domain-key-acls', is not a secret.
    ("GENERIC_SECRET",  re.compile(
        r"(?i)((?<![A-Za-z0-9])(?:api[_\-]?key|secret[_\-]?key|access[_\-]?token|"
        r"auth[_\-]?token|client[_\-]?secret|password|passwd)\s*[=:]\s*[\"']?)"
        r"(?=[A-Za-z0-9_\-/+=]{16,})(?=[^\s\"']*(?-i:[A-Z0-9]))[A-Za-z0-9_\-/+=]{16,}")),
]

# ── Noise — a block starting with these patterns is dropped whole ──
NOISE_PREFIX = (
    "<system-reminder>", "<command-name>", "<local-command-",
    "Caveat: The messages below", "[Request interrupted",
)
NOISE_RE = [
    re.compile(r"^\s*\|.{0,4}\|.{0,4}\|"),          # a table dump
    re.compile(r"^\s*(\d+→|\s{4,}\d+\t)"),          # cat -n output
    re.compile(r"^[\s\-=_*#]{40,}$"),               # a separator rule
]


def find_leaks(blob):
    """Secrets surviving the masking.  `{pattern name: count}` —— empty means clean.

    This judgement runs **before writing**, and one hit means nothing is written.  It used to
    write the file first and check afterwards, and finding a leak only printed and exited 0 ——
    that is, the only control was "a log line a person might read", and by then it was already
    on disk and had flowed downstream (chunks, vectors, KG).
    (adversarial review 2026-08-18, BLOCKER)

    It was pulled out of `main` —— that is what makes it testable.
    **It never returns the values.**  Names and counts only.
    """
    return {n: len(p.findall(blob)) for n, p in SECRETS if p.findall(blob)}


# **Synthetic** secrets for the self-check.  No real credential goes here —— this file is committed.
# `remask_docs` uses these too —— two tools tested against one corpus verify each other.
SYNTH = [
("ANTHROPIC_KEY", "sk-ant-api03-" + "A" * 30),
("AWS_KEY",       "AKIA" + "B" * 16),
("SLACK_TOKEN",   "xoxb-" + "1" * 12 + "-abcdefghij"),
("GITHUB_TOKEN",  "ghp_" + "c" * 36),
("SLACK_WEBHOOK", "https://hooks.slack.com/services/T00/B00/xxxxxxxx"),
("JWT",           "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk"),
]


def mask(text):
    """Substitute the secrets and count how many were substituted."""
    n = collections.Counter()
    for name, pat in SECRETS:
        text, k = pat.subn(lambda m, nm=name: (m.group(1) if m.lastindex else "") + f"[REDACTED:{nm}]", text)
        if k:
            n[name] += k
    return text, n


def blocks_of(msg):
    c = (msg or {}).get("content")
    if isinstance(c, str):
        yield c
    elif isinstance(c, list):
        for b in c:
            if isinstance(b, dict):
                if b.get("type") == "text" and b.get("text"):
                    yield b["text"]
                elif b.get("type") == "tool_use":
                    # A tool call keeps only its intent (the full input is noise)
                    yield f"[tool:{b.get('name','?')}]"


def is_noise(t):
    s = t.strip()
    if len(s) < 40:
        return True
    if s.startswith(NOISE_PREFIX):
        return True
    lines = s.split("\n")
    hit = sum(1 for ln in lines[:20] if any(r.match(ln) for r in NOISE_RE))
    return hit >= max(3, len(lines[:20]) * 0.5)


def parse_session(path):
    """One session → {text, meta}.  None on failure."""
    parts, seen = [], set()
    n_msg = 0
    first_ts = last_ts = None
    for ln in open(path, encoding="utf-8", errors="ignore"):
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get("type") not in ("user", "assistant"):
            continue
        ts = r.get("timestamp")
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        for t in blocks_of(r.get("message")):
            if is_noise(t):
                continue
            h = hashlib.md5(t[:400].encode()).hexdigest()
            if h in seen:                       # drop repeats of identical content
                continue
            seen.add(h)
            role = "Me" if r["type"] == "user" else "Claude"
            parts.append(f"**{role}**: {t.strip()[:6000]}")
            n_msg += 1
    if not parts:
        return None
    return {"text": "\n\n".join(parts), "n_msg": n_msg,
            "first_ts": first_ts, "last_ts": last_ts}


def _selftest():
    """Does the masking and its verification **actually run.**

    This is the last line of defence before a secret touches disk.  Pass here quietly and it
    flows straight downstream (chunks, vectors, KG, MCP responses).

    ⚠ The values below are **all synthetic**.  Real credentials do not go even into a
      self-check —— the self-check file is itself committed to the repository.
    """

    # ① does each pattern catch its own
    for name, sample in SYNTH:
        found = find_leaks(f"before {sample} after")
        assert found, f"no pattern catches the {name} shape"

    # ② does the masking really remove them —— afterwards the verification must be clean
    blob = "\n".join(f"line {i}: {sample}" for i, (_, sample) in enumerate(SYNTH))
    masked, counts = mask(blob)
    assert sum(counts.values()) >= len(SYNTH), \
        f"the masking removed only {sum(counts.values())} (at least {len(SYNTH)} expected)"
    assert not find_leaks(masked), \
        f"survived the masking: {list(find_leaks(masked))}"
    for _, sample in SYNTH:
        assert sample not in masked, "the original string survives verbatim"
    assert "[REDACTED:" in masked, "no substitution marker —— there is no way to see what was removed"

    # ③ **can the verification fail** —— an unmasked original must always be caught
    assert find_leaks(blob), "an unmasked original comes back clean —— the verification is dead"

    # ④ does it avoid false positives on ordinary prose (crying wolf and nobody believes it)
    plain = ("In today's meeting we talked about something starting with sk-.  The acronym AKIA "
             "came up too, and https://hooks.slack.com was mentioned.  No talk of tokens.")
    assert not find_leaks(plain), f"ordinary prose is read as a secret: {list(find_leaks(plain))}"

    print(f"  ✅ ingest_sessions self-check —— {len(SYNTH)} synthetic kinds detected, masked, re-verified · 0 false positives")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest(); sys.exit(0)
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-chars", type=int, default=1500, help="sessions shorter than this are dropped")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    files = sorted(glob.glob(f"{SESS}/*/*.jsonl"))
    if a.limit:
        files = files[:a.limit]

    kept, dropped, total_masked = [], 0, collections.Counter()
    raw_chars = out_chars = 0
    for f in files:
        proj = os.path.basename(os.path.dirname(f))
        sid = os.path.basename(f)[:-6]
        d = parse_session(f)
        if not d:
            dropped += 1
            continue
        raw_chars += len(d["text"])
        text, nm = mask(d["text"])
        total_masked += nm
        if len(text) < a.min_chars:
            dropped += 1
            continue
        out_chars += len(text)
        kept.append({
            "session_id": sid, "project": proj,
            "path": f"sessions/{proj}/{sid}.md",
            "abs_path": f, "n_msg": d["n_msg"],
            "first_ts": d["first_ts"], "last_ts": d["last_ts"],
            "masked": sum(nm.values()),
            "text": text,
        })

    # ⚠ The verification runs **before writing**.
    #   It used to write the file first and check afterwards, and finding a leak only printed
    #   and exited 0.  That is, the only control was "a log line a person might read", and by
    #   then it was already on disk and had flowed downstream (chunks, vectors, KG).
    #   (adversarial review 2026-08-18, BLOCKER)
    blob = "".join(x["text"] for x in kept)
    leak = find_leaks(blob)
    if leak:
        print(f"\n❌ masking verification failed — {sum(leak.values())} secret(s) remaining: {leak}")
        print("   Nothing is written and it stops here.  Strengthen the SECRETS patterns and run again.")
        print("   (Pattern names only.  The values are never printed.)")
        sys.exit(1)

    json.dump(kept, open(f"{OUT}/session_docs.json", "w"), ensure_ascii=False)
    os.chmod(f"{OUT}/session_docs.json", 0o600)     # even in a 700 directory the file was created 644
    print(f"{len(files)} session(s) → {len(kept)} kept · {dropped} dropped")
    print(f"body {raw_chars/1e6:.1f}M chars → {out_chars/1e6:.1f}M after removing noise and duplicates "
          f"({out_chars/max(1,raw_chars)*100:.0f}%)")
    print(f"\nsecrets masked, {sum(total_masked.values())}:")
    for k, v in total_masked.most_common():
        print(f"   {k:16} {v:>4}")
    if not total_masked:
        print("   (none)")
    print("\nmasking verification: ✅ 0 secrets remaining (confirmed before writing)")
    print(f"→ {OUT}/session_docs.json ({os.path.getsize(f'{OUT}/session_docs.json')/1e6:.0f} MB)")
