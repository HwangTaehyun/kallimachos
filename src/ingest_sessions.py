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
    #  ⚠ **One pattern, running to the END marker *or to the end of the text*.**  There used to be
    #     two —— a bounded one requiring `-----END` within 4,000 characters, and a `_TRUNC` fallback
    #     matching `BEGIN` plus 4,000 characters.  A key longer than that hit only the fallback,
    #     which masked exactly 4,000 characters and **left the rest verbatim**: measured 2026-09-04,
    #     a 5,342-character block left 1,281 characters of key material, and `find_leaks()` on the
    #     result returned `{}` —— the publication check certified the leak as clean, and the
    #     self-check asserting `not find_leaks(masked)` passed on it.
    #     Running to `\Z` can redact more than the key when the END marker is missing.  That is the
    #     right direction: a document carrying an unterminated private-key header is not one to
    #     send, and the substitution count says how much went.
    ("PRIVATE_KEY",   re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)")),
    # A block cut off without END.  Session logs frequently truncate tool output mid-stream,
    # leaving the header and part of the body.  The pattern above requires END and missed it (found by measurement).
    #   ⚠ **The signature is optional, because session logs truncate.**  Requiring all three
    #      segments at 10+ characters meant a JWT whose signature was cut short passed untouched
    #      —— and that is the shape these logs routinely produce, by the same mid-stream truncation
    #      the PRIVATE_KEY comment above records.  The signature is also the part that matters
    #      least here: the **payload** is base64 of the claims (subject, email), so a JWT with no
    #      valid signature at all is still the personal data.  Measured before widening: 0 new
    #      hits across the 1,134-file bundle, `src/` and `docs/` (counts only, values not printed).
    ("JWT",           re.compile(
        r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}(?:\.[A-Za-z0-9_\-]*)?")),

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
    # Mail providers.  Measured 2026-09-06: a Resend key pasted **in prose** ("키는 re_… 입니다")
    # passed every net above —— GENERIC_SECRET only fires on `NAME=value`, and nothing knew this
    # prefix.  Setting up the login mail is exactly when such a key is pasted into a session.
    ("RESEND_KEY",      re.compile(r"\bre_[A-Za-z0-9]{8,}_[A-Za-z0-9]{20,}\b")),
    ("SENDGRID_KEY",    re.compile(r"\bSG\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\b")),
    ("POSTMARK_TOKEN",  re.compile(r"(?i)\b(x-postmark-server-token\s*:\s*)[A-Za-z0-9\-]{20,}")),
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
    #   ⚠ **The label list was narrower than the labels people write.**  Eight synthetic shapes
    #      passed both `mask()` and `find_leaks()` untouched (adversarial review 2026-09-04):
    #      bare `token:`, bare `key:`, `pw:`, `DB_PASS=`, `비밀번호:`, `Authorization: Basic …`,
    #      `AccountKey=`, and a 32-hex key.  That matters more here than in an ordinary masker,
    #      because at `openwiki_emit.py:377` `find_leaks` is the **only** control on the publish
    #      path —— nothing masks there, it just refuses to write.
    #      Added: the bare forms, `pw`/`pass`, `DB_*`-style suffixes, the Korean label (this
    #      corpus is Korean), `Basic` auth and Azure's `AccountKey`.
    #      Bare `key:` is in too, but only after **measuring** it: `key: value` is ubiquitous in
    #      YAML and this corpus is *about* a configurable pipeline, so it was the one likely to
    #      cry wolf.  Counted against the live bundle before adding —— **0 pages, 0 hits**, and 0
    #      in `src/` and `docs/` as well (values never printed, counts only).  If that changes,
    #      the 16-character-plus-digit-or-capital floor is the knob, not deletion.
    #   ⚠ **Not added: the bare 32-hex shape.**  Any git object id, checksum or content hash
    #      matches it, and a false positive here **blocks a publication** rather than merely
    #      redacting a line.  Catching Datadog keys is not worth refusing to publish every page
    #      that quotes a commit.  Recorded so the next person does not read the omission as an
    #      oversight.
    ("GENERIC_SECRET",  re.compile(
        r"(?i)((?<![A-Za-z0-9])(?:api[_\-]?key|secret[_\-]?key|access[_\-]?token|"
        r"auth[_\-]?token|refresh[_\-]?token|client[_\-]?secret|credentials?|password|passwd|"
        r"(?:[a-z0-9]+[_\-])?(?:token|secret|passwd|password|pass|pwd|pw|key)|"
        #      `datadog:` / `dd-api:` in their bare forms —— `datadog_api_key:` was already caught
        #      (it contains `api_key`, and the lookbehind lets `_api_key` through).  Measured by
        #      the review across 3,742 session files: 3 hits, zero md5/sha collateral.  Added as a
        #      **label**, not as a shape, which is what keeps the 3,068-match 32-hex rule out.
        #      ⚠ **A closing quote may sit between the key and the separator.**  Every one of these
    #         labels was written for `key = value`, and JSON —— which is what a session log is
    #         full of —— writes `"api_key": "…"`.  The quote is not in `\s`, so the pattern
    #         stopped at the key name and **matched nothing**: measured 2026-09-04, four shapes
    #         including `{"api_key": …}` and `{"password": …}` passed `mask()` untouched and
    #         `find_leaks()` called them clean.  `["']?` is the whole fix.
    r"datadog|dd[_\-]?api|accountkey|비밀번호|암호)[\"']?\s*[=:]\s*[\"']?)"
        r"(?=[A-Za-z0-9_\-/+=]{16,})(?=[^\s\"']*(?-i:[A-Z0-9]))[A-Za-z0-9_\-/+=]{16,}")),
    #   `Authorization: Basic <b64>` —— a distinct shape, and the credential is the whole value.
    #   A cookie header is a credential wholesale —— the session id in it *is* the login.  Neither
    #   the label list above (the key is the cookie's own name, unknowable in advance) nor
    #   `BASIC_AUTH` reaches it.  Measured 2026-09-04 across the vault (1,078 files), ~/.kal
    #   (1,635) and this repository: 0 hits, so it costs nothing to carry.
    ("COOKIE",          re.compile(r"(?i)((?:set-)?cookie\s*:\s*)[A-Za-z0-9_\-]+=[^\s;\"']{8,}")),
    #   `https://user:password@host` —— the same shape `DB_URI` already covers for `postgres://`
    #   and friends, which is the tell that leaving http(s) out was an oversight rather than a
    #   decision.  Anchored on `://` and a `@`, so an ordinary URL cannot match.
    ("URL_USERINFO",    re.compile(r"\bhttps?://[^\s:/@]+:[^\s@/]{6,}@")),
    ("BASIC_AUTH",      re.compile(
        r"(?i)(authorization\s*:\s*basic\s+)[A-Za-z0-9+/=]{16,}")),
]

#  ⚠ **This pipeline's own LLM calls are logged as sessions.**  Claude Code records every call
#     made through `claude_cli.run()` in ~/.claude/projects/, with the same `userType`,
#     `isSidechain` and file shape as a conversation a person had —— there is no structural way
#     to tell them apart.  Measured 2026-09-02 on this machine:
#
#         3,501 Claude sessions on disk
#         ~2,000 of them this pipeline talking to itself
#         **153 of the 298 session documents already in the openwiki bundle** had been distilled
#         out of those calls —— the knowledge base was over half full of summaries of our own
#         prompts, which nobody noticed because the distiller paraphrases rather than quotes.
#
#     Two layers, because neither alone is enough:
#
#       ① `PIPELINE_MARK` —— `claude_cli.run()` prefixes it to every prompt from now on.  This is
#          the load-bearing one: exact, structural, and it covers prompts that do not exist yet.
#       ② The openings below —— for sessions **already on disk**, written before ① existed.  This
#          list can only ever be complete for the past; a new tool calling Claude Code from
#          outside this repository would need its own entry, which is precisely why ① is the
#          layer that matters going forward.  `Condense the tool payload` is one such outsider.
#
#     A retired prompt keeps its entry.  Removing one silently readmits every historical session
#     that used it.
try:
    from claude_cli import PIPELINE_MARK
except Exception:                              # a caller that imports only the masking helpers
    PIPELINE_MARK = "<!-- kal-pipeline-call:"
MACHINE_OPENINGS = (
    "You extract a knowledge graph from text",          # lr_extract
    "You are a knowledge-graph curator",                # lr_extract, entity profiles
    "You distil Claude Code conversation logs",         # distill_sessions (English, from 2026-09-01)
    "당신은 Claude Code 대화 로그를",                      # distill_sessions (Korean, retired 2026-09-01)
    "Below are documents distilled separately",         # distill_sessions MERGE
    "아래는 하나의 긴 Claude Code 대화를 조각내어",           # distill_sessions MERGE (Korean, retired)
    "Condense the tool payload below",                  # not this repository —— another local tool
)


def is_pipeline_call(text):
    """Was this session **produced by a program**, not typed by a person?

    Checked against the head of the text only: a genuine conversation may quote one of these
    strings while discussing the pipeline (this very repository does it constantly), and such a
    quote appears in the middle of a discussion, never as the opening prompt.
    """
    head = text[:1200]
    body = head.split("**Me**:", 1)[-1].lstrip() if "**Me**:" in head else head.lstrip()
    #  ⚠ **The marker is required at the START of the first prompt, not anywhere in it.**
    #     `PIPELINE_MARK in head` was the first version, and it deletes a conversation that merely
    #     *mentions* the marker —— which is what a session about this filter looks like.  Measured
    #     2026-09-02: the log of the session that added the marker contains the string six times,
    #     because writing the code means writing the string.  Over-blocking here is worse than
    #     under-blocking: it silently destroys exactly the conversations that explain the system.
    return body.startswith(PIPELINE_MARK) or any(body.startswith(s) for s in MACHINE_OPENINGS)


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
("RESEND_KEY",    "re_" + "d" * 8 + "_" + "e" * 24),
("SENDGRID_KEY",  "SG." + "f" * 22 + "." + "g" * 43),
]


def mask(text):
    """Substitute the secrets and count how many were substituted."""
    n = collections.Counter()
    for name, pat in SECRETS:
        text, k = pat.subn(lambda m, nm=name: (m.group(1) if m.lastindex else "") + f"[REDACTED:{nm}]", text)
        if k:
            n[name] += k
    return text, n


#  ⚠ **A subagent's report reaches the parent as a `tool_result`, and this used to drop it.**
#     Claude Code writes a subagent's own transcript to
#     `<project>/<parent-uuid>/subagents/agent-*.jsonl`, which the ingest glob (`*/*.jsonl`)
#     never reaches —— and the report it hands back lives in the parent as the `tool_result` of
#     the `Task`/`Agent` call, which this function did not look at.  So the work vanished twice.
#
#     It cannot simply keep every `tool_result`: measured on one 45MB session, of 712 results
#     **Bash was 254k characters and Read 377k** —— exactly the "command output dumps" the
#     distillation prompt forbids.  `Agent` was 20 results and 27k characters.
#
#     The pairing is what makes it separable: a `tool_use` block carries `id`, and its
#     `tool_result` carries `tool_use_id`.  Measured: **100% of results pair**.  So the result of
#     an agent call can be kept while a file dump is dropped, with no guessing.
AGENT_TOOLS = ("Task", "Agent")


def _result_text(block):
    """The readable body of a `tool_result` block."""
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c
                         if isinstance(x, dict) and x.get("type") == "text")
    return ""


def blocks_of(msg, agent_ids=()):
    """Readable blocks of one message.  `agent_ids` are the tool_use ids of subagent calls."""
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
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in agent_ids:
                    #  A subagent's report —— the only tool_result kept.  It is a written answer
                    #  to a question the parent asked, not a dump of a file or a command.
                    txt = _result_text(b).strip()
                    if txt:
                        yield "[subagent report]\n" + txt


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
    #  ⚠ Collected as the file is read, **in order**.  A `tool_use` for a subagent always precedes
    #     its `tool_result`, so a single forward pass is enough —— by the time the result arrives,
    #     its id is already known.  Anything else's result stays dropped.
    agent_ids = set()
    for ln in open(path, encoding="utf-8", errors="ignore"):
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get("type") not in ("user", "assistant"):
            continue
        _c = (r.get("message") or {}).get("content")
        if isinstance(_c, list):
            for _b in _c:
                if (isinstance(_b, dict) and _b.get("type") == "tool_use"
                        and _b.get("name") in AGENT_TOOLS and _b.get("id")):
                    agent_ids.add(_b["id"])
        ts = r.get("timestamp")
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        for t in blocks_of(r.get("message"), agent_ids):
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

    #  ⚠ **A long key must not leave a tail behind.**  A bounded pattern masked the first 4,000
    #     characters and left the rest verbatim —— measured 2026-09-04, a 5,342-character block
    #     left 1,281 characters of key material and `find_leaks()` returned nothing, so the
    #     publication check certified the leak.  The assertion above passed on it: the
    #     substitution had removed the detector's own anchor.
    _synth = "Zm9vYmFyYmF6cXV4" * 330          # synthetic base64, not a key
    for _tail in ("\n-----END RSA PRIVATE KEY-----", ""):
        _pem = "-----BEGIN RSA PRIVATE KEY-----\n" + _synth + _tail
        _m, _n = mask(_pem)
        _left = "".join(re.findall(r"[A-Za-z0-9+/=]{40,}", _m))
        assert not _left, f"{len(_left)} characters of key material survived masking"
        assert _n, "a private key block was not counted as masked"
        assert not find_leaks(_m), "the scanner certified a masked key as clean"
    for _, sample in SYNTH:
        assert sample not in masked, "the original string survives verbatim"
    assert "[REDACTED:" in masked, "no substitution marker —— there is no way to see what was removed"

    # ③ **can the verification fail** —— an unmasked original must always be caught
    assert find_leaks(blob), "an unmasked original comes back clean —— the verification is dead"

    # ③b **The labels people actually write.**  The keyword list was narrower than the corpus:
    #     eight synthetic shapes passed both `mask()` and `find_leaks()` untouched (adversarial
    #     review 2026-09-04).  This matters more than an ordinary masker gap because at
    #     `openwiki_emit.py:377` `find_leaks` is the **only** control on the publish path.
    for _name, _raw in {
        "bare token": "token: sk-synthetic-AAAABBBBCCCCDDDDEEEEFFFF1111",
        "bare key":   "key: synthetic-AAAABBBBCCCCDDDDEEEEFFFF2222",
        "pw":         "pw: synthetic-AAAABBBBCCCCDDDD3333",
        "DB_PASS":    "DB_PASS=synthetic-AAAABBBBCCCCDDDD4444",
        "Korean":     "비밀번호: synthetic-AAAABBBBCCCCDDDD5555",
        "basic auth": "Authorization: Basic c3ludGhldGljOnBhc3N3b3JkMTIzNDU2Nzg5",
        "AccountKey": "AccountKey=c3ludGhldGljAAAABBBBCCCCDDDDEEEEFFFF6666==",
    }.items():
        _m, _ = mask(_raw)
        assert _m != _raw, f"a {_name} secret passed the masker untouched"
        assert not find_leaks(_m), f"a masked {_name} secret still reads as a leak"
    #     …and the shapes deliberately **not** covered stay uncovered, so the omission is a
    #     decision on record rather than something that quietly drifts in later.  A bare 32-hex
    #     value is any git object id or checksum, and a false positive here refuses a
    #     publication rather than redacting a line.
    assert not find_leaks("fixed in 0123456789abcdef0123456789abcdef"), \
        "a bare 32-hex value is treated as a secret —— every page quoting a commit now blocks"
    #     ⚠ The 16-character floor is the knob the comment on `GENERIC_SECRET` names, so it needs
    #        a case only **it** excludes.  These carry a digit or a capital, so the other
    #        look-ahead lets them through and the length is the whole defence; without this,
    #        lowering the floor to 1 left the self-check green while `key: v2` became a secret.
    #     A JWT whose signature was truncated mid-stream —— the shape these logs produce.  The
    #     payload still carries the claims, so "no valid signature" is not "not personal data".
    _h, _pl = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", "eyJzdWIiOiJzeW50aGV0aWMiLCJpYXQiOjE1MTZ9"
    for _sig in ("SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c", "SflKxwRJS", "SflKx", ""):
        _j = f"{_h}.{_pl}.{_sig}" if _sig else f"{_h}.{_pl}"
        _mj, _ = mask(_j)
        assert _mj != _j, f"a JWT with a {len(_sig)}-character signature passed untouched"
        assert not find_leaks(_mj), "a masked JWT still reads as a leak"

    #     ⚠ …and the `eyJ` anchor is what keeps that from eating the corpus.  These documents are
    #        full of `module.symbol` citations, and without the prefix any two ten-character
    #        segments joined by a dot become a "secret" —— which at `openwiki_emit.py:377` means
    #        refusing to publish the pages that cite the code they describe.
    for _dotted in ("openwiki_emit.render_index", "schema_v3.indexable_count",
                    "docker-compose.viewer.yml", "lr_summary_cache.jsonl"):
        assert not find_leaks(_dotted), \
            f"a dotted identifier read as a JWT ({_dotted!r}) —— the eyJ anchor is gone"

    #  ⚠ **Four shapes an adversarial review found passing through both `mask()` and
    #     `find_leaks()` on 2026-09-04**, plus the JSON spelling that made the first two
    #     invisible (a closing quote sits between the key and the `:`).  Each is asserted masked
    #     *and* clean afterwards —— asserting only the first passes on a pattern that redacts one
    #     character and leaves the rest, which is the shape the PRIVATE_KEY note above records.
    for _shape in (
            '{"refreshToken": "1//0eXaBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcdef"}',
            'credentials = "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"',
            "Cookie: sessionid=8f2a91c4de77b03e5a16cc9d4402ab71; csrftoken=xyz",
            "https://admin:hunter2SuperSecret@internal.example.com/api/v1/things",
            '{"api_key": "sk_live_ABCDEFGHIJ0123456789"}'):
        _m, _ = mask(_shape)
        assert _m != _shape, f"a credential shape passed untouched: {_shape[:28]}…"
        assert not find_leaks(_m), f"masking left a leak behind: {list(find_leaks(_m))}"
    #     …and the other direction, because those five widen the net: prose using the same words
    #     must stay clean, or the check cries wolf and the next person deletes it.
    for _ok in ("credentials: see 1Password", "the refresh token expires in an hour",
                "Cookie 를 굽는 법", "https://github.com/HwangTaehyun/kallimachos",
                "set-cookie 헤더가 무엇인지", "the api_key parameter is documented below"):
        assert not find_leaks(_ok), f"ordinary prose read as a secret: {_ok!r}"

    for _short in ("key: v2", "pass: OK", "token: A1", "password: x9"):
        assert not find_leaks(_short), \
            f"a two-character value read as a secret ({_short!r}) —— the length floor is gone"

    # ④ does it avoid false positives on ordinary prose (crying wolf and nobody believes it)
    plain = ("In today's meeting we talked about something starting with sk-.  The acronym AKIA "
             "came up too, and https://hooks.slack.com was mentioned.  No talk of tokens.")
    assert not find_leaks(plain), f"ordinary prose is read as a secret: {list(find_leaks(plain))}"

    #  ── this pipeline's own calls never become documents ─────────────────────────────────
    #  Measured 2026-09-02: 153 of the 298 session documents in the bundle had been distilled
    #  out of our own prompts.  Nobody noticed because the distiller paraphrases, so the prompt
    #  text does not survive into the document —— only its subject matter does.
    from claude_cli import PIPELINE_MARK as _PM
    assert is_pipeline_call(f"**Me**: {_PM}\nanything at all"), "the marker is not honoured"
    assert is_pipeline_call("**Me**: You extract a knowledge graph from text. Output ONE JSON"), \
        "a historical machine prompt was let through"
    assert is_pipeline_call("**Me**: 당신은 Claude Code 대화 로그를 개인 위키로 정제한다"), \
        "the retired Korean prompt was let through —— its entry must stay"
    #  ⚠ and the other direction: a real conversation **about** the pipeline must survive.
    #     This repository discusses its own prompts constantly; matching anywhere in the text
    #     instead of at the opening would delete exactly the sessions worth keeping.
    assert not is_pipeline_call(
        "**Me**: 오늘 왜 느려?\n\n**Claude**: lr_extract 의 "
        "'You extract a knowledge graph from text' 프롬프트가 매 청크마다 나갑니다"), \
        "a genuine conversation quoting a machine prompt was dropped"
    assert not is_pipeline_call("**Me**: 세션 로그를 어떻게 옮기지"), "a plain question was dropped"
    #  ⚠ and a conversation *about the marker* must survive.  The session that introduced it
    #     necessarily contains the string —— writing the code means writing it.
    assert not is_pipeline_call(
        f"**Me**: 필터를 어떻게 걸지\n\n**Claude**: `{_PM}` 를 프롬프트 앞에 붙입니다"), \
        "a conversation discussing the marker was dropped —— it must be at the START"

    #  ── a subagent's report survives; a file dump does not ───────────────────────────────
    #  Claude Code writes a subagent's transcript to a `subagents/` folder the ingest glob never
    #  reaches, and hands the report back to the parent as the `tool_result` of the Task/Agent
    #  call —— which `blocks_of` used to drop with every other tool result.  The work vanished twice.
    #  Keeping *all* results is not the fix: measured on one 45MB session, Bash results were
    #  254k characters and Read 377k, exactly the dumps the distillation prompt forbids.
    _msg = {"content": [
        {"type": "tool_use", "id": "toolu_agent", "name": "Task", "input": {}},
        {"type": "tool_use", "id": "toolu_bash",  "name": "Bash", "input": {}},
    ]}
    _res = {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_agent", "content": "조사 결과 보고서"},
        {"type": "tool_result", "tool_use_id": "toolu_bash",  "content": "total 48\ndrwxr-xr-x …"},
    ]}
    _ids = {b["id"] for b in _msg["content"] if b.get("name") in AGENT_TOOLS}
    assert _ids == {"toolu_agent"}, "the Task/Agent call was not recognised"
    _out = "\n".join(blocks_of(_res, _ids))
    assert "조사 결과 보고서" in _out, "the subagent report was dropped"
    assert "drwxr-xr-x" not in _out, "a command dump leaked in with the report"
    #  and with no agent ids at all, nothing tool_result-shaped survives
    assert "조사 결과 보고서" not in "\n".join(blocks_of(_res, set())), \
        "a tool_result was kept without being paired to an agent call"
    #  the list-of-blocks content shape must work too —— that is what the API actually sends
    _res2 = {"content": [{"type": "tool_result", "tool_use_id": "toolu_agent",
                          "content": [{"type": "text", "text": "블록 형태 보고서"}]}]}
    assert "블록 형태 보고서" in "\n".join(blocks_of(_res2, _ids)), \
        "a report delivered as a list of text blocks was dropped"
    print("  ✅ subagent report kept, command dumps still dropped (paired by tool_use_id)")


    print("  ✅ pipeline self-calls filtered —— marker · historical openings · a conversation "
          "quoting one survives")
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

    kept, dropped, machine, total_masked = [], 0, 0, collections.Counter()
    raw_chars = out_chars = 0
    for f in files:
        proj = os.path.basename(os.path.dirname(f))
        sid = os.path.basename(f)[:-6]
        d = parse_session(f)
        if not d:
            dropped += 1
            continue
        if is_pipeline_call(d["text"]):
            machine += 1
            continue                           # our own LLM call, not a conversation
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
    print(f"{len(files)} session(s) → {len(kept)} kept · {dropped} dropped · "
          f"{machine} were this pipeline's own LLM calls")
    print(f"body {raw_chars/1e6:.1f}M chars → {out_chars/1e6:.1f}M after removing noise and duplicates "
          f"({out_chars/max(1,raw_chars)*100:.0f}%)")
    print(f"\nsecrets masked, {sum(total_masked.values())}:")
    for k, v in total_masked.most_common():
        print(f"   {k:16} {v:>4}")
    if not total_masked:
        print("   (none)")
    print("\nmasking verification: ✅ 0 secrets remaining (confirmed before writing)")
    print(f"→ {OUT}/session_docs.json ({os.path.getsize(f'{OUT}/session_docs.json')/1e6:.0f} MB)")
