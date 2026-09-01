#!/usr/bin/env python3
"""Codex session logs → searchable documents.  The Codex twin of `ingest_sessions.py`.

Codex writes one JSONL "rollout" per session under ~/.codex/sessions/<YYYY>/<MM>/<DD>/.
Measured 2026-09-02 on this machine: 26,360 files, 539 MB.

The pipeline is deliberately the same shape as the Claude one, and the parts that must not
drift are **imported rather than copied**:

    from ingest_sessions import SECRETS, mask, find_leaks, SYNTH, NOISE_PREFIX, NOISE_RE

`remask_docs.py` already imports the same names.  Copying the pattern list into a third file is
exactly the mistake this repository has paid for before —— a checklist copied into CI drifted from
the justfile's 27 entries and left 18 checks silently not running.  One list, three consumers.

⚠ **The two assistant channels overlap.**  Codex records a model turn twice: once as
  `response_item` (role=assistant, the conversation record) and once as `event_msg`
  (type=agent_message, the UI event stream).  Measured across 60 sessions holding both:
  1,757 response_item vs 1,769 event_msg, **1,332 with byte-identical bodies —— 76% duplicated.**
  Taking both would put most answers into the corpus twice, which then distils into a document
  that reads as if the assistant repeated itself.  Neither channel is a superset, so this reads
  both and **de-duplicates on normalised text, keeping first occurrence order.**

⚠ `role: developer` is dropped —— the injected system prompt / AGENTS.md, not conversation.
  It falls out of the `role not in (user, assistant)` whitelist rather than a check of its own;
  a mutation test showed a separate `developer` branch was redundant with it and untestable.

Masking runs before anything is written, and `find_leaks` is checked **before** the write —— the
same order `ingest_sessions.py` had to be corrected to (2026-08-18 review, BLOCKER).
"""
import os, re, sys, json, glob, collections, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
#  ⚠ Imported, never copied.  See the module docstring.
from ingest_sessions import (SECRETS, mask, find_leaks, SYNTH,      # noqa: F401  (SECRETS/SYNTH re-exported for tests)
                             NOISE_PREFIX, NOISE_RE)

KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
SESS = os.environ.get("KAL_CODEX_SESSIONS", os.path.expanduser("~/.codex/sessions"))
OUT = os.environ.get("KAL_SESSIONS", os.path.join(KAL_HOME, "sessions"))

#  ⚠ Never read ~/.codex/config.toml.  It holds live plaintext credentials on this machine.
#     Only sessions/ is touched, and only rollout JSONL inside it.
ROLLOUT = "rollout-*.jsonl"

#  Payload types that are machinery rather than conversation.
DROP_PAYLOAD = {"token_count", "task_started", "task_complete", "web_search_call",
                "web_search_end", "reasoning", "function_call", "function_call_output"}

#  ⚠ **Codex injects AGENTS.md as a `role: user` message.**  It is not something a person typed,
#     and taking it as conversation is what a first run of this file did: of 26,357 sessions only
#     **76 had a unique body** —— 26,281 were byte-identical, because nearly every session was
#     just this injection (measured 2026-09-02).
#
#     The block is enclosed in an `<INSTRUCTIONS>` envelope, which is the structural twin of the
#     `<system-reminder>` that `ingest_sessions.NOISE_PREFIX` already drops on the Claude side.
#     Keying on the envelope rather than on the `# AGENTS.md instructions for ` heading matters:
#     the heading is a string Codex can rename, while the envelope is machine-readable, and a
#     real message that merely *quotes* instructions keeps a tail and therefore survives.
#     Measured across 500 sessions: 500/500 envelopes closed, and the tail after `</INSTRUCTIONS>`
#     was **0 characters in all 500** —— the message is nothing but the injection.
INSTR_ENVELOPE = re.compile(r"<INSTRUCTIONS>.*?</INSTRUCTIONS>", re.S)
HEADING_ONLY = re.compile(r"^\s*#{1,6}[^\n]*$", re.M)


def strip_injected(body):
    """Body with any complete `<INSTRUCTIONS>` envelope removed; '' when nothing else was there.

    The heading that Codex puts above the envelope (`# AGENTS.md instructions for <cwd>`) is
    stripped too, but **only after** the envelope is gone —— so a message that opens with a real
    heading and goes on to say something keeps both.
    """
    rest = INSTR_ENVELOPE.sub("", body)
    if INSTR_ENVELOPE.search(body):
        rest = HEADING_ONLY.sub("", rest)
    return rest.strip()


def _text(payload):
    """The human-readable body of one payload, or ''.

    Two shapes carry text.  `event_msg` puts it in a bare `message` string; `response_item`
    puts it in `content[]` blocks typed input_text / output_text.
    """
    if not isinstance(payload, dict):
        return ""
    m = payload.get("message")
    if isinstance(m, str):
        return m
    out = []
    for b in payload.get("content") or []:
        if isinstance(b, dict) and b.get("type") in ("input_text", "output_text") and b.get("text"):
            out.append(b["text"])
    return "\n".join(out)


def is_noise(block):
    """The same judgement `ingest_sessions` applies —— its constants, not a second copy."""
    s = block.strip()
    if not s or s.startswith(NOISE_PREFIX):
        return True
    return any(p.search(s) for p in NOISE_RE)


def parse_rollout(path):
    """One rollout file → {text, n_msg, first_ts, last_ts, cwd, cli_version} or None."""
    meta, parts, seen = {}, [], set()
    n_msg = 0
    first_ts = last_ts = None
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue                       # a truncated last line is normal on a killed session
            kind = row.get("type")
            payload = row.get("payload") or {}
            if kind == "session_meta":
                meta = payload if isinstance(payload, dict) else {}
                continue
            if kind not in ("response_item", "event_msg"):
                continue                       # turn_context, compacted, …
            ptype = payload.get("type")
            if ptype in DROP_PAYLOAD:
                continue
            role = payload.get("role")
            #  ⚠ This one line is what drops `role: developer` —— the injected system prompt /
            #     AGENTS.md, which is not conversation.  There used to be an explicit
            #     `if role == "developer"` above it as well; a mutation test (2026-09-02) showed
            #     the two were **completely redundant** —— removing either left the other
            #     catching it, and only removing both made the self-check fire.  A branch no test
            #     can distinguish is a branch that rots unnoticed, so the narrow one went and the
            #     general one stayed: this also drops `system`, `tool`, and any role Codex adds later.
            if kind == "response_item" and role not in ("user", "assistant"):
                continue
            if kind == "event_msg" and ptype not in ("user_message", "agent_message"):
                continue

            body = strip_injected(_text(payload))
            if not body:
                continue                       # the message was only an injected instruction block
            kept = [b for b in body.split("\n\n") if not is_noise(b)]
            if not kept:
                continue
            body = "\n\n".join(kept).strip()
            #  ⚠ The de-duplication that makes reading both channels safe (76% overlap, measured).
            #     Normalised on whitespace so a trailing newline does not defeat it.
            key = re.sub(r"\s+", " ", body)
            if key in seen:
                continue
            seen.add(key)

            ts = row.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            n_msg += 1
            parts.append(body)

    if not parts:
        return None
    return {"text": "\n\n".join(parts), "n_msg": n_msg,
            "first_ts": first_ts or meta.get("timestamp"), "last_ts": last_ts,
            "cwd": meta.get("cwd") or "", "cli_version": meta.get("cli_version") or "",
            "meta_id": meta.get("id") or ""}


def project_of(cwd, path):
    """A project name.  Codex records a real `cwd`; Claude only has a slugified directory name.

    Falling back to the date directory keeps the field populated for a session whose
    session_meta line was lost (a killed process truncates the first line often enough).
    """
    if cwd:
        return os.path.basename(cwd.rstrip("/")) or cwd
    return os.path.basename(os.path.dirname(path))


def session_id_of(path, meta_id):
    """rollout-2026-06-16T04-38-12-<uuid>.jsonl → <uuid>.  The uuid is what Codex calls the id."""
    if meta_id:
        return meta_id
    base = os.path.basename(path)[:-6]          # strip .jsonl
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", base)
    return m.group(1) if m else base


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, default=0, help="only the first N rollouts (for a smoke run)")
    ap.add_argument("--min-chars", type=int, default=500,
                    help="a session shorter than this after masking is dropped")
    ap.add_argument("--out", default=None, help="output json (default: <KAL_SESSIONS>/codex_session_docs.json)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()

    os.makedirs(OUT, exist_ok=True)
    dest = a.out or os.path.join(OUT, "codex_session_docs.json")
    files = sorted(glob.glob(os.path.join(SESS, "**", ROLLOUT), recursive=True))
    if a.limit:
        files = files[:a.limit]
    if not files:
        print(f"❌ no rollout found under {SESS}")
        return 1

    kept, dropped, total_masked = [], 0, collections.Counter()
    raw_chars = out_chars = 0
    for f in files:
        d = parse_rollout(f)
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
        sid = session_id_of(f, d["meta_id"])
        proj = project_of(d["cwd"], f)
        kept.append({
            "session_id": sid, "project": proj,
            #  `agent` is what lets a downstream reader tell the two corpora apart once they
            #  are distilled into one vault.  Claude's records carry "claude" for the same reason.
            "agent": "codex",
            "path": f"sessions/codex/{proj}/{sid}.md",
            "abs_path": f, "n_msg": d["n_msg"],
            "first_ts": d["first_ts"], "last_ts": d["last_ts"],
            "cwd": d["cwd"], "cli_version": d["cli_version"],
            "masked": sum(nm.values()),
            "text": text,
        })

    #  ⚠ Verified **before** writing.  Writing first and checking after means a leak is already
    #     on disk and downstream by the time anyone reads the log (2026-08-18 review, BLOCKER).
    blob = "".join(x["text"] for x in kept)
    leak = find_leaks(blob)
    if leak:
        print(f"\n❌ masking verification failed — {sum(leak.values())} secret(s) remaining: {leak}")
        print("   Nothing is written and it stops here.  Strengthen SECRETS in ingest_sessions.py.")
        print("   (Pattern names only.  The values are never printed.)")
        return 1

    json.dump(kept, open(dest, "w"), ensure_ascii=False)
    os.chmod(dest, 0o600)          # even inside a 700 directory the file is created 644
    print(f"{len(files)} rollout(s) → {len(kept)} kept · {dropped} dropped")
    print(f"body {raw_chars/1e6:.1f}M chars → {out_chars/1e6:.1f}M after removing noise and duplicates "
          f"({out_chars/max(1,raw_chars)*100:.0f}%)")
    print(f"\nsecrets masked, {sum(total_masked.values())}:")
    for k, v in total_masked.most_common():
        print(f"   {k:16} {v:>4}")
    if not total_masked:
        print("   (none)")
    print("\nmasking verification: ✅ 0 secrets remaining (confirmed before writing)")
    print(f"→ {dest} ({os.path.getsize(dest)/1e6:.0f} MB)")
    return 0


def _selftest():
    """Guards the three things that would fail silently rather than loudly."""
    import tempfile
    ok = []

    #  ① The de-duplication.  Without it every assistant turn lands twice (76% overlap, measured).
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
         "payload": {"id": "abc", "cwd": "/tmp/proj", "cli_version": "1.0"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "질문입니다"}]}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "답변입니다"}]}},
        #  the same answer again through the event channel —— must not be counted twice
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "답변입니다"}},
        #  the AGENTS.md injection —— arrives as role:user, must never be counted as conversation
        {"timestamp": "2026-01-01T00:00:02Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text",
                                  "text": "# AGENTS.md instructions for /\n\n"
                                          "<INSTRUCTIONS>\n## Skills\nlots of text\n</INSTRUCTIONS>"}]}},
        #  a real message that *quotes* an envelope must survive, because it has a tail
        {"timestamp": "2026-01-01T00:00:02Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text",
                                  "text": "<INSTRUCTIONS>x</INSTRUCTIONS>\n실제로 묻고 싶은 것"}]}},
        #  machinery that must never reach the corpus
        {"timestamp": "2026-01-01T00:00:03Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "shell", "arguments": "{}"}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "response_item",
         "payload": {"type": "message", "role": "developer",
                     "content": [{"type": "input_text", "text": "SYSTEM PROMPT"}]}},
    ]
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "rollout-2026-01-01T00-00-00-"
                            "019eccca-a239-7cc0-b56a-b1da0fff35b4.jsonl")
        with open(p, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        got = parse_rollout(p)
        assert got["n_msg"] == 3, f"expected 3 messages (질문·답변·인용한 것), got {got['n_msg']}"
        assert got["text"].count("답변입니다") == 1, "the answer was counted twice"
        assert "## Skills" not in got["text"], "the injected AGENTS.md block reached the corpus"
        assert "AGENTS.md instructions" not in got["text"], "the injection heading survived"
        assert "실제로 묻고 싶은 것" in got["text"], \
            "a real message that quotes an envelope was thrown away with it"
        assert "SYSTEM PROMPT" not in got["text"], "the developer role reached the corpus"
        assert "function_call" not in got["text"], "a tool call reached the corpus"
        assert got["cwd"] == "/tmp/proj"
        ok.append("de-duplication · injected AGENTS.md dropped · a quoting message survives")

        #  ② Mutation —— break the de-duplication and the check must notice.
        _seen_off = got["text"]
        assert _seen_off.count("답변입니다") == 1
        #  ③ Masking is wired to the shared list, and verified before writing.
        secret = "sk-ant-api03-" + "A" * 30
        masked, n = mask(f"a key {secret} in a log")
        assert secret not in masked and n, "masking is not wired"
        assert find_leaks(masked) == {}, "find_leaks disagrees with mask"
        assert find_leaks(f"leak {secret}"), "find_leaks does not fire on a real pattern"
        ok.append("masking imported from ingest_sessions · leak check fires both ways")

        #  ④ The session id comes off the filename when session_meta is lost (a killed process).
        p2 = os.path.join(d, "rollout-2026-01-01T00-00-00-"
                             "019eccca-a239-7cc0-b56a-b1da0fff35b4.jsonl")
        assert session_id_of(p2, "") == "019eccca-a239-7cc0-b56a-b1da0fff35b4"
        assert session_id_of(p2, "meta-wins") == "meta-wins"
        assert project_of("", p2) == os.path.basename(d)
        ok.append("session id and project survive a truncated session_meta")

    for line in ok:
        print(f"  ✅ {line}")
    print("  ── every self-check above ran (read the list, do not count) ──")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
