#!/usr/bin/env python3
"""Hermes Agent session store → searchable documents.  The Hermes twin of `ingest_codex_sessions.py`.

Hermes keeps every session in one SQLite file —— `~/.hermes/state.db`, plus one per profile under
`~/.hermes/profiles/<name>/state.db` —— not in per-session files.  Measured 2026-09-25 on this
machine: `schema_version` 26, tables `sessions`, `messages`, `system_prompts`; only the schema and
row counts were read.  26 is what v0.21.0 and earlier write —— `SCHEMA_VERSION = 26` at tag
v2026.8.31 ([hermes_state_common.py:355](https://github.com/NousResearch/hermes-agent/blob/29112bef099274229cadff79cdff7bf7b99c4b77/hermes_state_common.py#L355),
committed and published 2026-08-31, retrieved 2026-09-26) —— while v0.21.1 through v0.21.5, the
latest release, write 30 (tag v2026.9.24, [hermes_state_common.py:239](https://github.com/NousResearch/hermes-agent/blob/f97608f178d1ffeca59860195ab7da295f7c8e5f/hermes_state_common.py#L239),
committed and published 2026-09-24, retrieved 2026-09-26).  The reader keys on **columns**, not
on the version number, so a release that renames a column fails loudly instead of returning
nothing —— and the self-check runs it against a 26 and a 30 fixture.

What this reads —— allowlists, never denylists:

    files      state.db and profiles/*/state.db.  Nothing else in ~/.hermes is opened: `.env`,
               `auth.json`, `config.yaml` and its `*.bak*` copies hold credentials, and a denylist
               of file names is exactly what a backup copy with a new suffix slips through.
    columns    sessions(id, source, started_at, cwd, title, parent_session_id, end_reason,
               handoff_state, hidden) and messages(session_id, role, content, timestamp) are
               selected.  The other REQUIRED columns are only referenced inside SQL —— the message
               ones filter (see MESSAGE_FILTER), sessions(model_config, ended_at, session_key)
               classify child sessions the way Hermes does (see LINEAGE).  Never `system_prompts`,
               `sessions.system_prompt`, `messages.reasoning*` / `api_content` / `tool_calls`.
    messages   user · assistant turns a person or the assistant wrote —— see MESSAGE_FILTER, and
               SYNTHETIC for the user rows Hermes itself writes.
               Tool results are where fetched web pages and chat messages arrive, the same reason
               ingest_sessions keeps only subagent results.
    sessions   compression continuations are stitched into one conversation; `/branch` and reset
               children are conversations of their own (a branch minus the transcript it copied);
               subagent runs are dropped —— their `user` turns are the parent agent's instructions.
               A bot's Bot Chat, and other agents' messages anywhere, are opt-in (see BOT_CHAT);
               any other hidden session is dropped.
    platforms  cli · tui · desktop, as Hermes records them in `sessions.source`, and only sessions
               never handed off to a messaging platform.  Override: --sources, else the
               KAL_HERMES_SOURCES environment variable —— deliberately not a kal_config key, because
               the local web UI, which has no authentication, can rewrite kal_config.

⚠ **Every other platform is opt-in, and gated.**  Messaging platforms carry other people's words by
  definition, and `acp` does too: an `acp` session is either the owner working from an editor or a
  channel mention another person wrote, forwarded by a messaging desktop through `hermes acp` ——
  and no column tells the two apart.  Other people's words would reach the extraction model, which
  is exactly where a planted instruction tries to use a tool.  So choosing any platform beyond the
  default refuses to run until `claude_cli.no_tools_verified()` holds —— `just verify-extract-tools`
  measured, for this CLI version and these flags, that the model has zero tools —— and a run that
  collects one leaves `claude_cli.THIRD_PARTY_MARK`, so the model calls that come later check the
  receipt again.  Measured 2026-09-25: 33 of the 36 sessions on this machine are `acp`, so the
  default collects almost nothing —— and every run prints what it skipped, per platform, so that is
  never a silent zero.

`<KAL_SESSIONS>/exclude.txt` (see ingest_sessions.load_excluded) applies by session id: listing any
session of a stitched conversation —— normally its first, the id the output carries —— drops it,
and a cutoff drops its messages from that instant on.

The output has the shape the Claude and Codex twins write, with the speaker marked the way the
Claude transcript marks it (`**Me**:` / `**Hermes**:`).  Masking runs before anything is written
and `find_leaks` is checked **before** the write —— imported from ingest_sessions, never copied.
"""
import argparse, collections, datetime, glob, json, os, re, sqlite3, sys, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
#  ⚠ Imported, never copied —— one list, four consumers now.  See ingest_codex_sessions' docstring.
from ingest_sessions import (SECRETS, mask, find_leaks, SYNTH, TURN_MAX,   # noqa: F401  (re-exported for tests)
                             load_excluded, before_cutoff)
#  Codex's noise judgement, not Claude's: both drop the blocks ingest_sessions' NOISE_PREFIX and
#  NOISE_RE describe, but the Claude one also drops every block under 40 characters —— and a
#  short turn ("yes, ship it") is the confirmation the distiller's "later beats earlier" reads.
from ingest_codex_sessions import is_noise  # noqa: E402
#  The module, not the name: the gate looks `no_tools_verified` up when it runs, so the self-check
#  can stand in for it without a receipt tied to the real `claude --version`.
import claude_cli  # noqa: E402

KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
def _hermes_root():
    """The Hermes root the way Hermes finds it (`get_default_hermes_root`, hermes_constants.py:216-233
    at 59004a6): `$HERMES_HOME` when set —— a profile folder under ~/.hermes means ~/.hermes, and
    `<root>/profiles/<name>` elsewhere means `<root>` —— else ~/.hermes.  The connection snippets honour
    HERMES_HOME, so the collector must too (review round 6)."""
    native = os.path.realpath(os.path.expanduser("~/.hermes"))
    env = os.environ.get("HERMES_HOME", "").strip()
    if not env:
        return os.path.expanduser("~/.hermes")
    p = os.path.realpath(os.path.expanduser(env))
    if p == native or p.startswith(native + os.sep):
        return os.path.expanduser("~/.hermes")
    return os.path.dirname(os.path.dirname(p)) if os.path.basename(os.path.dirname(p)) == "profiles" else p


HOME = os.environ.get("KAL_HERMES_HOME") or _hermes_root()      # KAL_HERMES_HOME overrides (self-checks)
OUT = os.environ.get("KAL_SESSIONS", os.path.join(KAL_HOME, "sessions"))
DOCS = "hermes_session_docs.json"         # distill_sessions.CORPORA reads it; its self-check compares

SESSION_COLS = ("id", "source", "started_at", "cwd", "title",
                "parent_session_id", "end_reason", "handoff_state", "hidden")
REQUIRED = {
    #  Everything a query names.  Only SESSION_COLS and the four message columns reach Python.
    "sessions": SESSION_COLS + ("model_config", "ended_at", "session_key"),
    "messages": ("session_id", "role", "content", "timestamp", "active", "compacted",
                 "_compressed_summary", "display_kind", "observed"),
}
#  The platforms where the words are the owner's own.  Anything else needs the no-tools receipt ——
#  including BOT_CHAT_SOURCE below, which is not a platform Hermes records but the name to opt in by.
DEFAULT_SOURCES = ("cli", "tui", "desktop")
ROLES = ("user", "assistant")
SPEAKER = {"user": "Me", "assistant": "Hermes", "peer": "Peer agent"}

#  ── MESSAGE_FILTER: which rows are a turn someone wrote (2026-09-26) ───────────────────────────
#  Every source cited below and in BOT_CHAT and LINEAGE is one commit unless another is named ——
#  https://github.com/NousResearch/hermes-agent/tree/59004a62356f3a4697ab0fe8ad5086d2b405e2a6
#  (committed 2026-09-25, retrieved 2026-09-26).
#
#    · Compaction runs **in place** by default (`in_place: True`, hermes_cli/config_defaults.py:663).
#      The turns it folds away stay under the same session as `active=0, compacted=1`: kept.  The
#      summary goes into a row flagged `_compressed_summary=1` —— on its own, or, when there is no
#      room for a separate row, merged into the row that continues the conversation: "merged
#      carriers contain live user text" (agent/context_compressor.py:545).  That row's original is
#      then archived as a superseded duplicate, `active=0, compacted=0` (:321, and
#      hermes_state_messages.py:830) —— the same flags a rewind leaves, so it stays dropped, and
#      the carrier's live part is kept instead (see `turn_text`).  A summary is the model's words
#      about the conversation, never a turn.
#    · `display_kind` is an **allowlist**, the one Hermes uses for "real user turns": NULL, '' and
#      `steer` (a /steer row "is typed for the renderer but is human input") —— hermes_state_search.py:764.
#      Every other kind is bookkeeping that happens to sit in a user or assistant row: a model
#      switch, a delegation's report (`async_delegation_complete`), a bot's process notice
#      (`process_complete`, tools/bot_mode_dm.py:731), …  A denylist let each new kind in as "Me".
#      `hidden` passes the SQL and is judged in `turn_text`: "Hidden is the legacy compaction
#      wrapper and doesn't hide an unwrapped human payload" (agent/context_compressor.py:5479) ——
#      a build of 2026-08-12 stored carriers that way (run_agent.py:2258-2272 at
#      NousResearch/hermes-agent@9da6d45, committed 2026-08-12, retrieved 2026-09-26).  A hidden row
#      that is not such a carrier stays out.
#    · `observed=1` is a message the agent saw but was not addressed with.
#  NULL reads as each column's default (hermes_state_common.py:402): `active` 1, the rest 0.
MESSAGE_FILTER = ("(COALESCE(active, 1) = 1 OR COALESCE(compacted, 0) = 1) "
                  "AND COALESCE(observed, 0) = 0 "
                  "AND (display_kind IS NULL OR display_kind IN ('', 'steer', 'hidden'))")
#  The shapes a compaction handoff takes in a row's text (agent/context_compressor.py): every summary
#  prefix Hermes has shipped opens with one of SUMMARY_HEADS (:252, :293, :619).  A carrier keeps the
#  live text before MERGED_DELIMITER, behind PRIOR_HEADER (:521-522) —— or, where the summary has
#  to lead the message (Anthropic, Bedrock), after SUMMARY_END (:517).  Both are written today
#  (`_merge_summary_into_tail_row`, :5203-5217).
SUMMARY_HEADS = ("[CONTEXT COMPACTION", "[CONTEXT SUMMARY]:")
PRIOR_HEADER = "[PRIOR CONTEXT — for reference only; not a new message]"
MERGED_DELIMITER = "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]"
SUMMARY_END = "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"
#  An unfinished request Hermes restates after a compaction —— as its own user row, or appended to
#  the carrier after SUMMARY_END —— opens with this (`_INFLIGHT_TASK_REPLAY_HEADER`, :534; written at
#  :4755-4788).  The request itself survives where it was first said; the restatement is a copy.
INFLIGHT_HEADER = "[STILL IN PROGRESS — this is the active request, restated"
#  Cron output delivered into a Bot Chat is framed as "[Cronjob …] … scheduled job, not the user"
#  (cron/scheduler_delivery.py:802).
CRON = '[Cronjob "'
#  ── SKILL: a /skill turn carries the whole skill (impl round 4) ─────────────────────────────
#  `/work fix the title leak` is stored as ONE user row: an activation note, the skill's full body,
#  then the instruction (`build_skill_invocation_message`, agent/skill_commands.py:506; stacked
#  `/a /b …` at :544, header :312; queued by cli.py:1329 —— all at 59004a6).  Kept whole, the body
#  read as the owner's words, and the request at its end was cut by TURN_MAX or dropped by
#  render()'s 400-character dedupe when the same skill ran twice.  `skill_turn` writes what Hermes's
#  own preview of such a row says, "/name — instruction" (`describe_skill_invocation`, :97; its
#  transcript view joins them with a space instead), with the markers copied byte for byte (:36-46).
SKILL_PREFIX = "[IMPORTANT: The user has invoked the "
SKILL_NAME = re.compile(re.escape(SKILL_PREFIX) + r'"([^"]*)"')
SKILL_SINGLE = "The full skill content is loaded below.]"
SKILL_SINGLE_SAID = "The user has provided the following instruction alongside the skill invocation: "
SKILL_RUNTIME_NOTE = "\n\n[Runtime note:"
SKILL_BUNDLE = " skill bundle,"
SKILL_BUNDLE_SAID = "\nUser instruction: "
SKILL_BUNDLE_FIRST = "\n\n[Loaded as part of the "


def skill_turn(text):
    """A `/skill` row → what the owner typed ("/work — fix the title leak", or "/work"); any other
    text unchanged.  The body quotes the single-skill marker at times, so the LAST one is the
    owner's; a bundle puts the instruction before its skills, so there it is the FIRST."""
    if not text.startswith(SKILL_PREFIX):
        return text
    m = SKILL_NAME.match(text)
    name = (m.group(1) if m else "").strip()
    label = name if name.startswith("/") else f"/{name}"
    if SKILL_BUNDLE in text:                  # the instruction is in the header, before any skill
        at = text.split(SKILL_BUNDLE_FIRST, 1)[0].find(SKILL_BUNDLE_SAID)
        marker, stop = SKILL_BUNDLE_SAID, SKILL_BUNDLE_FIRST
    elif SKILL_SINGLE in text:
        at, marker, stop = text.rfind(SKILL_SINGLE_SAID), SKILL_SINGLE_SAID, SKILL_RUNTIME_NOTE
    else:
        at = -1
    said = text[at + len(marker):].split(stop, 1)[0].strip() if at >= 0 else ""
    if not name:
        return said
    return f"{label} — {said}" if said else label


#  ── PROMPT: /plan, /learn and /init queue a whole prompt as the user's turn (review round 5) ──
#  `_queue_prompt_turn` (hermes_cli/cli_commands_mixin.py:1912) puts the built prompt on the input
#  queue as a normal user turn —— plan mode's rules and craft notes (agent/plan_prompt.py:60-71),
#  the learn brief (agent/learn_prompt.py:136-197), the AGENTS.md brief
#  (hermes_cli/init_command.py:30-79), all at 59004a6: 2-10k characters of Hermes's words around
#  what was typed (measured: /plan 2,181, /init 2,152-2,910, /learn 9,985).  The same harm as a
#  /skill row, and the same treatment.
PROMPT_TURNS = (   # (opening, command, right before the typed text, right after it, before from the end)
    ("[/plan — plan mode]", "/plan", "\nTask to plan:\n", "\n\nWrite the plan for an implementer", False),
    ("[/learn] The user wants you to learn a reusable skill", "/learn", "\nTHE REQUEST:\n",
     "\n\nThe request is open-ended", False),
    #  in update mode the existing AGENTS.md comes before the notes (init_command.py:68-77)
    ("[/init] The user wants you to ", "/init",
     "\nUSER NOTES — honor these while authoring (they override the defaults above where they "
     "conflict):\n", None, True),
)
#  What /learn asks for when nothing was typed (learn_prompt.py:139-142) —— Hermes's words, exactly.
LEARN_DEFAULT = ("the workflow we just went through in this conversation — review the steps taken and "
                 "distill them into a reusable skill")


def prompt_turn(text):
    """A /plan, /learn or /init row → what the owner typed ("/plan — fix the login flow", or the
    command alone); any other text unchanged.  Hermes writes its closing text once, so the typed text
    ends at the LAST copy of it —— a request quoting it is not cut short (review round 6)."""
    for opening, cmd, before, after, last in PROMPT_TURNS:
        if text.startswith(opening):
            at = text.rfind(before) if last else text.find(before)
            said = text[at + len(before):] if at >= 0 else ""
            said = (said.rsplit(after, 1)[0] if after else said).strip()
            return f"{cmd} — {said}" if said and said != LEARN_DEFAULT else cmd
    return text


#  ── STEER: words typed mid-turn are stored inside a frame (impl round 4) ─────────────────────
#  They reach the model wrapped (`STEER_MARKER_OPEN`/`_CLOSE`, agent/prompt_builder.py:536-540 at
#  59004a6) and the row keeps the wrapper.  Hermes shows the words inside
#  (`_extract_steer_text_from_message`, agent/conversation_compression.py:2296, used by
#  tui_gateway/session_history.py:232): the full marker first, else its stable prefix to the end
#  of that line (:2272).
STEER_OPEN = ("[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at this "
              "position; not tool output and not a new delivery when replayed from conversation history]")
STEER_PREFIX = "[OUT-OF-BAND USER MESSAGE"
STEER_CLOSE = "[/OUT-OF-BAND USER MESSAGE]"


def steer_text(text):
    """The words inside the steer frame a row opens with; any other text unchanged.  Applied only
    to `display_kind='steer'` rows, as Hermes does (session_history.py:229) —— run on every row, a
    peer's DM that carried a frame came out as "Me" (review round 5)."""
    t = text.lstrip()
    if t.startswith(STEER_OPEN):
        start = len(STEER_OPEN)
    elif t.startswith(STEER_PREFIX):
        nl = t.find("\n")
        start = nl + 1 if nl >= 0 else len(STEER_PREFIX)
    else:
        return text
    end = t.find(STEER_CLOSE, start)
    return t[start:end].strip() if end >= 0 else text
#  ── SYNTHETIC: user rows no one typed (2026-09-26) ───────────────────────────────────────────
#  SessionDB drops the `_` metadata that would mark them, so Hermes itself tells them by content
#  (`_is_synthetic_compression_user_turn`, agent/context_compressor.py:4187; `_SYNTHETIC_PROMPT`,
#  hermes_state_timeline.py:13; `_SYNTHETIC_USER_PREFIXES`, agent/conversation_compression.py:2233
#  —— and :2705 at NousResearch/hermes-agent@3145986, committed 2026-08-31, the CLI of that day
#  queueing process and delegation reports as plain user input, cli.py:13535-13542).  Today's CLI
#  still queues process heartbeats and watch matches plain (hermes_cli/cli_process_notifications.py:42).
#  Measured in review: each of these came out as "Me".  One alternative per opening —— some cover
#  several templates:
SYNTHETIC = re.compile(
    r"\s*(?:"
    r"\[IMPORTANT: Background process |"             # a process finished or matched a watch pattern
    r"\[IMPORTANT: \d+ background processes completed|"      # …a batch of them
    #     (tools/process_registry_notifications.py:426, :444, :30; tools/process_registry.py:3303,
    #      :3338 at 3145986)
    r"\[IMPORTANT: Watch(?: patterns disabled for process |-pattern overflow: |"
    r"-pattern notifications resumed\.)|"          # watch notices, queued plain by the 3145986 CLI
    #     (tools/process_registry.py:641, :707, :788, :742, wrapped at :3290, :3296 at 3145986;
    #      today's CLI marks them internal, so only stores that build wrote hold them)
    r"\[Background process |"                        # a process heartbeat (…notifications.py:419)
    r"\[ASYNC (?:DELEGATION )?(?:BATCH )?(?:COMPLETE|TASK FAILED)|"   # a subagent's report
    r"A background (?:fan-out of \d+ subagent\(s\)|subagent) you dispatched earlier has finished\.|"
    #     (…notifications.py:277, :204, :166; tools/process_registry.py:3110, :3189 at 3145986.
    #      The sentence is the report's second line, under the `[ASYNC …]` header (…:278 at
    #      59004a6); matched as an opening too, defensively)
    r"\[Heartbeat — recurring instruction|"          # a /heartbeat firing (hermes_cli/heartbeat.py:22)
    r"\[Your active task list was preserved across context compression\]|"   # tools/todo_tool.py:21
    r"\[System: |"                                   # recovery nudges (agent/conversation_loop.py:885-952)
    r"Your previous turn indicated a tool call but none was included\.|"            # …:942
    r"You just executed tool calls but returned an empty response\.|"               # …:949
    r"You've reached the maximum number of tool-calling iterations allowed\.|"    # context_compressor.py:336
    r"Continue from the compressed conversation context above\.)",                 # …:327, :331
    re.I)
#  List content (text plus images) is stored as JSON behind this sentinel (hermes_state.py:1581);
#  only its text parts are a turn —— the rest is image data.
JSON_SENTINEL = "\x00json:"

#  ── BOT_CHAT: other agents' words (2026-09-26) ────────────────────────────────────────────────
#  A bot's canonical Bot Chat is a session titled exactly this (`CANONICAL_BOT_CHAT_TITLE`,
#  hermes_state.py:1577).  Other agents talk into it: a DM arrives as a plain **user** turn, no
#  display_kind, opening `Message from 🤖 <name> (@<handle>): ` (tools/bot_mode_dm.py:238), through
#  `chat -c "Bot Chat" --create-if-missing` (tools/bot_relay.py:87), which creates the session as
#  `source="cli"` if it is missing (hermes_cli/main.py:1456) —— a default platform —— and "nothing
#  verifies the sender itself" (tools/bot_relay.py:524).  So, security before convenience:
#    · a Bot Chat —— hidden or not —— is collected only when BOT_CHAT_SOURCE is opted into like any
#      non-default platform: behind the no-tools receipt, and it leaves the third-party mark;
#    · a user turn opening with PEER_DM is another agent's, wherever it appears: dropped unless
#      BOT_CHAT_SOURCE is opted into, and then labelled as a peer's, never as "Me".
#  `hidden` is a sidebar flag, not a statement about who wrote a session: `set_session_hidden` hides
#  "from the default listing; still resumable" (hermes_state_sessions.py:969).  Besides a Bot Chat
#  ("born hidden", hermes_state_titles.py:87) Hermes hides "group-chat plumbing" (apps/desktop/src/
#  types/hermes.ts:559), and no column tells that from a chat the user hid —— so any other hidden
#  session is dropped, counted under `hidden`; unhiding it brings it back.
BOT_CHAT = "Bot Chat"
BOT_CHAT_SOURCE = "bot-chat"
PEER_DM = "Message from 🤖 "
#  …on any line of a steer row's raw text: Hermes joins pending steers with newlines, and a frame whose
#  close was lost left the DM behind it —— both came out as "Me" (review round 6).
PEER_LINE = re.compile(r"(?m)^\s*" + re.escape(PEER_DM))

#  ── LINEAGE: which child sessions are conversations —— Hermes's own classification (2026-09-25) ──
#  A session with a parent is one of four things, and only three are a person talking:
#
#      compression   the same conversation, continued after its context was compacted  → stitched
#      /branch       a copy of the parent's transcript, then continued by the user      → its own
#      reset         a new conversation that keeps its lineage                          → its own
#      subagent      delegated work; its `user` turns are the parent agent's orders     → dropped
#
#  "Subagent" is Hermes's `_ephemeral_child_sql` —— a child that is not a branch, not a compression
#  continuation and not a reset (hermes_state_common.py:213, with `_BRANCH_CHILD_SQL` at :151 and
#  `_RESET_END_REASONS` at :159) —— plus any row carrying the `_delegate_from` marker, which Hermes's
#  own session picker hides too (hermes_state_sessions.py:104): a subagent whose parent later ended
#  by compression would otherwise pass for a continuation.  The first version of this file dropped
#  every child whose parent did not end by compression, which also threw away branches and resets
#  —— the user's own conversations.  Source: the commit named above MESSAGE_FILTER.
#  ⚠ A fork marker outranks the parent's end reason, and counts only when it names **this row's
#     parent.**  The end reason can be overwritten after the fork (a branched parent resumed and
#     re-closed —— the case the `_branched_from` marker was introduced for), and a continuation
#     inherits its parent's model_config verbatim, markers included (the note above
#     `_FORK_EDGE_EXCLUSION_SQL`, hermes_state_sessions.py:244), so the continuation of a branch
#     still carries `_branched_from` —— naming the branch's parent, not its own.
#  ⚠ A parent missing from the store leaves only the markers to go on.  With none, Hermes calls the
#     row ephemeral and so does this —— the conservative reading, and it is counted, not silent.
#  model_config is consulted for those markers inside SQLite and never selected: its values do not
#  reach Python, and the self-check plants a canary in it.
RESET_END_REASONS = ("session_reset", "session_switch", "idle", "daily", "suspended",
                     "resume_pending_expired")          # Hermes's `_RESET_END_REASONS`, same commit
_PARENT = "SELECT 1 FROM sessions p WHERE p.id = s.parent_session_id"


def _mark(name):
    """A model_config marker, read the way Hermes's `_sql_json_extract` does —— never throwing on
    text that is not JSON."""
    return (f"json_extract(CASE WHEN json_valid(s.model_config) THEN s.model_config "
            f"ELSE json_object() END, '$.{name}')")


_BRANCH = (f"({_mark('_branched_from')} IS NOT NULL OR EXISTS ({_PARENT} "
           "AND p.end_reason = 'branched' AND s.started_at >= p.ended_at))")
_COMPRESSION = f"EXISTS ({_PARENT} AND p.end_reason = 'compression')"
_RESET = (f"({_mark('_reset_from')} IS NOT NULL OR EXISTS ({_PARENT} AND p.end_reason IN "
          f"({', '.join(repr(r) for r in RESET_END_REASONS)}) AND s.session_key IS NOT NULL "
          "AND s.session_key != '' AND s.session_key = p.session_key))")
DELEGATED_SQL = (f"((s.parent_session_id IS NOT NULL AND NOT {_BRANCH} AND NOT {_COMPRESSION} "
                 f"AND NOT {_RESET}) OR {_mark('_delegate_from')} IS NOT NULL)")
CONTINUES_SQL = (f"({_COMPRESSION} AND NOT (" + " OR ".join(
    f"COALESCE({_mark(m)}, '') = s.parent_session_id"
    for m in ("_delegate_from", "_branched_from", "_reset_from")) + "))")


class SchemaError(SystemExit):
    """A required column is missing.  Raised, never swallowed —— an empty corpus that looks fine
    is the failure this repository fears most."""


class UnreadableError(SystemExit):
    """A folder that holds, or may hold, a session store cannot be read."""


def stores(home, unreadable=None):
    """The session databases under `home` —— and nothing else.

    ⚠ A folder that is there but cannot be read **stops the run** (impl round 4).  `isfile` and
       `glob` report it as empty, so a Hermes home at mode 0 read as "no session store", and a
       profile at mode 0 was left out of the count —— both exiting 0.  `unreadable(folder, why)`
       reports and skips instead, for a caller that only looks ids up (`known_sessions`).
    """
    def fail(d, why):
        if unreadable is None:
            raise UnreadableError(f"❌ {d}: cannot be read ({why}) —— the sessions in it would be skipped "
                                  "silently.  Fix its permissions, or point KAL_HERMES_HOME elsewhere.  "
                                  "Nothing is written.")
        unreadable(d, why)

    def ls(d):
        try:
            with os.scandir(d) as it:
                return {e.name: e for e in it}
        except (FileNotFoundError, NotADirectoryError):
            return {}
        except OSError as e:                  # EACCES, and ELOOP through a looping link
            fail(d, e.strerror)
            return {}

    def is_(e, kind):
        #  `DirEntry.is_dir()` raises past a link into an unreadable folder or around a loop, and
        #  says False for a link whose target is gone —— both a store this could not look into
        #  (review round 5).
        try:
            if e.is_symlink() and not os.path.exists(e.path):
                fail(e.path, "a link whose target is gone or cannot be read")
                return False
            return e.is_dir() if kind == "dir" else e.is_file()
        except OSError as err:
            fail(e.path, err.strerror)
            return False
    top = ls(home)
    out = [os.path.join(home, "state.db")] if "state.db" in top and is_(top["state.db"], "file") else []
    if "profiles" in top and is_(top["profiles"], "dir"):
        for name, e in sorted(ls(os.path.join(home, "profiles")).items()):
            if not name.startswith(".") and is_(e, "dir"):              # what `*` matched before
                db = ls(e.path).get("state.db")
                if db and is_(db, "file"):
                    out.append(os.path.join(home, "profiles", name, "state.db"))
    return out


def _open(path):
    #  Read-only by URI, so a lock held by a running Hermes never becomes our write.  The journal
    #  mode may be WAL or DELETE —— settable since v0.21.4, whose notes list `hermes sessions
    #  set-journal-mode` (https://github.com/NousResearch/hermes-agent/releases/tag/v2026.9.21,
    #  published 2026-09-21, retrieved 2026-09-26) —— and mode=ro reads both.
    return sqlite3.connect(f"file:{urllib.parse.quote(path)}?mode=ro", uri=True, timeout=5)


def check_schema(con, path):
    have = {}
    for table in REQUIRED:
        have[table] = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    missing = {t: sorted(set(cols) - have[t]) for t, cols in REQUIRED.items() if set(cols) - have[t]}
    if missing:
        raise SchemaError(f"❌ {path}: the Hermes schema changed —— missing {missing}.  "
                          "Nothing is written.  Update REQUIRED in ingest_hermes_sessions.py.")
    try:
        return con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    except sqlite3.Error:
        return None


def _iso(ts):
    try:
        return datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def turn_text(content, summary_flag=False, hidden=False):
    """A stored message body → the words someone wrote in it, or "" when there are none.

    A compaction handoff —— flagged `_compressed_summary`, or shaped like one —— is unwrapped the
    way Hermes's `_strip_context_summary_handoff_message` does it (agent/context_compressor.py:4375,
    recognised as in `classify_summary_content`, :4158): a carrier keeps only its live part, the
    text before MERGED_DELIMITER minus PRIOR_HEADER, or the text after SUMMARY_END where the
    summary leads; with neither it is a summary and nothing is kept.  Before this the flagged
    carrier was dropped whole, taking the owner's latest words with it, and an unflagged one kept
    its header as "Me" or lost the request that followed the summary.
    A `hidden` row counts only as such a carrier (MESSAGE_FILTER), and a restated request
    (INFLIGHT_HEADER) is a copy of words already kept.
    """
    if isinstance(content, str) and content.startswith(JSON_SENTINEL):
        try:
            parts = json.loads(content[len(JSON_SENTINEL):])
        except ValueError:
            return ""
        content = "\n\n".join(p["text"] for p in parts if isinstance(p, dict)
                              and p.get("type") == "text" and isinstance(p.get("text"), str)) \
            if isinstance(parts, list) else ""
    if not isinstance(content, str):
        return ""
    before, delim, after = content.partition(MERGED_DELIMITER)
    handoff = summary_flag or (after if delim else content).lstrip().startswith(SUMMARY_HEADS)
    if hidden and not handoff:
        return ""
    if handoff:
        if delim:
            content = before.strip()
            if content.startswith(PRIOR_HEADER):
                content = content[len(PRIOR_HEADER):].lstrip()
        elif SUMMARY_END in content:
            content = content.split(SUMMARY_END, 1)[1].lstrip()
        else:
            return ""
    return "" if content.lstrip().startswith((INFLIGHT_HEADER, CRON)) else content


def _sessions(con):
    """Every session row, as selected (SESSION_COLS) plus the three lineage flags."""
    sel = ", ".join("s." + c for c in SESSION_COLS)
    return {r[0]: dict(zip(SESSION_COLS + ("delegated", "continues", "branched"), r))
            for r in con.execute(f"SELECT {sel}, {DELEGATED_SQL}, {CONTINUES_SQL}, {_BRANCH} FROM sessions s")}


def _root(sess, sid):
    """The first session of the conversation `sid` is stitched into."""
    seen = set()
    while sess[sid]["continues"] and sid not in seen:     # CONTINUES_SQL implies the parent exists
        seen.add(sid)
        sid = sess[sid]["parent_session_id"]
    return sid


def chain_roots(path):
    """{session id: the first session of its conversation}, lower-cased —— the id its document and
    distill markers carry, which exclude.txt needs to find them from any session of it."""
    con = _open(path)
    try:
        check_schema(con, path)
        sess = _sessions(con)
    finally:
        con.close()
    return {str(s).lower(): str(_root(sess, s)).lower() for s in sess}


def read_store(path, sources=DEFAULT_SOURCES, excluded=None, roles=ROLES):
    """One state.db → (conversations, tally, schema version, every session id in it, lower-cased).

    conversations  [{session fields…, 'members': [ids], 'turns': [(speaker, text, ts), …]}], one
                   per stitched conversation, keyed by its first session
    tally          why the rest were left out, one count per conversation, first reason wins ——
                   `excluded` · `hidden` · `delegated` · `handoff` · `platform:<source>` (judged
                   last, so it means "would come in if opted in"; a Bot Chat counts as
                   `platform:bot-chat`) —— and `fork` (branch or reset conversations kept), `copied`
                   (branch messages dropped as the parent's copy), `cut` (conversations cut at an
                   exclude.txt timestamp), `peer` (other agents' messages dropped from them),
                   `synthetic` (user rows no one typed, see SYNTHETIC)

    `roles` is a parameter only so the self-check can prove its canaries are live (a broader role
    set must let them through); production callers never pass it.
    """
    excluded = excluded or {}
    con = _open(path)
    try:
        version = check_schema(con, path)
        sess = _sessions(con)
        q = ("SELECT session_id, role, content, timestamp, COALESCE(_compressed_summary, 0), "
             "COALESCE(display_kind, '') = 'hidden', COALESCE(display_kind, '') = 'steer' "
             f"FROM messages WHERE role IN ({','.join('?' * len(roles))}) AND {MESSAGE_FILTER} "
             "ORDER BY session_id, timestamp, rowid")
        rows = con.execute(q, roles).fetchall()
    finally:
        con.close()

    peers_in = BOT_CHAT_SOURCE in sources
    turns, peer, synth = collections.defaultdict(list), collections.Counter(), collections.Counter()
    for sid, role, content, ts, flag, hidden, steer in rows:
        text = turn_text(content, flag, hidden)
        if role == "user" and steer:
            text = steer_text(text)            # a steer row's words, before any check reads them (STEER)
        if sid not in sess or not text:
            continue
        if role == "user" and SYNTHETIC.match(text):                # a notice or nudge (SYNTHETIC)
            synth[sid] += 1
            continue
        speaker = SPEAKER.get(role, role)
        if role == "user" and (text.lstrip().startswith(PEER_DM)     # another agent's DM (BOT_CHAT)
                               or (steer and isinstance(content, str) and PEER_LINE.search(content))):
            if not peers_in:
                peer[sid] += 1
                continue
            speaker = SPEAKER["peer"]
        if role == "user":
            text = prompt_turn(skill_turn(text))    # a /skill, /plan, /learn or /init row (SKILL, PROMPT)
            if not text:
                continue
        turns[sid].append((speaker, text, ts))

    chains = collections.defaultdict(list)                      # first session → all of them, oldest first
    for sid in sorted(sess, key=lambda k: sess[k]["started_at"] or 0):
        chains[_root(sess, sid)].append(sid)

    out, tally = [], collections.Counter()
    for r, members in chains.items():
        ss = [sess[m] for m in members]
        #  Any session of a stitched conversation names all of it: it is one document.
        listed = [excluded[m.lower()] for m in members if m.lower() in excluded]
        bot_chat = any(s["title"] == BOT_CHAT for s in ss)
        off = ([BOT_CHAT_SOURCE] if bot_chat and not peers_in else []) + \
              [s["source"] for s in ss if s["source"] not in sources]
        why = ("excluded" if None in listed else
               "hidden" if any(s["hidden"] for s in ss) and not bot_chat else
               "delegated" if any(s["delegated"] for s in ss) else
               #  Any handoff state, `failed` included.  A handoff can fail after the gateway has
               #  re-bound the channel and dispatched the turn (gateway/run_startup.py:1766, then
               #  gateway/run_adapters.py:499), a restart fails a `running` one that "may already have
               #  switched the session key" (hermes_state_gateway.py:918-925), and a retry re-arms
               #  from `completed` (:853-860) —— all at 59004a6.  Round 4 kept `failed` from the
               #  CLI's timeout alone; that was wrong (review round 5).
               "handoff" if any(s["handoff_state"] for s in ss) else
               f"platform:{off[0]}" if off else None)
        if why:
            tally[why] += 1
            continue
        tally["peer"] += sum(peer[m] for m in members)
        tally["synthetic"] += sum(synth[m] for m in members)
        cut = min(listed) if listed else None
        #  A first session that still has a parent is a branch or a reset (a subagent was dropped
        #  above).  `/branch` copies the parent's transcript **with the parent's timestamps**
        #  (`_BRANCH_COPY_KEYS`, hermes_cli/cli_commands_mixin.py:209, and `_insert_message_rows`
        #  writes each stored time back onto the live message it copies from,
        #  hermes_state_messages.py:530) —— so anything stamped before the fork began is that copy,
        #  already in the parent's document.  A reset copies nothing, so the rule costs it nothing.
        fork = sess[r]["parent_session_id"] is not None
        start, t = sess[r]["started_at"], []
        #  Only a branch copied anything: a reset child cut before it began needs no parent cut
        #  (impl round 4).
        if sess[r]["branched"] and cut is not None and cut < start and sess[r]["parent_session_id"] in sess:
            #  ⚠ A cutoff before the branch began falls in text the branch only copied.  Those words
            #     are the parent conversation's, in its document —— cutting the branch leaves them
            #     there while the count says "cut".  Stop, unless that conversation is left out or
            #     cut at least as early.  (A parent no longer in the store has no document to leak.)
            pr = _root(sess, sess[r]["parent_session_id"])
            cover = [excluded[m.lower()] for m in chains[pr] if m.lower() in excluded]
            if not (None in cover or (cover and min(cover) <= cut)):
                raise SystemExit(
                    f"❌ exclude.txt cuts {r} at {_iso(cut)}, before it was branched from "
                    f"{sess[r]['parent_session_id']} at {_iso(start)} —— what it held before the branch is "
                    f"the conversation {pr}'s, and stays in that document.  List {pr} with the same "
                    "cutoff (or an earlier one), then run again.  Nothing is written.")
        for m in members:
            for x in turns.get(m, []):
                if fork and x[2] < start:
                    tally["copied"] += 1
                elif before_cutoff(x[2], cut):
                    t.append(x)
        tally["fork"] += fork
        tally["cut"] += cut is not None
        out.append({**sess[r], "schema_version": version, "store": path, "members": members, "turns": t})
    return out, tally, version, {str(s).lower() for s in sess}


def render(turns):
    """Turns → the transcript text the distiller reads.  Noise blocks dropped, repeats dropped."""
    parts, seen = [], set()
    for speaker, text, _ in turns:
        kept = [b for b in (text or "").split("\n\n") if not is_noise(b)]
        body = "\n\n".join(kept).strip()
        if not body:
            continue
        key = " ".join(body.split())[:400]
        if key in seen:
            continue
        seen.add(key)
        parts.append(f"**{speaker}**: {body[:TURN_MAX]}")
    return "\n\n".join(parts)


def project_of(cwd, source):
    if cwd:
        return os.path.basename(cwd.rstrip("/")) or cwd
    return source or "hermes"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sources", default=None,
                    help="comma-separated platforms to collect (default: KAL_HERMES_SOURCES, else "
                         "cli,tui,desktop).  Any other needs `just verify-extract-tools` first")
    ap.add_argument("--min-chars", type=int, default=500,
                    help="a conversation shorter than this after masking is dropped")
    ap.add_argument("--out", default=None, help=f"output json (default: <KAL_SESSIONS>/{DOCS})")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()

    raw, origin = a.sources, "--sources"
    if not raw:
        raw, origin = os.environ.get("KAL_HERMES_SOURCES", ""), "KAL_HERMES_SOURCES"
    sources = tuple(s.strip() for s in raw.split(",") if s.strip())
    if not sources:
        sources, origin = DEFAULT_SOURCES, "default"
    #  ⚠ Before a single store is opened, and asked of the receipt's *validity* —— not of a file
    #     merely existing, which a CLI upgrade or a flag change would leave behind as a lie.
    optin = [s for s in sources if s not in DEFAULT_SOURCES]
    if optin and not claude_cli.no_tools_verified():
        print(f"❌ {','.join(optin)}: sessions beyond {','.join(DEFAULT_SOURCES)} can carry other "
              "people's messages, and they must not reach an extraction model until `just "
              "verify-extract-tools` has confirmed that the model runs with zero tools, for this "
              f"claude CLI version (receipt: {claude_cli.NO_TOOLS_MARKER}).  Nothing is read or written.")
        return 1

    dbs = stores(HOME)
    if not dbs:
        #  Not an error: most machines have no Hermes, and `just openwiki-sessions` runs this for
        #  everyone.  Said out loud, so a wrong KAL_HERMES_HOME is visible.
        print(f"ⓘ no Hermes session store under {HOME} —— nothing to collect")
        return 0
    excluded = load_excluded()
    convs, tally, versions, ids = [], collections.Counter(), set(), set()
    for db in dbs:
        c, t, v, i = read_store(db, sources, excluded)
        convs += c
        tally += t
        versions.add(v)
        ids |= i
    matched = len(excluded.keys() & ids)

    kept, dropped, total_masked = [], 0, collections.Counter()
    for s in convs:
        text, nm = mask(render(s["turns"]))
        total_masked += nm
        if len(text) < a.min_chars:
            dropped += 1
            continue
        proj = project_of(s["cwd"], s["source"])
        ts = [t for _, _, t in s["turns"] if t is not None]
        kept.append({
            "session_id": s["id"], "project": proj, "agent": "hermes",
            "path": f"sessions/hermes/{proj}/{s['id']}.md",
            "abs_path": f"{s['store']}#{s['id']}", "n_msg": len(s["turns"]),
            #  every session stitched into this document —— distill matches exclude.txt against any
            "members": s["members"],
            "first_ts": _iso(min(ts)) if ts else _iso(s["started_at"]),
            "last_ts": _iso(max(ts)) if ts else None,
            "cwd": s["cwd"] or "", "source": s["source"],
            "schema_version": s["schema_version"],
            "masked": sum(nm.values()), "text": text,
        })

    #  ⚠ Verified **before** writing —— the order ingest_sessions had to be corrected to.
    leak = find_leaks("".join(x["text"] for x in kept))
    if leak:
        print(f"\n❌ masking verification failed — {sum(leak.values())} secret(s) remaining: {leak}")
        print("   Nothing is written.  Strengthen SECRETS in ingest_sessions.py.  (Pattern names only.)")
        return 1

    if optin:
        #  The corpus is about to hold text other people wrote.  claude_cli refuses every model
        #  call while this mark exists and the no-tools receipt does not hold —— distil and extract
        #  run later than the check above, after the CLI may have updated itself.  Written first,
        #  so a corpus with such text never exists without it.
        mark = claude_cli.THIRD_PARTY_MARK
        os.makedirs(os.path.dirname(mark), mode=0o700, exist_ok=True)
        with os.fdopen(os.open(mark, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as fh:
            fh.write(f"{datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')} "
                     f"ingest_hermes_sessions collected {','.join(optin)}\n")
        os.chmod(mark, 0o600)
    dest = a.out or os.path.join(OUT, DOCS)
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    #  Created 0600 rather than chmod-ed after: the twins leave a 644 window, harmless only inside
    #  the 700 sessions directory, and --out can point anywhere.
    with os.fdopen(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
        json.dump(kept, fh, ensure_ascii=False)
    os.chmod(dest, 0o600)                    # an older file keeps its old mode through O_TRUNC
    plat = ", ".join(f"{k.split(':', 1)[1]} {n}" for k, n in sorted(tally.items())
                     if k.startswith("platform:"))
    print(f"{len(dbs)} store(s), schema {sorted(v for v in versions if v is not None) or '?'}, "
          f"platforms {','.join(sources)} ({origin}) → {len(kept)} kept · {dropped} dropped "
          f"(shorter than {a.min_chars})")
    print(f"   skipped by platform: {plat or 'none'}")
    if plat:
        print("   (each is opt-in: `just verify-extract-tools`, then --sources or KAL_HERMES_SOURCES)")
    print("   skipped: " + " · ".join(f"{k} {tally[k]}" for k in ("hidden", "delegated", "handoff",
                                                                    "excluded")))
    print(f"   branch/reset conversations kept {tally['fork']} ({tally['copied']} copied message(s) "
          "of the parent dropped)")
    print(f"   rows no one typed dropped {tally['synthetic']} (process and subagent reports, heartbeats, "
          "task lists, recovery nudges)")
    print(f"   other agents' messages dropped {tally['peer']}"
          + ("" if BOT_CHAT_SOURCE in sources else f" (opt in with {BOT_CHAT_SOURCE}, as for a Bot Chat)"))
    print(f"   exclude.txt: {matched} of {len(excluded)} listed id(s) name a Hermes session —— "
          f"{tally['excluded']} left out whole · {tally['cut']} cut at a timestamp")
    if optin:
        print(f"   ⚠ {','.join(optin)} collected —— {claude_cli.THIRD_PARTY_MARK} now holds model calls "
              "to a valid no-tools receipt")
    print(f"secrets masked: {sum(total_masked.values())}   masking verification: ✅ (before writing)")
    print(f"→ {dest}")
    return 0


def _selftest():
    """Everything that would fail silently: canaries that must never surface, a schema that must
    fail loudly, the lineage rules, the platform gate, the exclusion list and the masking wire.

    Hermetic: the store is a fixture; the exclusion list, the no-tools receipt, the third-party mark
    and distillation's output live in a temp directory; the other collectors' view of which sessions
    exist is a fixture; and `claude_cli.no_tools_verified` is stood in for —— so nothing here reads
    the real ~/.kal or runs `claude --version`.
    """
    import contextlib, io, stat, tempfile
    from unittest import mock
    import ingest_sessions, distill_sessions
    ok = []
    T0 = 1790326800.0                                  # 2026-09-25T09:00:00Z
    CUTOFF = "2026-09-25T09:30:00Z"                    # T0 + 1800
    CANARY = {
        "system_prompts": "CANARY-SYSPROMPT-TABLE", "session_sp": "CANARY-SESSION-SYSPROMPT",
        "model_config": "CANARY-MODEL-CONFIG", "tool_calls": "CANARY-TOOL-CALLS",
        "reasoning": "CANARY-REASONING", "api_content": "CANARY-API-CONTENT",
        "unknown_col": "CANARY-UNKNOWN-COLUMN", "dotenv": "CANARY-DOTENV",
        #  message bodies —— each must be in the fixture (checked) and never in the corpus
        "tool_result": "CANARY-TOOL-RESULT", "hidden_display": "CANARY-HIDDEN-DISPLAY",
        "inactive": "CANARY-REWOUND", "observed": "CANARY-OBSERVED",
        "summary": "CANARY-COMPACTION-SUMMARY", "model_switch": "CANARY-MODEL-SWITCH",
        "async_delegation": "CANARY-ASYNC-DELEGATION", "bot_dm": "CANARY-BOT-DM",
        "cron": "CANARY-CRON-OUTPUT", "legacy_handoff": "CANARY-LEGACY-HANDOFF",
        "merged_summary": "CANARY-MERGED-SUMMARY", "image": "CANARY-IMAGE-DATA",
        "telegram": "CANARY-TELEGRAM", "buzz": "CANARY-BUZZ", "acp": "CANARY-ACP-CHANNEL",
        "handoff": "CANARY-HANDOFF", "hidden_session": "CANARY-HIDDEN-SESSION",
        "delegated": "CANARY-DELEGATED", "delegated_marked": "CANARY-MARKED-SUBAGENT",
        "excluded": "CANARY-EXCLUDED", "after_cut": "CANARY-AFTER-CUTOFF",
        "upper_id": "CANARY-CAPITALISED-ID",
        "carrier_summary_a": "CANARY-CARRIER-A-SUMMARY", "carrier_summary_b": "CANARY-CARRIER-B-SUMMARY",
        "headed_summary": "CANARY-HEADED-SUMMARY", "legacy_end_summary": "CANARY-LEGACY-END-SUMMARY",
        "peer_dm": "CANARY-PEER-DM", "dm_in_tui": "CANARY-DM-IN-TUI",
        #  one per kind of row Hermes writes as a user turn that no one typed (SYNTHETIC)
        "proc_done": "CANARY-PROC-DONE", "proc_batch": "CANARY-PROC-BATCH",
        "watch_match": "CANARY-WATCH-MATCH", "proc_heartbeat": "CANARY-PROC-HEARTBEAT",
        "subagent_report": "CANARY-SUBAGENT-REPORT", "subagent_old": "CANARY-SUBAGENT-OLD-OPENING",
        "recurring_heartbeat": "CANARY-RECURRING-HEARTBEAT", "task_list": "CANARY-TASK-LIST",
        "system_nudge": "CANARY-SYSTEM-NUDGE", "max_iter": "CANARY-MAX-ITERATIONS",
        "continuation": "CANARY-CONTINUATION-MARKER", "empty_nudge": "CANARY-EMPTY-NUDGE",
        "toolcall_nudge": "CANARY-TOOLCALL-NUDGE", "watch_disabled": "CANARY-WATCH-DISABLED",
        "watch_overflow": "CANARY-WATCH-OVERFLOW", "watch_resumed": "CANARY-WATCH-RESUMED",
        "indented": "CANARY-INDENTED-REPORT",
        #  any handoff state is a handed-off conversation; a peer's DM stays a peer's however framed
        "handoff_failed": "CANARY-FAILED-HANDOFF", "handoff_running": "CANARY-RUNNING-HANDOFF",
        "peer_framed": "CANARY-PEER-FRAMED", "peer_steered": "CANARY-PEER-STEERED",
        #  /plan, /learn, /init: Hermes's prompt around what was typed
        "plan_rules": "CANARY-PLAN-RULES", "plan_second": "CANARY-SECOND-PLAN",
        "plan_craft": "CANARY-PLAN-CRAFT", "learn_brief": "CANARY-LEARN-BRIEF",
        "init_brief": "CANARY-INIT-BRIEF", "init_old_notes": "CANARY-OLD-AGENTS-NOTES",
        "peer_joined": "CANARY-PEER-JOINED", "peer_unclosed": "CANARY-PEER-UNCLOSED",
        "handoff_chain": "CANARY-CHAIN-HANDED-OFF",
        #  a /skill row: the skill body, a marker the body quotes, the runtime note, a bundle's skills
        "skill_body": "CANARY-SKILL-BODY", "skill_quoted": "CANARY-SKILL-QUOTED-MARKER",
        "skill_note": "CANARY-SKILL-RUNTIME-NOTE", "skill_body2": "CANARY-SECOND-SKILL-BODY",
        "bundle_body": "CANARY-BUNDLE-BODY",
        "restated_row": "CANARY-RESTATED-ROW", "restated_carrier": "CANARY-RESTATED-IN-CARRIER",
        "hidden_carrier_summary": "CANARY-HIDDEN-CARRIER-SUMMARY",
    }
    BODIES = [k for k in CANARY if k not in ("system_prompts", "session_sp", "model_config",
                                             "tool_calls", "reasoning", "api_content",
                                             "unknown_col", "dotenv")]
    #  The words that must come through —— the owner's, however Hermes stored them.
    PRESENT = ("FIRST question", "ARCHIVED original turn, compacted in place", "STEER typed mid-turn",
               "MERGED-KEPT words before the delimiter", "MULTIMODAL text part", "CONTINUED answer",
               "TUI question", "RESET conversation", "BEFORE the cutoff",
               "CARRIER-A the owner's latest words", "CARRIER-B the request after the summary",
               "LEGACY-HEADED words behind the header", "LEGACY-END the request after the summary",
               "HIDDEN-CARRIER live ask", "OWNER quoting [IMPORTANT: Background process x] in passing",
               "/work — SKILL-ASK fix the title leak", "/work — SKILL-ASK-AGAIN a second request",
               "/clean /work — BUNDLE-ASK tidy the repo", "STEER-WRAPPED words typed mid-turn",
               "**Me**: STEER-SAME-LINE words", "**Me**: STEER-REWORDED words",
               "/plan — PLAN-ASK fix the login flow", "/plan — PLAN-ASK-AGAIN the signup flow",
               "/learn — LEARN-ASK the release checklist", "/init — INIT-ASK mention the just recipes",
               "QUOTED-TAIL-KEPT", "/init — INIT-REAL notes for the update")
    SYNTHETIC_ROWS = (   # (canary, the opening Hermes writes) —— each must stay out
        ("proc_done", "[IMPORTANT: Background process proc_1 exited (exit code 0).\n"),
        ("proc_batch", "[IMPORTANT: 3 background processes completed. "),
        ("watch_match", '[IMPORTANT: Background process proc_2 matched watch pattern "ERROR".\n'),
        ("proc_heartbeat", "[Background process proc_3 heartbeat #2 — still running after 120s]\n"),
        ("watch_disabled", "[IMPORTANT: Watch patterns disabled for process proc_4 — 3 consecutive "),
        ("watch_overflow", "[IMPORTANT: Watch-pattern overflow: >15 notifications in 60s "),
        ("watch_resumed", "[IMPORTANT: Watch-pattern notifications resumed. 4 match event(s) "),
        #  Hermes strips a row before comparing (agent/conversation_compression.py:2265 at 59004a6)
        ("indented", "  \n [ASYNC DELEGATION COMPLETE — d2]\n"),
        ("subagent_report", "[ASYNC DELEGATION COMPLETE — d1]\nA background subagent you dispatched "
                            "earlier has finished. "),
        ("subagent_old", "A background subagent you dispatched earlier has finished. "),
        ("recurring_heartbeat", "[Heartbeat — recurring instruction, fires every 1h]\n"),
        ("task_list", "[Your active task list was preserved across context compression]\n"),
        ("system_nudge", "[System: Your previous response was truncated by the output length limit. "),
        ("max_iter", "You've reached the maximum number of tool-calling iterations allowed. "),
        ("continuation", "Continue from the compressed conversation context above. "),
        ("empty_nudge", "You just executed tool calls but returned an empty response. "),
        ("toolcall_nudge", "Your previous turn indicated a tool call but none was included. "),
    )
    RESTATED = ("[STILL IN PROGRESS — this is the active request, restated after the compaction boundary "
                "because it was not finished yet. Continue it; do not start over.]\n")
    #  …and only once the Bot Chat is opted into, behind the receipt.
    OPTED = ("BOTCHAT my own words", "BOTRECV the bot answering its peer")
    #  /branch forks: (parent, child, when, the parent's end reason *now*, the copied question,
    #  what the branch said).  The second and third parents were re-closed after the fork, so
    #  only the child's `_branched_from` still says what it is —— `tui_shutdown` overwriting
    #  `branched`, and a parent resumed and compacted, whose end reason now reads as a continuation.
    BRANCHES = (("trunk", "br", 1900, "branched", "TRUNK-A question asked before the branch",
                 "BRANCH-A question after the branch"),
                ("trunk2", "br3", 2300, "tui_shutdown", "TRUNK-B question asked before the branch",
                 "BRANCH-B reclosed parent, still a conversation"),
                ("trunk3", "br4", 2600, "compression", "TRUNK-C question asked before the branch",
                 "BRANCH-C reclosed by compaction, still its own conversation"))
    KEPT = ["br", "br3", "br4", "cutme", "root", "rst", "trunk", "trunk2", "trunk3", "tui1"]

    def build(home, version, drop=None):
        os.makedirs(home, exist_ok=True)
        #  Files that must never be opened: unreadable, so opening one fails the run —— and their
        #  content is a canary too, for a machine where the mode does not bind (root).
        for f in (".env", "auth.json", "config.yaml", "config.yaml.bak.20260901"):
            p = os.path.join(home, f)
            with open(p, "w") as fh:
                fh.write(CANARY["dotenv"])
            os.chmod(p, 0)
        s_cols = [c for c in REQUIRED["sessions"] if c != drop]
        m_cols = [c for c in REQUIRED["messages"] if c != drop]
        #  Schema 30 is 26 plus columns this reader has never heard of, holding a canary.
        s_more = ["system_prompt"] + (["room_policy", "archived_at"] if version >= 30 else [])
        m_more = ["tool_calls", "reasoning", "api_content"] + (["reactions"] if version >= 30 else [])
        db = os.path.join(home, "state.db")
        con = sqlite3.connect(db)
        con.execute(f"CREATE TABLE sessions ({', '.join(s_cols + s_more)})")
        con.execute(f"CREATE TABLE messages (id INTEGER PRIMARY KEY, {', '.join(m_cols + m_more)})")
        con.execute("CREATE TABLE system_prompts (hash, prompt)")
        con.execute("CREATE TABLE schema_version (version)")
        con.execute("INSERT INTO schema_version VALUES (?)", (version,))
        con.execute("INSERT INTO system_prompts VALUES ('h', ?)", (CANARY["system_prompts"],))

        def put(table, cols, more, row, filler):
            con.execute(f"INSERT INTO {table} ({', '.join(cols + more)}) "
                        f"VALUES ({', '.join('?' * (len(cols) + len(more)))})",
                        [row.get(c) for c in cols] + [filler.get(c, CANARY["unknown_col"]) for c in more])

        def sess(sid, source="cli", at=0, parent=None, end=None, handoff=None, hidden=0, marks=None,
                 title="t"):
            put("sessions", s_cols, s_more, {
                "id": sid, "source": source, "started_at": T0 + at, "cwd": "/w/proj", "title": title,
                "parent_session_id": parent, "end_reason": end, "handoff_state": handoff,
                "hidden": hidden, "ended_at": T0 + at + 50 if end else None, "session_key": None,
                "model_config": json.dumps({**(marks or {}), "note": CANARY["model_config"]})},
                {"system_prompt": CANARY["session_sp"]})

        def msg(sid, role, content, at, active=1, kind=None, observed=0, compacted=0, summary=0):
            put("messages", m_cols, m_more, {
                "session_id": sid, "role": role, "content": content, "timestamp": T0 + at,
                "active": active, "display_kind": kind, "observed": observed,
                "compacted": compacted, "_compressed_summary": summary},
                {"tool_calls": CANARY["tool_calls"], "reasoning": CANARY["reasoning"],
                 "api_content": CANARY["api_content"]})

        secret = "sk-ant-api03-" + "A" * 30  # oh-my-airs:allow fake key: the secret-masking canary
        sess("root", at=0, end="compression")            # continued by `cont` after compaction
        msg("root", "user", "FIRST question, with a key " + secret, 1)
        msg("root", "assistant", "FIRST answer", 2)
        msg("root", "tool", CANARY["tool_result"], 3)
        msg("root", "assistant", CANARY["hidden_display"], 4, kind="hidden")
        msg("root", "user", CANARY["inactive"], 5, active=0)                 # rewound: not compacted
        msg("root", "user", CANARY["observed"], 6, observed=1)
        #  In-place compaction: the original stays, archived; the summary row replaces it.
        msg("root", "user", "ARCHIVED original turn, compacted in place", 7, active=0, compacted=1)
        msg("root", "user", CANARY["summary"], 8, summary=1)
        msg("root", "user", "STEER typed mid-turn by the owner", 9, kind="steer")
        msg("root", "user", CANARY["model_switch"], 10, kind="model_switch")
        msg("root", "user", CANARY["async_delegation"], 11, kind="async_delegation_complete")
        msg("root", "user", "[CONTEXT SUMMARY]: " + CANARY["legacy_handoff"], 12)
        msg("root", "user", "MERGED-KEPT words before the delimiter\n" + MERGED_DELIMITER +
            "\n[CONTEXT COMPACTION — REFERENCE ONLY] " + CANARY["merged_summary"], 13)
        msg("root", "user", JSON_SENTINEL + json.dumps([
            {"type": "text", "text": "MULTIMODAL text part"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + CANARY["image"]}}]), 14)
        #  Compaction carriers: the summary merged into the row that goes on, flagged —— in the
        #  delimiter form and the summary-first (end-marker) form.  The carried row's original is archived
        #  `active=0, compacted=0`, like a rewind.
        live_a = "CARRIER-A the owner's latest words, merged with the summary"
        msg("root", "user", live_a, 15, active=0)
        msg("root", "user", f"{PRIOR_HEADER}\n{live_a}\n{MERGED_DELIMITER}\n"
            f"[CONTEXT COMPACTION — REFERENCE ONLY] {CANARY['carrier_summary_a']}", 16, summary=1)
        msg("root", "user", f"[CONTEXT COMPACTION — REFERENCE ONLY] {CANARY['carrier_summary_b']}\n"
            f"{SUMMARY_END}\nCARRIER-B the request after the summary", 17, summary=1)
        #  …and the same two shapes without the flag, from before it existed.
        msg("root", "user", f"{PRIOR_HEADER}\nLEGACY-HEADED words behind the header\n{MERGED_DELIMITER}\n"
            f"[CONTEXT COMPACTION — REFERENCE ONLY] {CANARY['headed_summary']}", 18)
        msg("root", "user", f"[CONTEXT COMPACTION — REFERENCE ONLY] {CANARY['legacy_end_summary']}\n"
            f"{SUMMARY_END}\nLEGACY-END the request after the summary", 19)
        sess("cont", at=100, parent="root")
        msg("cont", "user", "CONTINUED question", 101)
        msg("cont", "assistant", "CONTINUED answer", 102)
        #  A subagent whose parent later ended by compression: Hermes's ephemeral test alone would
        #  read it as a continuation.  Its marker is what gives it away.
        sess("dlg2", at=60, parent="root", marks={"_delegate_from": "root"})
        msg("dlg2", "user", CANARY["delegated_marked"], 61)
        sess("tui1", "tui", at=200)
        msg("tui1", "user", "TUI question typed in the terminal interface", 201)
        msg("tui1", "user", PEER_DM + "stray (@stray): " + CANARY["dm_in_tui"], 202)   # not in a Bot Chat
        for i, (k, opening) in enumerate(SYNTHETIC_ROWS):
            msg("tui1", "user", opening + CANARY[k], 210 + i)
        msg("tui1", "user", "OWNER quoting [IMPORTANT: Background process x] in passing", 230)
        #  /skill rows as Hermes builds them: the same 6 KB skill twice (TURN_MAX cut the first
        #  request, the 400-character dedupe dropped the second), then a stacked bundle.
        def skill_row(body_canary, ask, quoted="", note=""):
            return ('[IMPORTANT: The user has invoked the "work" skill, indicating they want you to '
                    "follow its instructions. The full skill content is loaded below.]\n\n"
                    + "Follow the house style. " * 250 + body_canary
                    + (f"\nQuote: {SKILL_SINGLE_SAID}{quoted}" if quoted else "")
                    + "\n\n" + SKILL_SINGLE_SAID + ask
                    + (f"\n\n[Runtime note: {note}]" if note else ""))
        msg("tui1", "user", skill_row(CANARY["skill_body"], "SKILL-ASK fix the title leak",
                                      CANARY["skill_quoted"], CANARY["skill_note"]), 240)
        msg("tui1", "user", skill_row(CANARY["skill_body2"], "SKILL-ASK-AGAIN a second request"), 241)
        msg("tui1", "user", '[IMPORTANT: The user has invoked the "/clean /work" stacked skill bundle, '
            "loading 2 skills together. Treat every skill below as active guidance for this turn.]\n\n"
            "Skills loaded: clean, work\n\nUser instruction: BUNDLE-ASK tidy the repo\n\n"
            f'[Loaded as part of the stacked skill invocation "clean".]\n\nSee the example:'
            f'{SKILL_BUNDLE_SAID}{CANARY["bundle_body"]}', 242)
        msg("tui1", "user", f"{STEER_OPEN}\nSTEER-WRAPPED words typed mid-turn\n{STEER_CLOSE}", 243, kind="steer")
        msg("tui1", "user", f"{STEER_OPEN} STEER-SAME-LINE words{STEER_CLOSE}", 244, kind="steer")
        #  A later Hermes may reword the header: its stable prefix, to the end of that line.
        msg("tui1", "user", f"{STEER_PREFIX} — reworded]\nSTEER-REWORDED words\n{STEER_CLOSE}", 245,
            kind="steer")
        #  A peer's DM that carries a frame is still a peer's DM —— unwrapped on every row, it came out
        #  as "Me".  And one that arrived as a steer is unwrapped first, then caught.
        msg("tui1", "user", f"{PEER_DM}bot (@bot): {STEER_OPEN}\n{CANARY['peer_framed']}\n{STEER_CLOSE}", 246)
        msg("tui1", "user", f"{STEER_OPEN}\n{PEER_DM}bot (@bot): {CANARY['peer_steered']}\n{STEER_CLOSE}", 247,
            kind="steer")
        #  /plan twice (the same rules: the 400-character dedupe dropped the second), /learn, /init
        def plan_row(canary, ask):
            return ("[/plan — plan mode]\n\nFor this turn, you are in PLAN MODE — planning only. "
                    + "Read before you write. " * 30 + canary + f"\n\nTask to plan:\n{ask}\n\n"
                    "Write the plan for an implementer with zero context for the codebase. "
                    + (CANARY["plan_craft"] if canary == CANARY["plan_rules"] else ""))
        msg("tui1", "user", plan_row(CANARY["plan_rules"], "PLAN-ASK fix the login flow"), 248)
        msg("tui1", "user", plan_row(CANARY["plan_second"], "PLAN-ASK-AGAIN the signup flow"), 248.5)
        msg("tui1", "user", "[/learn] The user wants you to learn a reusable skill from the request "
            "below, and save it.\n\nTHE REQUEST:\nLEARN-ASK the release checklist\n\nThe request is "
            f"open-ended {CANARY['learn_brief']}", 249)
        msg("tui1", "user", "[/init] The user wants you to generate an AGENTS.md project-instructions "
            f"file for the project at: /p\n{CANARY['init_brief']}\n\nUSER NOTES — honor these while "
            "authoring (they override the defaults above where they conflict):\nINIT-ASK mention the "
            "just recipes", 249.5)
        #  /learn with nothing typed: Hermes fills in its own request —— not the owner's words
        msg("tui1", "user", "[/learn] The user wants you to learn a reusable skill from the request "
            f"below, and save it.\n\nTHE REQUEST:\n{LEARN_DEFAULT}\n\nThe request is open-ended", 249.7)
        #  A request that quotes Hermes's closing text keeps all of it; an update-mode /init whose
        #  existing AGENTS.md holds the notes marker gives the new notes, not the old (review round 6).
        msg("tui1", "user", "[/learn] The user wants you to learn a reusable skill from the request "
            "below, and save it.\n\nTHE REQUEST:\nLEARN-QUOTE the doc says\n\nThe request is open-ended "
            "QUOTED-TAIL-KEPT\n\nThe request is open-ended and so on", 249.8)
        msg("tui1", "user", "[/init] The user wants you to UPDATE the existing AGENTS.md project-instructions "
            "file for the project at: /p\nThe existing file:\nUSER NOTES — honor these while authoring (they "
            f"override the defaults above where they conflict):\n{CANARY['init_old_notes']}\n\nUSER NOTES — "
            "honor these while authoring (they override the defaults above where they conflict):\nINIT-REAL "
            "notes for the update", 249.85)
        #  A steer row with another agent's DM on a later line —— pending steers joined with a newline ——
        #  or behind a frame whose close was lost: both are the peer's (review round 6).
        msg("tui1", "user", f"{STEER_OPEN}\nOWNER-FIRST words\n{STEER_CLOSE}\n{PEER_DM}bot (@bot): "
            f"{CANARY['peer_joined']}", 249.9, kind="steer")
        msg("tui1", "user", f"{STEER_OPEN}\n{PEER_DM}bot (@bot): {CANARY['peer_unclosed']} and no close",
            249.95, kind="steer")
        #  An unfinished request restated after a compaction —— its own row, and appended to a
        #  carrier after the end marker.  Both are copies of what was said first.
        msg("tui1", "user", RESTATED + CANARY["restated_row"], 231)
        msg("tui1", "user", f"[CONTEXT COMPACTION — REFERENCE ONLY] summary\n{SUMMARY_END}\n\n"
            f"{RESTATED}{CANARY['restated_carrier']}", 232, summary=1)
        #  A carrier the 2026-08-12 build stored hidden: its live ask is the owner's.
        msg("tui1", "user", f"[CONTEXT COMPACTION — REFERENCE ONLY] {CANARY['hidden_carrier_summary']}\n"
            f"{SUMMARY_END}\nHIDDEN-CARRIER live ask", 233, kind="hidden", summary=1)
        sess("dlg", at=250, parent="tui1")               # a pre-marker subagent row: ephemeral
        msg("dlg", "user", CANARY["delegated"], 251)
        sess("rst", at=300, parent="tui1", marks={"_reset_from": "tui1"})
        msg("rst", "user", "RESET conversation, started fresh", 301)
        sess("tg", "telegram", at=400)
        msg("tg", "user", CANARY["telegram"], 401)
        sess("bz", "buzz", at=450)
        msg("bz", "user", CANARY["buzz"], 451)
        sess("acp1", "acp", at=500)
        msg("acp1", "user", CANARY["acp"], 501)
        sess("ho", at=600, handoff="completed")
        sess("hofail", at=620, handoff="failed")
        msg("hofail", "user", CANARY["handoff_failed"], 621)
        sess("horun", at=640, handoff="running")
        msg("horun", "user", CANARY["handoff_running"], 641)
        #  A handoff in a later session of a stitched conversation takes the whole conversation.
        sess("hoc1", at=660, end="compression")
        msg("hoc1", "user", CANARY["handoff_chain"], 661)
        sess("hoc2", at=680, parent="hoc1", handoff="completed")
        msg("hoc2", "user", "HOC2 continued on a messaging platform", 681)
        msg("ho", "user", CANARY["handoff"], 601)
        sess("hid", at=700, hidden=1)                    # hidden, and not a Bot Chat
        msg("hid", "user", CANARY["hidden_session"], 701)
        #  A bot's canonical Bot Chat: born hidden, the owner's own —— with another bot's message and
        #  a cron delivery inside it, neither of which is the owner.
        #  Two Bot Chats only to cover both flags —— Hermes keeps one per profile.  The desktop's is
        #  born hidden; the one `chat -c "Bot Chat" --create-if-missing` makes for an incoming DM is
        #  a visible `cli` session, and the DM in it is a plain user turn.
        sess("botrecv", "cli", at=740, title=BOT_CHAT)
        msg("botrecv", "user", PEER_DM + "peer (@peer): " + CANARY["peer_dm"], 741)
        msg("botrecv", "assistant", "BOTRECV the bot answering its peer", 742)
        sess("botchat", "desktop", at=750, hidden=1, title=BOT_CHAT)
        msg("botchat", "user", "BOTCHAT my own words to my bot", 751)
        msg("botchat", "user", CANARY["bot_dm"], 752, kind="process_complete")
        msg("botchat", "user", '[Cronjob "digest" output — scheduled job, not the user. Review it, '
            "act on anything that needs action, and summarize for the chat.]\n\n" + CANARY["cron"], 753)
        sess("exc", at=800)
        msg("exc", "user", CANARY["excluded"], 801)
        sess("UPPER-ID", at=850)                          # listed in lower case: must still go
        msg("UPPER-ID", "user", CANARY["upper_id"], 851)
        sess("cutme", at=900)
        msg("cutme", "user", "BEFORE the cutoff", 1000)
        msg("cutme", "assistant", CANARY["after_cut"], 1800)      # exactly at it: "at or after"
        for trunk, br, at, end, copied, own in BRANCHES:
            sess(trunk, at=at, end=end)
            msg(trunk, "user", copied, at + 1)
            sess(br, at=at + 100, parent=trunk, end="compression" if br == "br" else None,
                 marks={"_branched_from": trunk})
            msg(br, "user", copied, at + 1)                # the copy keeps the parent's timestamp
            msg(br, "user", own, at + 101)
        #  `br`'s compaction continuation inherits `_branched_from` —— naming the trunk, not `br`.
        sess("br2", at=2100, parent="br", marks={"_branched_from": "trunk"})
        msg("br2", "user", "BRANCH-A continued after compaction", 2101)
        con.commit()
        con.close()
        return db, secret

    def blob_of(convs):
        return mask("\n".join(render(c["turns"]) for c in convs))[0]

    #  Distillation's output points into the temp dir from the start —— load_excluded looks there
    #  for pages already made, and the real ~/.kal/distilled must not be read.
    with tempfile.TemporaryDirectory() as d, \
            mock.patch.object(distill_sessions, "OUT", os.path.join(d, "distilled")), \
            mock.patch.object(distill_sessions, "DONE", os.path.join(d, "distilled", ".done")):
        home = os.path.join(d, "hermes")
        db26, secret = build(home, 26)
        db30, _ = build(os.path.join(home, "profiles", "work"), 30)
        excl_path = os.path.join(d, "sessions", "exclude.txt")
        os.makedirs(os.path.dirname(excl_path))
        with open(excl_path, "w") as fh:
            fh.write(f"# reasons stay out of the repository\nEXC\ncutme {CUTOFF}\nupper-id\n"
                     "a-claude-session\n")               # another collector's: counted, not matched
        known = {k: v for db in (db26, db30) for k, v in chain_roots(db).items()}
        assert (known["cont"], known["br2"], known["br"], known["dlg2"]) == ("root", "br", "br", "dlg2"), \
            "chain_roots does not name each session's conversation the way read_store stitches it"
        known["a-claude-session"] = "a-claude-session"
        excluded = load_excluded(excl_path, known=known)

        #  ① Only session stores are opened —— never .env / auth.json / config.yaml / *.bak*.
        assert stores(home) == [db26, db30], stores(home)
        #    …and a folder that cannot be read stops the run instead of reading as empty.
        #    (Root reads a mode-0 folder anyway, so there it cannot be staged.)
        for _locked in ((os.path.join(home, "profiles", "work"), home) if os.geteuid() else ()):
            os.chmod(_locked, 0)
            try:
                stores(home)
                raise AssertionError(f"{_locked} at mode 0 read as holding no session store")
            except UnreadableError as e:
                assert _locked in str(e), e
            finally:
                os.chmod(_locked, 0o700)
        #    …and so does a profile link that loops or points nowhere: `DirEntry.is_dir()` raised
        #    past the first and said "not a folder" for the second (review round 5).
        for _bad in ("loops", "nowhere"):
            _lp = os.path.join(home, "profiles", _bad)
            os.symlink(_lp if _bad == "loops" else os.path.join(home, "gone"), _lp)
            try:
                stores(home)
                raise AssertionError(f"a profile link that {_bad} read as holding no session store")
            except UnreadableError as e:
                assert _lp in str(e), e
            finally:
                os.remove(_lp)
        #    …and the root is found the way Hermes finds it when HERMES_HOME is set (review round 6).
        _nat = os.path.expanduser("~/.hermes")
        for _env, _want in (("", _nat), ("/opt/h", "/opt/h"), ("/opt/h/profiles/work", "/opt/h"),
                            (os.path.join(_nat, "profiles", "x"), _nat)):
            with mock.patch.dict(os.environ, {"HERMES_HOME": _env}):
                assert os.path.realpath(_hermes_root()) == os.path.realpath(_want), (_env, _hermes_root())
        ok.append("files: only state.db and profiles/*/state.db (credential files unreadable)")

        #  ② Both schema versions: canaries never surface, lineage is Hermes's, each skip is counted.
        want_skips = {"delegated": 2, "hidden": 1, "handoff": 4, "excluded": 2, "platform:bot-chat": 2,
                      "platform:telegram": 1, "platform:buzz": 1, "platform:acp": 1}
        texts = []
        for db, v in ((db26, 26), (db30, 30)):
            convs, tally, version, _ids = read_store(db, DEFAULT_SOURCES, excluded)
            assert version == v, version
            ids = sorted(c["id"] for c in convs)
            assert ids == KEPT, ids
            skips = {k: n for k, n in tally.items() if k not in ("fork", "copied", "cut", "peer", "synthetic")}
            assert skips == want_skips, f"schema {v}: {skips}"
            assert (tally["fork"], tally["copied"], tally["cut"], tally["peer"], tally["synthetic"]) == \
                (4, 3, 1, 5, len(SYNTHETIC_ROWS)), tally
            blob = blob_of(convs)
            leaked = [k for k, val in CANARY.items() if val in blob]
            assert not leaked, f"schema {v}: canaries reached the corpus: {leaked}"
            assert LEARN_DEFAULT not in blob, f"schema {v}: /learn's own filler reads as the owner's"
            assert "OUT-OF-BAND" not in blob and "direct message from the user" not in blob, \
                f"schema {v}: a steer frame reached the corpus"
            for need in PRESENT + ("BRANCH-A continued after",) + tuple(own for *_, own in BRANCHES):
                assert need in blob, f"schema {v}: {need!r} is missing"
            by_id = {c["id"]: render(c["turns"]) for c in convs}
            for trunk, br, _, _, copied, own in BRANCHES:
                assert blob.count(copied) == 1 and copied in by_id[trunk], \
                    f"{br}: the parent's transcript, copied by /branch, survived in the branch"
                assert own in by_id[br], f"{br}: the branch lost its own words"
            assert "BRANCH-A continued after" in by_id["br"], \
                "an inherited `_branched_from` split a compaction continuation off its branch"
            assert blob.index("FIRST question") < blob.index("CONTINUED answer"), \
                "the compression chain is out of order"
            assert "**Me**: FIRST question" in blob and "**Hermes**: FIRST answer" in blob, \
                "speakers are not marked"
            assert secret not in blob and not find_leaks(blob), "masking is not wired"
            assert PRIOR_HEADER not in blob and SUMMARY_END not in blob and "[CONTEXT" not in blob, \
                "a compaction header reached the corpus as a turn"
            texts.append(blob)
        assert texts[0] == texts[1], "schema 26 and 30 read differently —— the reader keyed on a version"
        ok.append("schema 26 and 30 alike: columns · roles (tool) · platforms · handoff · hidden · "
                  "observed · rewound · delegated (marked and not) canaries stay out, each skip counted")
        ok.append("messages as Hermes stores them: an in-place compaction keeps the archived original "
                  "and drops its summary; display_kind is an allowlist (steer kept; model_switch, "
                  "async_delegation_complete, process_complete out); legacy and merged handoffs, "
                  "cron output and image data never read as the owner")
        ok.append("compaction carriers keep their live words —— flagged or not, delimiter or end-marker "
                  "form —— and never their summary or header")
        ok.append("rows no one typed stay out, one canary per kind: process and subagent reports, "
                  "heartbeats, task lists, recovery nudges, the iteration limit, a restated request "
                  "(its own row or in a carrier); a hidden legacy carrier gives up its live ask")
        ok.append("a Bot Chat (hidden or not) and other agents' DMs anywhere stay out by default; any other "
                  "hidden session stays out")
        ok.append("lineage as Hermes defines it: compression stitched, reset kept, branches kept "
                  "without the parent's copy —— also when the parent was re-closed after the fork, "
                  "and an inherited marker does not split a continuation off")
        ok.append("exclude.txt: a listed id is gone (capitals too), a cutoff drops the message at it "
                  "and after")

        #  ③ The canaries are live —— widening a filter must let them through, and the SQL-filtered
        #     ones must really be in the store.  Without this, ② passes just as happily on a fixture
        #     that never held them.
        raw = sqlite3.connect(db26)
        for k in BODIES:
            assert raw.execute("SELECT COUNT(*) FROM messages WHERE instr(content, ?) > 0",
                               (CANARY[k],)).fetchone()[0] == 1, f"the {k} canary is not in the fixture"
        for _, br, _, _, copied, _ in BRANCHES:
            assert raw.execute("SELECT COUNT(*) FROM messages WHERE session_id = ? AND content = ?",
                               (br, copied)).fetchone()[0] == 1, f"{br} holds no copied prefix"
        raw.close()
        wide = blob_of(read_store(db26, DEFAULT_SOURCES, excluded, roles=ROLES + ("tool",))[0])
        assert CANARY["tool_result"] in wide, "the tool-result canary is not live"
        wide = blob_of(read_store(db26, DEFAULT_SOURCES + ("acp", "telegram", "buzz"), excluded)[0])
        assert all(CANARY[k] in wide for k in ("acp", "telegram", "buzz")), "a platform canary is not live"
        wide = blob_of(read_store(db26, DEFAULT_SOURCES, {})[0])
        assert CANARY["excluded"] in wide and CANARY["after_cut"] in wide, "an exclusion canary is not live"
        ok.append("mutation: widening roles, platforms or the exclusion list lets the canaries through; "
                  "the SQL-filtered canaries and each branch's copied prefix are in the store")

        #  ④ A missing column fails loudly and names it —— a selected one, a filtering one, and one
        #     the lineage rule needs.
        for col in ("handoff_state", "hidden", "observed", "compacted", "session_key"):
            bad_db, _ = build(os.path.join(d, "bad-" + col), 30, drop=col)
            try:
                read_store(bad_db)
                raise AssertionError(f"a missing {col} column was not reported")
            except SchemaError as e:
                assert col in str(e), str(e)
        ok.append("schema: a missing column stops the run and names it")

        #  ④b A cutoff before a branch began cuts text the branch only copied —— its parent's words,
        #     still in the parent's document.  It stops and names that conversation, unless the
        #     parent is left out or cut at least as early.
        try:
            read_store(db26, DEFAULT_SOURCES, {"br": T0 + 1950})
            raise AssertionError("a cutoff before the fork was reported as cut")
        except SystemExit as e:
            assert "List trunk with the same cutoff" in str(e), str(e)
        read_store(db26, DEFAULT_SOURCES, {"br": T0 + 1950, "trunk": T0 + 1950})
        read_store(db26, DEFAULT_SOURCES, {"br": T0 + 1950, "trunk": None})
        read_store(db26, DEFAULT_SOURCES, {"br": T0 + 2050})           # after the fork: the branch's own
        read_store(db26, DEFAULT_SOURCES, {"br": T0 + 2000})           # at it: nothing copied is cut
        _rc, *_ = read_store(db26, DEFAULT_SOURCES, {"rst": T0 + 250})  # a reset copied nothing —— no stop,
        assert [c["turns"] for c in _rc if c["id"] == "rst"] == [[]], \
            "a reset cut before it began kept its words"                  # …and everything after goes
        ok.append("a cutoff before a branch's fork stops and names the parent conversation, unless "
                  "that is cut as early or left out")

        #  ⑤ The command end to end —— the gate, where the platforms come from, the files it writes.
        #     The receipt and the third-party mark point into the temp dir and the check is stood in
        #     for, so nothing on this machine decides the outcome.  A receipt *file* is planted that
        #     does not verify: only the check may open the gate, never the file existing.
        checks = os.path.join(d, "kal", "checks")
        os.makedirs(checks)
        with open(os.path.join(checks, "extract-no-tools.ok"), "w") as fh:
            fh.write("{}")
        third = os.path.join(checks, "third-party-corpus")
        verified = [False]

        def run(*args, env=None, ok_receipt=False):
            verified[0] = ok_receipt
            if env is None:
                os.environ.pop("KAL_HERMES_SOURCES", None)
            else:
                os.environ["KAL_HERMES_SOURCES"] = env
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(list(args) + ["--min-chars", "1"])
            return rc, buf.getvalue()

        def corpus(path):
            with open(path) as fh:
                return "\n".join(r["text"] for r in json.load(fh))

        saved_env = os.environ.get("KAL_HERMES_SOURCES")
        with mock.patch.object(sys.modules[__name__], "HOME", home), \
                mock.patch.object(ingest_sessions, "EXCLUDE", excl_path), \
                mock.patch.object(ingest_sessions, "known_sessions", lambda unreadable=None: known), \
                mock.patch.object(claude_cli, "NO_TOOLS_MARKER", os.path.join(checks, "extract-no-tools.ok")), \
                mock.patch.object(claude_cli, "THIRD_PARTY_MARK", third), \
                mock.patch.object(claude_cli, "no_tools_verified", lambda version=None: verified[0]):
            try:
                out = os.path.join(d, "out", "default.json")
                rc, log = run("--out", out)
                assert rc == 0, log
                assert stat.S_IMODE(os.stat(out).st_mode) == 0o600, "the corpus is readable by others"
                assert not os.path.exists(third), "a default run left the third-party mark"
                with open(out) as fh:
                    recs = json.load(fh)
                assert {r["agent"] for r in recs} == {"hermes"} and len(recs) == 2 * len(KEPT), recs
                text = "\n".join(r["text"] for r in recs)
                assert not [k for k, val in CANARY.items() if val in text], "a canary reached the file"
                assert "TUI question" in text and "exc" not in {r["session_id"] for r in recs}
                for line in ("skipped by platform: acp 2, bot-chat 4, buzz 2, telegram 2",
                             "other agents' messages dropped 10 (opt in with bot-chat",
                             f"rows no one typed dropped {2 * len(SYNTHETIC_ROWS)} ",
                             "skipped: hidden 2 · delegated 4 · handoff 8 · excluded 4",
                             "kept 8 (6 copied message(s)", "(default)",
                             "3 of 4 listed id(s) name a Hermes session —— 4 left out whole · 2 cut"):
                    assert line in log, f"{line!r} is not printed:\n{log}"

                #  Every platform beyond the default is gated —— a messaging platform as much as acp.
                for args, env in ((("--sources", "cli,acp"), None), (("--sources", "cli,telegram"), None),
                                  (("--sources", "cli,bot-chat"), None),
                                  ((), "cli,acp")):
                    bad = os.path.join(d, "out", f"refused-{'-'.join(args) or env}.json")
                    rc, log = run("--out", bad, *args, env=env)
                    assert rc != 0 and not os.path.exists(bad), \
                        f"{args or env} was collected without a verified no-tools receipt:\n{log}"
                    assert "verify-extract-tools" in log and not os.path.exists(third), log
                #  the flag outranks the environment —— so the environment's acp never reaches the gate
                rc, log = run("--out", os.path.join(d, "out", "flag.json"), "--sources", "cli,tui",
                              env="cli,acp")
                assert rc == 0 and "(--sources)" in log, log
                out = os.path.join(d, "out", "opted-in.json")
                rc, log = run("--out", out, "--sources", "cli,tui,desktop,acp,telegram,bot-chat",
                              ok_receipt=True)
                assert rc == 0, log
                assert CANARY["acp"] in corpus(out) and CANARY["telegram"] in corpus(out), \
                    "an opted-in platform did not come in once the receipt held"
                #  …and the model calls that come later can see that it did
                assert os.path.exists(third) and stat.S_IMODE(os.stat(third).st_mode) == 0o600, \
                    "collecting other people's text left no third-party mark (or a readable one)"
                with open(third) as fh:
                    assert "acp,telegram,bot-chat" in fh.read(), \
                        "the third-party mark does not say what was collected"
                #  The Bot Chat and the DMs come in —— as a peer's words, never as the owner's.
                opted = corpus(out)
                for need in OPTED + (CANARY["peer_dm"], CANARY["dm_in_tui"]):
                    assert need in opted, f"{need!r} did not come in once bot-chat was opted into"
                assert f"**{SPEAKER['peer']}**: {PEER_DM}peer (@peer)" in opted, \
                    "another agent's DM was not labelled as a peer's"
                assert f"**Me**: {PEER_DM}" not in opted, "another agent's DM was labelled as the owner's"
                assert CANARY["bot_dm"] not in opted and CANARY["cron"] not in opted, \
                    "a process notice or cron output came in as a turn"

                #  ⚠ The refusal before writing —— the masking is stubbed out so the fixture's key
                #     reaches the write path, and nothing may be written.  Without this, dropping
                #     the refusal leaves every other check green: masking works, so it never fires.
                leak_out = os.path.join(d, "out", "leak.json")
                with mock.patch.object(sys.modules[__name__], "mask",
                                       lambda t: (t, collections.Counter())):
                    rc, log = run("--out", leak_out)
                assert rc != 0 and not os.path.exists(leak_out), \
                    f"an unmasked key reached the write path and was written:\n{log}"
                assert "masking verification failed" in log, log

                #  ⚠ An abbreviated id stops the run —— it reads as applied and excludes nothing.
                with open(excl_path, "w") as fh:
                    fh.write("exc\ncutm\n")
                bad = os.path.join(d, "out", "abbreviated.json")
                try:
                    run("--out", bad)
                    raise AssertionError("an abbreviated exclude.txt id was accepted")
                except SystemExit as e:
                    assert "'cutm' is only the start of session cutme" in str(e.code), e.code
                assert not os.path.exists(bad), "a run stopped by an abbreviated id still wrote its output"
            finally:
                if saved_env is None:
                    os.environ.pop("KAL_HERMES_SOURCES", None)
                else:
                    os.environ["KAL_HERMES_SOURCES"] = saved_env
        ok.append("every platform beyond cli·tui·desktop (acp, telegram, bot-chat) is refused unless "
                  "no_tools_verified() holds —— a receipt file alone does not count —— by flag or by "
                  "env, and comes in once it holds; --sources outranks KAL_HERMES_SOURCES")
        ok.append("an opt-in run leaves the third-party mark (0600, naming the platforms); a default "
                  "run does not")
        ok.append("the command writes 0600, agent=hermes, prints every skip per platform and how "
                  "many exclude.txt ids matched; an unmasked key stops it before writing; an "
                  "abbreviated id stops it")

        #  ⑥ Masking, the exclusion list and the noise rule are shared objects, not copies.
        assert mask is ingest_sessions.mask and find_leaks is ingest_sessions.find_leaks
        assert load_excluded is ingest_sessions.load_excluded and TURN_MAX is ingest_sessions.TURN_MAX
        import ingest_codex_sessions
        assert is_noise is ingest_codex_sessions.is_noise
        ok.append("masking, the exclusion list, TURN_MAX and the noise rule are imported, not copied")

    for line in ok:
        print(f"  ✅ {line}")
    print("  ── every self-check above ran (read the list, do not count) ──")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
