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
import os, re, sys, json, glob, errno, hashlib, datetime, collections, argparse


# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
SESS = os.path.expanduser("~/.claude/projects")
OUT = os.environ.get("KAL_SESSIONS", os.path.join(KAL_HOME, "sessions"))

#  ── Sessions kept out on purpose (2026-09-25) ───────────────────────────────────────────────
#  An operator sometimes has to keep one particular conversation —— or the end of one —— out of
#  the knowledge base without dropping the whole project it ran in (`distill_sessions.
#  EXCLUDE_PROJECTS` works per project, far too coarse for that).  The list lives in
#  KAL_SESSIONS, outside the public repository, because the reason a session is listed can itself
#  be private.  One list, every consumer: the Codex and Hermes ingesters import `load_excluded`.
#
#      <session-id>                        the whole session stays out
#      <session-id> <ISO-8601 instant>     the session stays, minus every message at or after it
#
#  The cutoff exists because a whole session is too blunt a unit: a long-running session can hold
#  a month of useful work and, only at its end, material that must not be ingested.
#  ⚠ A line that cannot be read **stops the run.**  Skipping it would silently keep exactly the
#     session someone asked to remove.  A cutoff without an offset is refused for the same reason
#     —— read as local time or as UTC, it moves by hours.
EXCLUDE = os.path.join(OUT, "exclude.txt")

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
    # The product's own token (`kal_` + 32 base64url characters).  Review 2026-09-26: a bare token
    # and the hosted MCP address that carries one (`…/u/kal_…/mcp`) passed every net above ——
    # GENERIC_SECRET needs a `NAME=` label, and a URL has none.  Connecting an agent is exactly
    # when one gets pasted into a session.  The 32-character floor keeps the 12-character prefix
    # the app displays (`kal_` + 8) from reading as a leak.
    #   ⚠ **The anchor is not `\b`.**  `\b` missed the URL-encoded address (`%2Fkal_…`), a
    #      JSON-escaped one (`\nkal_…`) and one glued to Korean text (`토큰은kal_…`) —— and no
    #      anchor at all matched ~100 generated protobuf names (`file_kal_cloud_v1_…`).  So: not
    #      after an ASCII word character, or right after any percent-encoded byte (`%20`, `%3D`,
    #      `%2F`, twice-encoded `%252F`) or an escape (`\n`, `\x2F`, `\u002F`) —— review rounds
    #      5–6.  Counted against the repository, the session corpora, the distilled pages and the
    #      vault: 0 false hits.
    ("KAL_TOKEN",       re.compile(r"(?:(?<![A-Za-z0-9_])|(?<=%[0-9A-Fa-f]{2})|(?<=%25[0-9A-Fa-f]{2})"
                                   r"|(?<=\\[nrt])|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\u[0-9A-Fa-f]{4}))"
                                   r"kal_[A-Za-z0-9_\-]{32,}")),
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
("KAL_TOKEN",     "kal_" + "h" * 16 + "H1" * 8),
("KAL_TOKEN",     "https://mcp.example/u/kal_" + "i" * 16 + "I2" * 8 + "/mcp"),
("KAL_TOKEN",     "https%3A%2F%2Fmcp.example%2Fu%2Fkal_" + "j" * 16 + "J3" * 8 + "%2Fmcp"),
("KAL_TOKEN",     '{"url": "…\\nkal_' + "k" * 16 + "K4" * 8 + '"}'),
("KAL_TOKEN",     "토큰은kal_" + "m" * 16 + "M5" * 8),
("KAL_TOKEN",     "Authorization: Bearer%20kal_" + "n" * 16 + "N6" * 8),
("KAL_TOKEN",     "https%253A%252F%252Fmcp.example%252Fu%252fkal_" + "p" * 16 + "P7" * 8),
("KAL_TOKEN",     '"\\u002Fu\\u002Fkal_' + "q" * 16 + "Q8" * 8 + '"'),
("KAL_TOKEN",     "\\tkal_" + "r" * 16 + "R9" * 8),
("KAL_TOKEN",     "\\rkal_" + "s" * 16 + "S0" * 8),
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


TURN_MAX = 6000        # the most one turn contributes; the Hermes twin imports it so the corpora read alike


def is_noise(t):
    s = t.strip()
    if len(s) < 40:
        return True
    if s.startswith(NOISE_PREFIX):
        return True
    lines = s.split("\n")
    hit = sum(1 for ln in lines[:20] if any(r.match(ln) for r in NOISE_RE))
    return hit >= max(3, len(lines[:20]) * 0.5)


def epoch(ts):
    """A timestamp as UTC epoch seconds, or None when it cannot be read.

    ISO-8601 **with an offset** (the Claude and Codex logs write `…Z`) or a number (Hermes stores
    epoch seconds).  A naive ISO string reads as None: which clock it meant would be a guess.
    """
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        t = datetime.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return t.timestamp() if t.tzinfo else None


def before_cutoff(ts, cutoff):
    """May a message stamped `ts` be kept under `cutoff` (epoch seconds; None = no cutoff)?

    ⚠ Fail closed: under a cutoff, a message whose time cannot be read is dropped —— nothing
       shows it came before the point someone asked to cut at.
    """
    if cutoff is None:
        return True
    e = epoch(ts)
    return e is not None and e < cutoff


def load_excluded(path=None, known=None, strict=True):
    """`exclude.txt` → {session id: cutoff}.  The cutoff is epoch seconds, or None when the whole
    session stays out.  `#` starts a comment, blank lines are ignored, a missing file excludes
    nothing, and an id listed twice keeps the stricter entry.  See the note above `EXCLUDE`.

    Ids are lower-cased and a byte-order mark is read past: a UUID pasted in capitals, or an editor
    that writes a BOM, used to leave a line that matched nothing —— silently.  Collectors look ids
    up lower-cased too.

    Every listed id is then checked (`_check_listed`) against what distillation already made and
    against `known` —— {id: the first session of its conversation}; from `known_sessions()` when
    `strict` and not given (the self-checks pass a fixture).  The collectors are strict.
    Distillation is not (`strict=False`): it runs where not every collector's sessions may be
    visible (a container), so it passes only what its corpora say about conversations.  There the
    abbreviated-id check still runs against them, and "is this id real at all" is left to the
    collectors.
    A Hermes session stitched into a longer conversation is excluded with all of it —— the
    conversation is one document; a cutoff on any of its sessions cuts it at that instant.
    """
    path = path or EXCLUDE
    try:
        fh = open(path, encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    out = {}
    with fh:
        for n, line in enumerate(fh, 1):
            f = line.split("#", 1)[0].split()
            if not f:
                continue
            cut = epoch(f[1]) if len(f) == 2 else None
            if len(f) > 2 or (len(f) == 2 and cut is None):
                raise SystemExit(f"❌ {path}:{n}: cannot read {line.strip()!r} —— write `<session-id>` "
                                 "or `<session-id> <ISO-8601 with an offset>`, e.g. "
                                 "2026-09-25T09:00:00Z.  Nothing is written.")
            sid = f[0].lower()
            if sid in out:
                cut = None if out[sid] is None or cut is None else min(out[sid], cut)
            out[sid] = cut
    if out:
        unread = []                        # folders known_sessions could not read (see _check_listed)
        if strict:
            known = known_sessions(unread) if known is None else known
        _check_listed(out, path, known or {}, strict, unread)
    return out


def transcripts(onerror):
    """Every Claude transcript (`<SESS>/<project>/<id>.jsonl`), sorted.  What cannot be read —— the
    store or a folder above it, a project folder, a link whose target is gone, a transcript —— goes
    to `onerror(OSError)` instead of reading as absent: glob skipped all of it without a word, and a
    mode-000 ~/.claude collected "0 sessions" and overwrote the corpus with [] (review rounds 5–6).
    A missing store is simply empty —— most machines have no Claude Code.
    """
    try:
        with os.scandir(SESS) as it:
            projects = sorted((e for e in it if not e.name.startswith(".")), key=lambda e: e.name)
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as e:
        onerror(e)
        return []
    out = []
    for p in projects:
        try:
            if p.is_symlink() and not os.path.exists(p.path):
                onerror(FileNotFoundError(errno.ENOENT, "a link whose target is gone or cannot be read", p.path))
                continue
            if not p.is_dir():
                continue
            with os.scandir(p.path) as it:
                names = sorted(e.name for e in it if e.name.endswith(".jsonl") and not e.name.startswith("."))
        except OSError as e:
            onerror(OSError(e.errno, e.strerror, e.filename or p.path))
            continue
        for n in names:
            f = os.path.join(p.path, n)
            if not os.access(f, os.R_OK):
                onerror(PermissionError(errno.EACCES, "Permission denied", f))
            out.append(f)
    return out


def known_sessions(unreadable=None):
    """{session id: the first session of its conversation}, lower-cased, for every session the
    three collectors can see, whatever their own filters decide later.  A Claude or Codex session is
    its own conversation; a Hermes session may be stitched into a longer one (see
    ingest_hermes_sessions.LINEAGE).  Read-only —— file names for Claude and Codex, one read of the
    session table per Hermes store —— and the roots are the collectors' own constants, imported.

    ⚠ A Hermes store that cannot be read as one is reported and skipped, not fatal: it used to raise
       here and take the Claude and Codex collection down with it.  Only a line naming none of the
       sessions that *could* be read then fails (2026-09-26).
    """
    import sqlite3
    import ingest_codex_sessions as codex, ingest_hermes_sessions as hermes   # both import this module
    out = {}

    def warn(folder, why):          # also kept for the stop: see `_check_listed`
        if unreadable is not None:
            unreadable.append(folder)
        print(f"⚠ {folder}: cannot be read ({why}) —— exclude.txt lines naming its sessions cannot be matched")
    for f in transcripts(lambda e: warn(e.filename, e.strerror)):
        sid = os.path.basename(f)[:-6].lower()
        out[sid] = sid
    for f in codex.rollouts(codex.SESS, lambda e: warn(e.filename, e.strerror)):
        sid = codex.session_id_of(f, "").lower()
        out[sid] = sid
    for db in hermes.stores(hermes.HOME, unreadable=warn):
        try:
            out.update(hermes.chain_roots(db))
        except (hermes.SchemaError, sqlite3.Error) as e:
            why = str(e).splitlines()[0][:160] if str(e) else type(e).__name__
            print(f"⚠ {db}: not readable as a Hermes store ({why}) —— exclude.txt lines naming its "
                  "sessions cannot be matched")
    return out


def _check_listed(listed, path, known, strict=True, unreadable=()):
    """Stop before anything is collected when the list cannot do what it says (2026-09-26).

      · an id already distilled.  Excluding is not retroactive: listed whole, its pages would keep
        reaching the bundle, the index and a push while the log says "left out".  Listed with a
        cutoff, pages whose text reaches **past** the cutoff may hold exactly what it removes;
        the rest are fine, and the later text is distilled only on `--restart`, cut.  How far a
        page's text reaches is what its marker recorded (`distill_sessions.mark_done`) —— the
        file times said "after the cutoff" for pages made from the cut text itself, so a cutoff
        stopped every run after the first distillation, and following the stop looped (impl round
        5).  Files from before 2026-09-26 recorded nothing and are judged by their mtime.
        A session's pages and markers carry the id of its conversation's **first** session
        (`known`), so a later session of a stitched Hermes conversation is looked up as that too.
      · an id that only **starts** a session —— abbreviated, the first 8 characters the way a UUID
        is usually quoted —— reads as applied and excludes nothing.  Checked in both modes, against
        whatever `known` holds: distillation's own corpora are enough to see it, and a line added
        after the last collection reached distillation first (impl round 4).
      · `strict` only: an id no collector can see: a typo, or a session deleted since —— the line
        matches nothing, and if the session is gone it should go too.
    """
    import distill_sessions as ds                  # the distilled layout is defined there, once
    done = {n.lower(): os.path.join(ds.DONE, n)
            for n in (os.listdir(ds.DONE) if os.path.isdir(ds.DONE) else ())}
    names = {sid: {sid, known.get(sid, sid)} for sid in listed}
    markers = set(done.values())
    made = collections.defaultdict(list)
    for sid, ids in names.items():                 # markers: `<agent>-<id>`, or the bare legacy id
        made[sid] += [done[x] for i in sorted(ids) for x in [i] + [f"{a}-{i}" for a, _ in ds.CORPORA]
                      if x in done]
    agent_of = {}
    for p in sorted(glob.glob(os.path.join(ds.OUT, "*.md"))):
        with open(p, encoding="utf-8", errors="replace") as fh:
            head = fh.read(1500)
        m = re.search(r"^session_id:\s*\"?([^\"\s]+)", head, re.M)
        a = re.search(r"^session_agent:\s*\"?([a-z0-9_-]+)", head, re.M)
        agent_of[p] = a.group(1) if a else "claude"
        for sid, ids in names.items():
            if m and m.group(1).lower() in ids:
                made[sid].append(p)
    problems = []
    for sid, cut in listed.items():
        paths = made.get(sid)
        via = f" (as part of {known[sid]}, the conversation it was stitched into)" \
            if known.get(sid, sid) != sid else ""
        #  How far the text behind each file reached: what **this session's** markers recorded ——
        #  another session's stale marker listing a reused page name vouched for it before (review
        #  round 6) —— else the file's mtime.
        reach = {}
        for mk in (p for p in paths or () if p in markers):
            info = ds.marker_info(mk) or {}
            t = epoch(info.get("last_ts"))
            if t is not None:
                reach[mk] = t
                reach.update((os.path.join(ds.OUT, s + ".md"), t) for s in info.get("pages") or [])
        at = max((reach.get(p, os.path.getmtime(p)) for p in paths), default=None) if paths else None
        if paths and (cut is None or at >= cut):
            pages = [p for p in paths if p.endswith(".md")]
            keys = sorted({k for p in pages for k in (
                f"personal_sessions_{agent_of.get(p, 'claude')}_{os.path.basename(p)[:-3]}",
                f"raw_conversations_sessions_{os.path.basename(p)[:-3]}")})
            problems.append(
                (f"{sid} is listed whole, but was distilled already{via}" if cut is None else
                 f"{sid} was distilled past its cutoff{via}, so its pages may hold what the cutoff "
                 "removes") + " —— remove these, and any copy already promoted or emitted:"
                + "".join(f"\n        {p}" for p in paths)
                + ("\n      …and what was already extracted from those pages: their lines in "
                   "~/.kal/lr_cache.jsonl (`doc` " + " · ".join(keys) + "), or `just reset extract` "
                   "—— a page later written under the same name otherwise brings the removed text "
                   "back as history" if keys else ""))
            continue
        if paths:
            print(f"ⓘ {path}: {sid}{via} is distilled up to "
                  f"{datetime.datetime.fromtimestamp(at, datetime.timezone.utc).isoformat(timespec='seconds')}, "
                  "before its cutoff —— a conversation is distilled once, so anything it said after "
                  "that is not (see Cache ① in docs/PIPELINE.md)")
        if sid not in known:
            hit = next((s for s in known if s.startswith(sid)), None)
            if hit or strict:
                problems.append(f"{sid!r} is only the start of session {hit} —— an abbreviated id "
                                "excludes nothing; write it in full" if hit else
                                f"{sid!r} names no session any collector can see (Claude, Codex or "
                                "Hermes) —— " + (f"it may be in {', '.join(unreadable)}, which could not "
                                "be read: fix that rather than deleting the line" if unreadable else
                                "delete the line if the session is gone"))
    if problems:
        raise SystemExit(f"❌ {path}:\n   " + "\n   ".join(problems) + "\n   Nothing is written.")


def parse_session(path, cutoff=None):
    """One session → {text, meta}.  None on failure.  `cutoff`: see `load_excluded`."""
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
        #  Before anything is taken from the record —— a subagent report arrives inside a user
        #  record, so it is cut with the record that carries it.
        if not before_cutoff(r.get("timestamp"), cutoff):
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
            parts.append(f"**{role}**: {t.strip()[:TURN_MAX]}")
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
    assert not find_leaks("the token starting kal_Ab3dEf9h was rotated"), \
        "the 12-character prefix the app displays reads as a token —— KAL_TOKEN's floor dropped"
    assert not find_leaks("var file_kal_cloud_v1_cloud_proto_rawDescData00 = []"), \
        "a generated identifier reads as a kal token —— KAL_TOKEN lost its anchor"
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

    #  ── known_sessions: a folder it cannot read is named, and a line it then cannot match is not
    #     told "delete the line" —— the session may be in that folder (review round 6) ────────────
    import tempfile as _tf, contextlib as _cl, io as _io
    from unittest import mock as _mock
    import ingest_codex_sessions as _cx, ingest_hermes_sessions as _hm, distill_sessions as _ds0
    with _tf.TemporaryDirectory() as _d0, \
            _mock.patch.object(sys.modules[__name__], "SESS", os.path.join(_d0, "claude")), \
            _mock.patch.object(_cx, "SESS", os.path.join(_d0, "codex")), \
            _mock.patch.object(_hm, "HOME", os.path.join(_d0, "hermes")), \
            _mock.patch.object(_ds0, "OUT", os.path.join(_d0, "distilled")), \
            _mock.patch.object(_ds0, "DONE", os.path.join(_d0, "distilled", ".done")):
        os.makedirs(os.path.join(_d0, "claude", "p"))
        open(os.path.join(_d0, "claude", "p", "seen-one.jsonl"), "w").close()
        _lk = os.path.join(_d0, "claude", "locked")
        os.makedirs(_lk)
        _l0 = os.path.join(_d0, "exclude.txt")
        with open(_l0, "w") as _fh:
            _fh.write("seen-one\nnot-seen\n")
        if os.geteuid():
            os.chmod(_lk, 0)
        try:
            _b = _io.StringIO()
            with _cl.redirect_stdout(_b):
                _k = known_sessions()
            try:
                with _cl.redirect_stdout(_io.StringIO()):
                    load_excluded(_l0)
                _why = ""
            except SystemExit as _e:
                _why = str(_e)
        finally:
            os.chmod(_lk, 0o700)
        assert "seen-one" in _k, _k
        if os.geteuid():
            assert "locked: cannot be read" in _b.getvalue(), f"an unreadable Claude folder went unreported: {_b.getvalue()}"
            assert "could not be read" in _why and "delete the line" not in _why, \
                f"a line that may name a session in an unreadable folder was told to go: {_why}"
    print("  ✅ known_sessions names what it cannot read, and the stop does not say \"delete the line\" then")

    #  ── exclude.txt: whole sessions and cutoffs (2026-09-25) ─────────────────────────────────
    import subprocess, tempfile, contextlib, io
    from unittest import mock
    import distill_sessions as _ds
    #  Distillation's output is pointed into the temp dir for the whole block —— nothing here reads
    #  the real ~/.kal/distilled.
    with tempfile.TemporaryDirectory() as _d, \
            mock.patch.object(_ds, "OUT", os.path.join(_d, "distilled")), \
            mock.patch.object(_ds, "DONE", os.path.join(_d, "distilled", ".done")):
        os.makedirs(_ds.DONE)
        _lst = os.path.join(_d, "exclude.txt")
        assert load_excluded(_lst) == {}, "a missing list must exclude nothing"
        _K = {k: k for k in ("aaa", "bbb", "ccc", "ddd", "eee", "ok-id",
                             "f0f0f0f0-1111-2222-3333-444444444444")}
        #  A byte-order mark in front of the first id, and a UUID in capitals: both used to match
        #  nothing, silently.
        with open(_lst, "w", encoding="utf-8-sig") as _fh:
            _fh.write("AAA\n# the reasons can be private\n\nbbb 2026-09-25T09:00:00Z  # the end only\n"
                      "ccc 2026-09-25T20:00:00+09:00\nccc\nddd 2026-09-25T18:00:00+09:00\n"
                      "eee 2026-09-25T10:00:00Z\neee 2026-09-25T09:00:00Z\n"
                      "  F0F0F0F0-1111-2222-3333-444444444444  \n")
        _want = {"aaa": None, "bbb": 1790326800.0, "ccc": None, "ddd": 1790326800.0,
                 "eee": 1790326800.0, "f0f0f0f0-1111-2222-3333-444444444444": None}
        assert load_excluded(_lst, known=_K) == _want, \
            f"BOM · case · comments · offsets · 'the stricter entry wins' misread: {load_excluded(_lst, known=_K)}"

        def _stops(listing, needle, known=_K):
            with open(_lst, "w") as _fh:
                _fh.write(listing)
            try:
                load_excluded(_lst, known=known)
            except SystemExit as _e:
                assert needle in str(_e), f"the stop does not say {needle!r}: {_e}"
                return str(_e)
            raise AssertionError(f"exclude.txt was accepted, but should have stopped ({needle!r}):\n{listing}")

        #  ⚠ A line it cannot read **stops the run** —— skipped, it would keep exactly the session
        #     someone asked to remove.  A naive time is refused: local or UTC is a guess.
        for _bad in ("aaa 2026-09-25", "aaa 2026-09-25T09:00:00", "aaa yesterday",
                     "aaa 2026-09-25T09:00:00Z trailing"):
            _stops(f"ok-id\n{_bad}\n", "exclude.txt:2")
        #  …and so does an id that cannot exclude anything: abbreviated, or known to no collector.
        _stops("ok-id\nf0f0f0f0\n", "'f0f0f0f0' is only the start of session f0f0f0f0-1111")
        _stops("ok-id\nnever-seen-anywhere\n", "delete the line if the session is gone")
        #  Distillation is not strict, but an abbreviated id still stops it: a line added after the
        #  last collection reaches distillation first, and would let the session through silently.
        with open(_lst, "w") as _fh:
            _fh.write("ok-id\nf0f0f0f0\n")
        try:
            load_excluded(_lst, known=_K, strict=False)
            raise AssertionError("distillation accepted an abbreviated id —— the session is distilled")
        except SystemExit as _e:
            assert "only the start of session" in str(_e), _e
        with open(_lst, "w") as _fh:
            _fh.write("ok-id\nnever-seen-anywhere\n")
        assert load_excluded(_lst, known=_K, strict=False) == {"ok-id": None, "never-seen-anywhere": None}, \
            "distillation stopped on an id it cannot see —— that check belongs to the collectors"

        #  ⚠ Excluding is not retroactive.  A session listed after it was distilled keeps its pages
        #     —— they reach the bundle, the index and a push while the log says "left out".
        def _mark(name, when):
            _p = os.path.join(_ds.DONE, name)
            open(_p, "w").close()
            os.utime(_p, (when, when))
            return _p

        _m = _mark("hermes-aaa", 1790326000.0)
        _stops("aaa\n", _m)                                 # listed whole, marker present
        os.remove(_m)
        _page = os.path.join(_ds.OUT, "a-distilled-page.md")
        with open(_page, "w") as _fh:
            _fh.write("---\ntitle: \"t\"\nsession_id: BBB\nsession_agent: claude\n---\nbody\n")
        _stops("bbb\n", _page)                              # listed whole, a page but no marker
        #  …and the stop names what was extracted from that page too, or the removed text comes back
        #  as history under a page later written with the same name (review round 6)
        _stops("bbb\n", "personal_sessions_claude_a-distilled-page")
        os.remove(_page)
        _m = _mark("ccc", 1790326000.0)                     # the bare legacy Claude marker
        _stops("ccc\n", _m)
        os.remove(_m)
        #  A later session of a stitched conversation has no pages of its own —— they, and the
        #  marker, carry the conversation's first id.
        _m = _mark("hermes-root1", 1790326000.0)
        _stops("m2\n", _m, known={"m2": "root1", "root1": "root1"})
        os.remove(_m)
        #  With a cutoff: a marker written at or after the cutoff means the pages may hold the text
        #  it removes; one written before means they cannot —— a note, not a stop.
        _m = _mark("claude-ddd", 1790326800.0)
        _stops("ddd 2026-09-25T09:00:00Z\n", "distilled past its cutoff")
        os.utime(_m, (1790326799.0, 1790326799.0))
        with open(_lst, "w") as _fh:
            _fh.write("ddd 2026-09-25T09:00:00Z\n")
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf):
            assert load_excluded(_lst, known=_K) == {"ddd": 1790326800.0}
        assert "is distilled up to" in _buf.getvalue() and "--restart" not in _buf.getvalue(), \
            f"a session distilled before its cutoff got no note, or one sending it to --restart: {_buf.getvalue()}"
        os.remove(_m)
        #  ⚠ **Distilled from the cut text itself is not "after its cutoff".**  The marker records how
        #     far the text reached, and it and every page it lists are judged by that —— by their
        #     mtimes (both newer than any cutoff), a cutoff stopped every run after the first
        #     distillation, and following the stop's advice looped (impl round 5).  A page the
        #     marker does not list still goes by its mtime, and stops.
        with open(os.path.join(_ds.DONE, "claude-ddd"), "w") as _fh:
            json.dump({"last_ts": "2026-09-25T08:59:00Z", "pages": ["ddd-cut"]}, _fh)
        for _slug in ("ddd-cut", "ddd-older"):
            with open(os.path.join(_ds.OUT, _slug + ".md"), "w") as _fh:
                _fh.write("---\ntitle: \"t\"\nsession_id: ddd\n---\nbody\n")
        _stops("ddd 2026-09-25T09:00:00Z\n", "ddd-older.md")
        os.remove(os.path.join(_ds.OUT, "ddd-older.md"))
        with open(_lst, "w") as _fh:
            _fh.write("ddd 2026-09-25T09:00:00Z\n")
        with contextlib.redirect_stdout(io.StringIO()):
            assert load_excluded(_lst, known=_K) == {"ddd": 1790326800.0}, \
                "a session distilled from its cut text reads as distilled after its cutoff —— every run stops"
        os.remove(os.path.join(_ds.DONE, "claude-ddd"))
        os.remove(os.path.join(_ds.OUT, "ddd-cut.md"))
        #  Only a session's own markers vouch for its pages: another session's stale marker listing a
        #  reused name approved a page holding text past the cutoff (review round 6).
        with open(os.path.join(_ds.DONE, "claude-aaa"), "w") as _fh:
            json.dump({"last_ts": "2026-09-25T08:00:00Z", "pages": ["shared-name"]}, _fh)
        with open(os.path.join(_ds.OUT, "shared-name.md"), "w") as _fh:
            _fh.write("---\ntitle: \"t\"\nsession_id: eee\n---\nbody\n")
        _stops("eee 2026-09-25T09:00:00Z\n", "shared-name.md")
        os.remove(os.path.join(_ds.DONE, "claude-aaa"))
        os.remove(os.path.join(_ds.OUT, "shared-name.md"))

        #  ⚠ An unreadable message time under a cutoff is dropped —— nothing shows it came first.
        assert before_cutoff(None, None) and before_cutoff("unreadable", None), \
            "without a cutoff nothing may be dropped"
        for _ts in (None, "", "unreadable", "2026-09-25T08:00:00"):
            assert not before_cutoff(_ts, 1790326800.0), \
                f"{_ts!r} passed a cutoff —— an unreadable time must fail closed"

        #  A cutoff keeps what came before it —— and a subagent report is cut with the record that
        #  carries it, whenever the call that asked for it was made.
        _proj = os.path.join(_d, "home", ".claude", "projects", "p")
        os.makedirs(_proj)

        def _write(sid, rows):
            with open(os.path.join(_proj, sid + ".jsonl"), "w") as _fh:
                _fh.write("".join(json.dumps(r) + "\n" for r in rows))
            return os.path.join(_proj, sid + ".jsonl")

        _p = _write("cut-me", [
            {"type": "user",
             "message": {"content": "CANARY-NO-TIMESTAMP a record whose time cannot be read at all"}},
            {"type": "user", "timestamp": "2026-09-25T07:00:00.000Z",
             "message": {"content": "A question asked well before the cutoff, long enough to keep"}},
            {"type": "assistant", "timestamp": "2026-09-25T07:00:01.000Z",
             "message": {"content": [{"type": "tool_use", "id": "toolu_early", "name": "Task", "input": {}},
                                     {"type": "tool_use", "id": "toolu_late", "name": "Task", "input": {}}]}},
            {"type": "user", "timestamp": "2026-09-25T08:59:59.999Z",
             "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_early",
                                      "content": "EARLY report from a subagent, delivered before the cutoff"}]}},
            {"type": "user", "timestamp": "2026-09-25T09:00:00.000Z",
             "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_late",
                                      "content": "CANARY-LATE-REPORT delivered exactly at the cutoff instant"}]}},
            {"type": "assistant", "timestamp": "2026-09-25T10:00:00.000Z",
             "message": {"content": "CANARY-AFTER-CUTOFF an answer written after the cutoff instant"}},
        ])
        _full = parse_session(_p)["text"]
        assert all(c in _full for c in ("CANARY-LATE-REPORT", "CANARY-AFTER-CUTOFF",
                                        "CANARY-NO-TIMESTAMP")), "the cutoff canaries are not live"
        _cut = parse_session(_p, 1790326800.0)                  # 2026-09-25T09:00:00Z
        assert "well before the cutoff" in _cut["text"] and "EARLY report" in _cut["text"], \
            "the cutoff took what came before it"
        assert "CANARY-LATE-REPORT" not in _cut["text"], \
            "a subagent report at the cutoff survived —— a report is cut with its record"
        assert "CANARY-AFTER-CUTOFF" not in _cut["text"], "a message after the cutoff survived"
        assert "CANARY-NO-TIMESTAMP" not in _cut["text"], \
            "a record with no readable time survived a cutoff —— it must fail closed"
        assert _cut["last_ts"] == "2026-09-25T08:59:59.999Z", _cut["last_ts"]

        #  …and the command honours both kinds of line.  Run for real against a temp HOME, so it is
        #  the call site that is tested —— a parser-only check stays green with the call deleted.
        #  The other collectors' roots are temp dirs too, holding one session each, so the "known
        #  to some collector" check has something of theirs to find.
        _write("gone", [{"type": "user", "timestamp": "2026-09-25T07:00:00Z",
                         "message": {"content": "CANARY-EXCLUDED-WHOLE a session listed whole in the list"}}])
        _write("kept", [{"type": "user", "timestamp": "2026-09-25T07:00:00Z",
                         "message": {"content": "An ordinary session that nobody asked to exclude at all"}}])
        #  a file name in capitals, listed in lower case —— the lookup must fold case too
        _write("Upper-Case", [{"type": "user", "timestamp": "2026-09-25T07:00:00Z",
                               "message": {"content": "CANARY-UPPER-STEM a session whose file name has capitals"}}])
        _codex = os.path.join(_d, "codex", "2026", "09", "25")
        os.makedirs(_codex)
        open(os.path.join(_codex, "rollout-2026-09-25T07-00-00-"
                                  "019eccca-a239-7cc0-b56a-b1da0fff35b4.jsonl"), "w").close()
        import sqlite3 as _sq, ingest_hermes_sessions as _H
        os.makedirs(os.path.join(_d, "hermes", "profiles", "broken"))
        _con = _sq.connect(os.path.join(_d, "hermes", "state.db"))
        for _t, _cols in _H.REQUIRED.items():
            _con.execute(f"CREATE TABLE {_t} ({', '.join(_cols)})")
        _con.execute("INSERT INTO sessions (id, parent_session_id) VALUES ('herm-1', NULL)")
        #  a stitched conversation: `herm-cont` continues `herm-root` after compaction
        _con.execute("INSERT INTO sessions (id, end_reason) VALUES ('herm-root', 'compression')")
        _con.execute("INSERT INTO sessions (id, parent_session_id) VALUES ('herm-cont', 'herm-root')")
        _con.commit()
        _con.close()
        #  …and a store that is not a Hermes store at all: reported, and it must not take the
        #  Claude collection down with it.
        _sq.connect(os.path.join(_d, "hermes", "profiles", "broken", "state.db")).close()
        _out = os.path.join(_d, "out")
        os.makedirs(_out)
        _json = os.path.join(_out, "session_docs.json")

        def _run(listing):
            with open(os.path.join(_out, "exclude.txt"), "w") as _fh:
                _fh.write(listing)
            return subprocess.run([sys.executable, os.path.abspath(__file__), "--min-chars", "1"],
                                  env=dict(os.environ, HOME=os.path.join(_d, "home"),
                                           KAL_HOME=os.path.join(_d, "kal"), KAL_SESSIONS=_out,
                                           KAL_CODEX_SESSIONS=os.path.join(_d, "codex"),
                                           KAL_HERMES_HOME=os.path.join(_d, "hermes"),
                                           KAL_DISTILLED=_ds.OUT),
                                  capture_output=True, text=True, timeout=120)

        #  The last two ids are a Codex and a Hermes session's —— known elsewhere, left alone here.
        _r = _run("gone\ncut-me 2026-09-25T09:00:00Z\nupper-case\n"
                  "019eccca-a239-7cc0-b56a-b1da0fff35b4\nHERM-1\n")
        assert _r.returncode == 0, _r.stdout + _r.stderr
        with open(_json) as _fh:
            _docs = {x["session_id"]: x["text"] for x in json.load(_fh)}
        assert set(_docs) == {"cut-me", "kept"}, f"exclude.txt was not honoured: {sorted(_docs)}"
        assert "EARLY report" in _docs["cut-me"] and "CANARY-AFTER-CUTOFF" not in _docs["cut-me"], \
            "the cutoff did not reach the command"
        assert "3 of 5 listed id(s) name a Claude session —— 2 left out whole · 1 cut at a timestamp" \
            in _r.stdout, _r.stdout
        assert "broken/state.db: not readable as a Hermes store" in _r.stdout, \
            f"an unreadable Hermes store was not reported:\n{_r.stdout}"
        #  A profile folder that cannot be read is reported and skipped the same way —— the
        #  readable stores still answer (HERM-1), and the Claude collection goes on (impl round 4).
        _locked = os.path.join(_d, "hermes", "profiles", "locked")
        os.makedirs(_locked)
        os.chmod(_locked, 0)
        try:
            _r = _run("gone\nHERM-1\n")
        finally:
            os.chmod(_locked, 0o700)
        assert _r.returncode == 0 and ("profiles/locked: cannot be read" in _r.stdout or not os.geteuid()), \
            f"an unreadable Hermes folder stopped the Claude collection, or went unreported:\n{_r.stdout}{_r.stderr}"
        #  …while a Claude project folder that cannot be read stops the Claude collection itself:
        #  glob skipped it without a word (review round 5).  (Root reads it anyway.)
        _lockp = os.path.join(_d, "home", ".claude", "projects", "locked-project")
        os.makedirs(_lockp)
        os.chmod(_lockp, 0)
        try:
            _r = _run("gone\n")
        finally:
            os.chmod(_lockp, 0o700)
            os.rmdir(_lockp)
        if os.geteuid():
            assert _r.returncode != 0 and "locked-project: cannot be read" in _r.stdout + _r.stderr, \
                f"an unreadable Claude project folder was skipped quietly:\n{_r.stdout}{_r.stderr}"
        #  …and a Codex folder that cannot be read is reported by the id lookup, which goes on ——
        #  glob skipped it, and the line naming a session in it was told "delete the line if the
        #  session is gone" (review round 5).
        _lockc = os.path.join(_d, "codex", "locked-day")
        os.makedirs(_lockc)
        os.chmod(_lockc, 0)
        try:
            _r = _run("gone\n")
        finally:
            os.chmod(_lockc, 0o700)
            os.rmdir(_lockc)
        if os.geteuid():
            assert _r.returncode == 0 and "locked-day: cannot be read" in _r.stdout, \
                f"an unreadable Codex folder went unreported, or stopped the Claude collection:\n{_r.stdout}{_r.stderr}"
        #  A project link whose target is gone, and a Claude store that cannot be read at all, stop the
        #  Claude collection too —— glob made the second "0 sessions" and overwrote the corpus with []
        #  (review round 6).
        with open(_json) as _fh:
            _before = _fh.read()
        _dangle = os.path.join(_d, "home", ".claude", "projects", "gone-project")
        os.symlink(os.path.join(_d, "nowhere"), _dangle)
        try:
            _r = _run("gone\n")
        finally:
            os.remove(_dangle)
        assert _r.returncode != 0 and "gone-project: cannot be read" in _r.stdout + _r.stderr, \
            f"a dangling project link was skipped quietly:\n{_r.stdout}{_r.stderr}"
        if os.geteuid():
            _claude = os.path.join(_d, "home", ".claude")
            os.chmod(_claude, 0)
            try:
                _r = _run("gone\n")
            finally:
                os.chmod(_claude, 0o700)
            with open(_json) as _fh:
                assert _r.returncode != 0 and "cannot be read" in _r.stdout + _r.stderr and _fh.read() == _before, \
                    f"an unreadable Claude store collected nothing and went on:\n{_r.stdout}{_r.stderr}"
        #  A later session of a stitched Hermes conversation, listed whole after the conversation was
        #  distilled: found through its first session's marker, from the Claude collector too.
        _mk = os.path.join(_ds.DONE, "hermes-herm-root")
        open(_mk, "w").close()
        _r = _run("herm-cont\n")
        assert _r.returncode != 0 and _mk in _r.stderr and "as part of herm-root" in _r.stderr, \
            f"a distilled conversation was not found from its later session:\n{_r.stdout}{_r.stderr}"
        os.remove(_mk)
        #  ⚠ An abbreviated id, and an id no collector knows, stop the run before anything is written.
        for _listing, _needle in (("gone\nkep\n", "'kep' is only the start of session kept"),
                                  ("gone\nnever-seen-anywhere\n", "delete the line if the session is gone")):
            if os.path.exists(_json):
                os.remove(_json)
            _r = _run(_listing)
            assert _r.returncode != 0 and _needle in _r.stderr, \
                f"exclude.txt was accepted ({_needle!r}):\n{_r.stdout}{_r.stderr}"
            assert not os.path.exists(_json), "a run stopped by exclude.txt still wrote its output"
    print("  ✅ exclude.txt —— a listed id is gone, a cutoff keeps what came before it (subagent "
          "reports included; an unreadable time fails closed), a malformed line · an abbreviated "
          "id · an id no collector knows · an already-distilled session stop the run, a BOM and "
          "capitals are read, other collectors' ids are left alone and the matches are counted")


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
    blocked = []
    files = transcripts(blocked.append)     # an unreadable store, folder or transcript stops the run
    if blocked:
        raise SystemExit(f"❌ {blocked[0].filename}: cannot be read ({blocked[0].strerror}) —— the sessions "
                         "in it would be left out without a word.  Fix its permissions.  Nothing is written.")
    if a.limit:
        files = files[:a.limit]

    excluded = load_excluded()
    matched = len(excluded.keys() & {os.path.basename(f)[:-6].lower() for f in files})
    kept, dropped, machine, n_excl, n_cut, total_masked = [], 0, 0, 0, 0, collections.Counter()
    raw_chars = out_chars = 0
    for f in files:
        proj = os.path.basename(os.path.dirname(f))
        sid = os.path.basename(f)[:-6]
        key = sid.lower()                      # exclude.txt ids are lower-cased
        if key in excluded and excluded[key] is None:
            n_excl += 1
            continue                           # listed whole in exclude.txt —— never read
        n_cut += key in excluded
        d = parse_session(f, excluded.get(key))
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
    print(f"exclude list {EXCLUDE}: {matched} of {len(excluded)} listed id(s) name a Claude session "
          f"—— {n_excl} left out whole · {n_cut} cut at a timestamp")
    print(f"body {raw_chars/1e6:.1f}M chars → {out_chars/1e6:.1f}M after removing noise and duplicates "
          f"({out_chars/max(1,raw_chars)*100:.0f}%)")
    print(f"\nsecrets masked, {sum(total_masked.values())}:")
    for k, v in total_masked.most_common():
        print(f"   {k:16} {v:>4}")
    if not total_masked:
        print("   (none)")
    print("\nmasking verification: ✅ 0 secrets remaining (confirmed before writing)")
    print(f"→ {OUT}/session_docs.json ({os.path.getsize(f'{OUT}/session_docs.json')/1e6:.0f} MB)")
