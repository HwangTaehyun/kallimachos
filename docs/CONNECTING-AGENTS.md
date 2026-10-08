# Connecting to agent runtimes — Buzz · Hermes · others

kal is an **MCP server**. It exposes six read-only tools by default; four extraction tools only
when `KAL_MCP_WRITE=1` on a host stdio server (see `skills/kal-extract`):

| Tool | What |
|---|---|
| `kal_search` | natural-language exploration (the main entry point) |
| `kal_entity` | what a known name is + its sources + a change summary |
| `kal_timeline` | the whole history of changes |
| `kal_neighbors` | one hop in the graph |
| `kal_doc` | citation verification — the source text |
| `kal_stats` | what the graph holds — sources, agents, date range, types, index age |

`kal_stats` arrives with 0.2.0. Images built from v0.1.2 or earlier source expose only the first
five. Go by the source, not the tag: the README builds whatever you have checked out and tags it
with the manifest's version.

### The four write tools — local stdio only, opt-in

`kal_extract_begin`/`kal_extract_next`/`kal_extract_submit`/`kal_extract_finish` fill in the
knowledge graph's entities/relationships using your own agent's model and tokens. They exist only
on a **local stdio** server (§① below) started with `KAL_MCP_WRITE=1` in its own process
environment — the server reads that flag once at import time and only then registers the four
tools; a client cannot turn it on after connecting. Neither the shipped plugin Docker image (its
`docker run` mounts the vault and DB `:ro` and runs `--read-only --cap-drop ALL`) nor the hosted
remote server (`mcp.kallimachos.dev`, a shared multi-tenant process) ever sets this flag, so they
never expose these tools regardless of what a client asks for. See `skills/kal-extract/SKILL.md`
for the setup command and the full extraction workflow.

There are **two** transports, and most of this document is about telling which one you can use.

```
  ┌─ ① local stdio ────────────────────────────────────────────┐
  │   src/kal_mcp.py  ←  the agent spawns it as a child process │
  │   · it **opens no** network listener (by design, it cannot) │
  │   · reads the knowledge DB (~/.kal) directly on that machine│
  │   · the default for self-hosting — always works, any client │
  └────────────────────────────────────────────────────────────┘

  ┌─ ② remote Streamable HTTP (hosted, kallimachos.dev) ───────┐
  │   header form   https://mcp.kallimachos.dev/mcp             │
  │                 + Authorization: Bearer <token>   ← use this│
  │                   if your client supports headers (the      │
  │                   secret stays out of the config file)      │
  │   URL form      https://mcp.kallimachos.dev/u/<credential>/mcp │
  │                 · the path itself is the credential — giving │
  │                   out the address is giving out the account  │
  └────────────────────────────────────────────────────────────┘
```

Tokens and addresses are created on the hosted app's MCP screen
(`https://app.kallimachos.dev/mcp`).

---

## Hermes Agent

[Hermes Agent](https://hermes-agent.nousresearch.com/docs/) (Nous Research) attaches MCP through
the `mcp_servers` block in the profile's `config.yaml` —
[MCP Config Reference](https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference)
(retrieved 2026-08-31).

⚠ **Give kal only to a private agent** (rule 1 of the security section). Unless a platform has a
tool list of its own, Hermes attaches every `mcp_servers` entry to every platform it serves —
Telegram, Slack, Buzz, ACP — so anyone who can message the agent there can have it post your
notes into the chat. So kal lives in a profile of its own that no chat can reach. Create it blank
and set it up yourself — one line; `setup` is interactive:

```bash
hermes profile create kal-private && hermes -p kal-private setup
```

In `setup`, pick a model provider and nothing else: no messaging platform, no gateway token. Do
not create the profile with `--clone`. A clone copies your current `config.yaml` and `.env`, and
up to Hermes v0.21.2 that includes bot tokens and enabled platforms
([profiles.md at v2026.9.11](https://github.com/NousResearch/hermes-agent/blob/939e45c91d/website/docs/user-guide/profiles.md?plain=1#L51-L57)
— committed 2026-09-11, retrieved 2026-09-26). Later versions strip the channels but still copy
your other API keys
([profiles.md](https://github.com/NousResearch/hermes-agent/blob/5307e93252/website/docs/user-guide/profiles.md?plain=1#L120-L128)
— committed 2026-09-25, retrieved 2026-09-26).

What keeps chats away from kal is an invariant you maintain, not a switch:

- the profile's `.env` holds no bot or gateway token, and its `config.yaml` enables no platform;
- no `gateway.profile_routes` entry (in the default profile's config) names it;
- it is not your `hermes profile use` default.

A gateway that serves several profiles — multiplexing is on by default, `gateway.multiplex_profiles`
— starts each profile's platforms with that profile's own credentials, and a bot shared from the
default profile reaches another profile only through a `gateway.profile_routes` entry
(multi-profile-gateways.md
[L101-L112](https://github.com/NousResearch/hermes-agent/blob/5307e93252/website/docs/user-guide/multi-profile-gateways.md?plain=1#L101-L112),
[L182-L186](https://github.com/NousResearch/hermes-agent/blob/5307e93252/website/docs/user-guide/multi-profile-gateways.md?plain=1#L182-L186),
[L735-L741](https://github.com/NousResearch/hermes-agent/blob/5307e93252/website/docs/user-guide/multi-profile-gateways.md?plain=1#L735-L741)
— committed 2026-09-25, retrieved 2026-09-26). No token, no platform and no route: no chat lands in `kal-private`.
`hermes profile use` breaks this from the other side. It makes the profile the default for later
CLI **and gateway** runs, so a bot you add or a gateway you start afterwards lands on it and
serves kal to chats (profiles.md
[L203-L212](https://github.com/NousResearch/hermes-agent/blob/5307e93252/website/docs/user-guide/profiles.md?plain=1#L203-L212)
and [L315-L316](https://github.com/NousResearch/hermes-agent/blob/5307e93252/website/docs/user-guide/profiles.md?plain=1#L315-L316)
— committed 2026-09-25, retrieved 2026-09-26).

Both examples below go in `~/.hermes/profiles/kal-private/config.yaml` (or
`$HERMES_HOME/profiles/kal-private/config.yaml` if set). If that file already has
an `mcp_servers:` key, put `kal:` under it. A second top-level `mcp_servers:` is not merged:
Hermes v0.21.5 keeps only the last one
([`utils.py`](https://github.com/NousResearch/hermes-agent/blob/f97608f178/utils.py#L454-L456),
tag v2026.9.24 — committed 2026-09-24), and later builds refuse the file as a duplicate key
([`hermes_yaml.py`](https://github.com/NousResearch/hermes-agent/blob/5307e93252/hermes_yaml.py#L1-L4) — committed 2026-09-25)
— both retrieved 2026-09-26.

A profile is not a sandbox, though — the Profiles page says so: *"Profiles do **not** sandbox the
agent."* ([profiles.md](https://github.com/NousResearch/hermes-agent/blob/5307e93252/website/docs/user-guide/profiles.md?plain=1#L222-L228)
— committed 2026-09-25, retrieved 2026-09-26). Hermes' messaging toolsets include a terminal by
default ([`toolsets.py`](https://github.com/NousResearch/hermes-agent/blob/d956f0ae57/toolsets.py) — committed 2026-09-23,
retrieved 2026-09-25), so a chat agent in another profile can still read `~/.kal` or start this profile
itself. The separate profile keeps kal's tools out of chats; it does not stop an agent that can run
commands. Treat any chat-facing agent with a terminal as able to read your notes.

The commented `platform_toolsets` lines in both examples are a weaker fallback, for a profile that has to
serve a chat platform anyway. It fails open: every enabled platform needs its own line (Buzz too;
ACP honours it only from Hermes v0.21.5), a platform enabled later gets kal again, saving in
`hermes tools` removes `no_mcp`, and on Discord an explicit list also switches on the server-admin
tools. Keep the platform's own toolset in each list — `[no_mcp]` alone turns off the built-in
tools as well
([`hermes_cli/tools_config.py`](https://github.com/NousResearch/hermes-agent/blob/7c3d5e93e8/hermes_cli/tools_config.py#L590-L733)
— committed 2026-09-24, retrieved 2026-09-25).

### Local stdio (self-hosted · free)

Carry over the hardening flags from the repository root's [`.mcp.json`](../.mcp.json):

```yaml
# ~/.hermes/profiles/kal-private/config.yaml
mcp_servers:
  kal:
    command: "docker"
    args: [
      "run", "--rm", "-i",
      "--network", "none",
      "--read-only", "--cap-drop", "ALL",
      "--security-opt", "no-new-privileges", "--tmpfs", "/tmp",
      "--user", "UID:GID",
      "-v", "/absolute/path/.kal/db:/data/db:ro",
      "-v", "/absolute/path/vault:/vault:ro",
      "ghcr.io/hwangtaehyun/kal:0.1.2",
    ]

# weaker fallback only — read the warning above first
# platform_toolsets:
#   telegram: [hermes-telegram, no_mcp]
#   slack: [hermes-slack, no_mcp]
```

- Change only the left side of the two `-v` mounts to your own paths (absolute — `~` expansion
  differs per host).
- Set `UID:GID` **to your own values** (`id -u` / `id -g`: usually `1000:1000` on Linux, `501:20`
  on macOS). Leaving it as-is makes docker refuse loudly, which is better than silently running
  as root. A wrong value means the mounted folder cannot be read, and that shows up as an
  **empty result with no error** (measured, see README).
- ⚠ `ghcr.io/hwangtaehyun/kal:<version>` **cannot be pulled publicly yet** (measured 2026-08-31 —
  an anonymous `docker pull` fails with denied). Until then, build it from the repository:
  `docker compose -f docker-compose.kal.yml build` → tag `kal:local` → replace the image name
  above.
- Without docker: `command: /absolute/path/kal/.venv/bin/python`,
  `args: ["/absolute/path/kal/src/kal_mcp.py"]`, `env: { KAL_HOME: "/Users/me/.kal" }` — it must
  be **the `.venv` python**, since the system python has none of the dependencies.

### Remote (hosted)

Hermes supports headers, so use **Bearer instead of the URL form**. After the profile exists and
`setup` is done, the token goes in the profile's `.env`, never on a command line. Paste this one
line into bash or zsh and press Enter; at `kal token:`, paste the token (the app's MCP screen shows it
in a box of its own) and press Enter. Nothing is echoed. (Not fish: its `read` has no `-r` —
[`read`](https://github.com/fish-shell/fish-shell/blob/master/doc_src/cmds/read.rst), continuously
developed, retrieved 2026-09-26.)

```bash
printf 'kal token: ' && read -rs KAL_CLOUD_TOKEN && echo && { [ -d ${HERMES_HOME:-$HOME/.hermes}/profiles/kal-private ] || { echo 'no kal-private profile yet: run hermes profile create kal-private && hermes -p kal-private setup first'; false; }; } && (umask 077; printf '\nKAL_CLOUD_TOKEN=%s\n' "$KAL_CLOUD_TOKEN" >> ${HERMES_HOME:-$HOME/.hermes}/profiles/kal-private/.env) && chmod 600 ${HERMES_HOME:-$HOME/.hermes}/profiles/kal-private/.env; unset KAL_CLOUD_TOKEN
```

It is one line on purpose. Pasted as several lines into a shell without bracketed paste (macOS
`/bin/bash` 3.2), `read` takes the next pasted line as the token, and the token you paste after
that runs as a command and lands in your shell history — so `read` comes right after the prompt,
before anything else, and the profile check comes after `read`, never before it. `printf` is a
shell builtin, so unlike an argument to `hermes config set` the token never shows up in a process
list; `umask` covers a new file and `chmod` one that already exists, and the leading `\n` keeps the
line apart from a last line that lacks a newline. Never commit that file. After rotating the token,
run the line again: it appends the new token, and Hermes takes the last `KAL_CLOUD_TOKEN=` line
([`env_loader.py`](https://github.com/NousResearch/hermes-agent/blob/5307e93252/hermes_cli/env_loader.py#L287-L305)
— committed 2026-09-25, retrieved 2026-09-26), so the old line can simply be deleted. Then restart
Hermes — a running one keeps the old token and keeps sending it, since the `.env` is read only at
startup. Without the profile, the line still reads the token and discards it before printing the
hint and stopping, so nothing pasted afterward is ever left for the shell to run as a command.

⚠ The token then also sits in Hermes' own environment. The profile's `.env` is loaded into it
([`env_loader.py`](https://github.com/NousResearch/hermes-agent/blob/a6578fcaa5/hermes_cli/env_loader.py#L315) — committed 2026-09-25), and the terminal and
`execute_code` hand
that environment to every command they run: the scrub drops a fixed list of provider and tool
keys, not `KAL_CLOUD_TOKEN`
([`local_env_policy.py`](https://github.com/NousResearch/hermes-agent/blob/a6578fcaa5/tools/environments/local_env_policy.py#L46-L95),
[`local.py`](https://github.com/NousResearch/hermes-agent/blob/a6578fcaa5/tools/environments/local.py#L240-L270) — committed 2026-09-25), and no setting adds
a name to it.
So give the kal profile no shell, or a sandboxed one. In its `config.yaml` (merge under an
existing `agent:` or `terminal:` key):

- `agent.disabled_toolsets: [terminal, code_execution]` — removed even where a bundle lists them
  ([`model_tools.py`](https://github.com/NousResearch/hermes-agent/blob/a6578fcaa5/model_tools.py#L334-L339) — committed 2026-09-25), or
- `terminal.backend: docker` — terminal, file and `execute_code` calls then run in a container
  that does not inherit host credentials; keep `KAL_CLOUD_TOKEN` out of `docker_forward_env`
  ([configuration.md](https://github.com/NousResearch/hermes-agent/blob/a6578fcaa5/website/docs/user-guide/configuration.md?plain=1#L656-L673)
  — committed 2026-09-25).

All hermes-agent a6578fcaa5, retrieved 2026-09-26.

```yaml
# ~/.hermes/profiles/kal-private/config.yaml
mcp_servers:
  kal:
    url: "https://mcp.kallimachos.dev/mcp"
    headers:
      Authorization: "Bearer ${KAL_CLOUD_TOKEN}"

# weaker fallback only — read the warning above first
# platform_toolsets:
#   telegram: [hermes-telegram, no_mcp]
#   slack: [hermes-slack, no_mcp]
```

`${KAL_CLOUD_TOKEN}` resolves from that profile's `.env`. Hermes also accepts `${env:KAL_CLOUD_TOKEN}`
([MCP Config Reference](https://github.com/NousResearch/hermes-agent/blob/5307e93252/website/docs/reference/mcp-config-reference.md?plain=1#L75-L86)
— committed 2026-09-25, retrieved 2026-09-26); this page and the app use the plain form. The default transport is
Streamable HTTP, so no `transport` key is needed. There is no `tools.include` list on purpose: it
would silently hide any tool a later kal version adds.

The two examples share the key `kal` because you pick one of them. To keep both, split the keys
into `kal-local` and `kal-remote` (reusing one key silently overwrites the other) but **do not
enable both at once** — the same tool names appear from both sides and there is no guarantee which
the agent calls. Leave one at `enabled: false` and flip that single line to switch.

### Project instructions

Writing the usage into the project instructions file (`AGENTS.md`, or
`~/.hermes/profiles/kal-private/SOUL.md` for every session of that profile — each profile has its
own `SOUL.md`, and `~/.hermes/SOUL.md` belongs to the default one) tells the agent when to reach for
the tools:

```markdown
## Knowledge lookup

Argument names are exact: `kal_search` takes `query` and `top` (how many hits; default 20). The MCP SDK
**ignores unknown arguments without an error**, so a call with `limit: 3` is answered with the default 20 hits and
nothing says why (measured 2026-09-06). Check the count you get back once.

For questions about my notes, try `kal_search` first. The answer carries its sources (`docs`),
so cite them directly. Use `kal_doc` when you need the original text.
```

---

## Buzz (block/buzz)

In Buzz, kal attaches **to the teammate agent, not to Buzz itself.** There are two paths:

```
Buzz Relay ──WS──> buzz-acp ──stdio──> ACP agent
                                        ├─ (A) external agent (hermes acp · goose acp ·
                                        │      claude-agent-acp · codex-acp)
                                        │      → kal goes in that agent's own MCP config
                                        └─ (B) Buzz's built-in buzz-agent
                                               → stdio only, received via session/new
```

### (A) When an external agent is the teammate — each has its own MCP config

| Teammate | Where kal attaches |
|---|---|
| Hermes | §Hermes above — kal stays in the `kal-private` profile, and a Buzz teammate does **not** get it. Buzz starts a Hermes teammate with `HERMES_ACP_SKIP_CONFIGURED_MCP=1` unless you set that variable yourself, so the teammate loads no MCP server from any `config.yaml` ([`default_agent_env`](https://github.com/block/buzz/blob/ea1e97e65f/crates/buzz-acp/src/config.rs#L799-L817) — committed 2026-09-24, retrieved 2026-09-26; [ACP Host Integration](https://hermes-agent.nousresearch.com/docs/user-guide/features/acp) — retrieved 2026-09-26, still true at main per [acp.md L325-L337](https://github.com/NousResearch/hermes-agent/blob/d0288be5b3/website/docs/user-guide/features/acp.md?plain=1#L325-L337) — committed 2026-09-26). And `hermes-acp` started without `HERMES_HOME` runs the default profile (`~/.hermes`), not your `hermes profile use` choice ([`get_hermes_home`](https://github.com/NousResearch/hermes-agent/blob/f97608f178/hermes_constants.py#L112-L119), tag v2026.9.24 — committed 2026-09-24, retrieved 2026-09-26). Do not add kal to the default profile to reach Buzz — every platform that profile serves then gets kal, not only Buzz, and once the skip variable is off anyone who can message or mention the teammate can have it post your notes (rule 1 of the security section). Buzz Desktop shows Hermes **automatically** under Settings → Runtimes if Hermes is installed. On the server side, the Buzz channel connection (relay bridge) links it via `hermes acp` (stdio) — **if you plan to run this on a server, read the host axis in the security section first.** |
| Goose | the stdio extension in the [Extensions docs](https://block.github.io/goose/) (command = the docker line from §Hermes local) |
| Claude Code | the project's `.mcp.json` — the repository's [`.mcp.json`](../.mcp.json) is canonical |
| Codex | `mcp_servers` in `config.toml` (same docker command) |

**Check**: @-mention the agent once in a Buzz DM with "find ○○ with kal_search". If the answer
comes back with sources (`docs`) attached, it is connected. Read the security section **before**
adding it to a channel.

### (B) When Buzz's built-in buzz-agent is the teammate — stdio only

buzz-agent's ACP `initialize` response says so itself: `mcpCapabilities.http: false` ·
`sse: false` — *"No HTTP, no SSE."*
([crates/buzz-agent/README.md](https://github.com/block/buzz/blob/main/crates/buzz-agent/README.md) — retrieved 2026-08-31).
⚠ **The hosted remote address cannot go here** — use ① (local stdio).

It is received **at launch time**, not from a config file — the client passes it in the
`mcpServers` array of the `session/new` request:

```json
{
  "mcpServers": [
    {
      "name": "kal",
      "command": "/absolute/path/kal/.venv/bin/python",
      "args": ["/absolute/path/kal/src/kal_mcp.py"],
      "env": [ { "name": "KAL_HOME", "value": "/Users/me/.kal" } ]
    }
  ]
}
```

Note that `env` is an **array of objects** (`{name, value}`), not the usual `{"KEY":"value"}`
shape.

To pass docker instead, move `command` / `args` from [`.mcp.json`](../.mcp.json) into that array,
but ⚠ **do not copy it verbatim** — that file's `${user_config.vault_dir}`,
`${user_config.kal_dir}`, and `${user_config.run_as}` are slots the Claude Code plugin fills in.
Any other harness passes those strings literally and `docker run` dies:

| Slot | What to put |
|---|---|
| `${user_config.vault_dir}` | absolute path to your notes folder |
| `${user_config.kal_dir}` | the knowledge DB folder (usually `~/.kal`) |
| `${user_config.run_as}` | `id -u`:`id -g` (e.g. `501:20`) |

---

## Other clients — the general rule

| What the client supports | Use |
|---|---|
| stdio only | ① local `src/kal_mcp.py` |
| Streamable HTTP + headers | ② Bearer form (recommended), or ① |
| Streamable HTTP, no headers (claude.ai custom connectors and similar) | ② URL form |
| SSE only (legacy) | ① — the remote is Streamable HTTP |

**stdio is the side that always works.** If you are not sure which to use, start with stdio.

### Checking that it connected

`just mcp-test` actually calls all seven read-only tools by default; four extraction tools only
when `KAL_MCP_WRITE=1` on a host stdio server (see `skills/kal-extract`) — it needs a real vault
(`kal_doc` reads a document). On Claude Code, `claude mcp list` should show kal as ✔ Connected. Pasting the app's
Claude Code line runs `claude mcp add … --header "Authorization: Bearer $KAL_CLOUD_TOKEN"`; while
that command runs, the token is a command-line argument, and anyone else on the machine can see it
with `ps` — this matters on a machine other people log into. If kal was added with a command that
had no `--scope` (the default scope is local), also run `claude mcp remove kal --scope local` in
that project folder: there the local entry takes precedence over the user one, so an old token
keeps being used after a rotation
([scope hierarchy](https://code.claude.com/docs/en/mcp#scope-hierarchy-and-precedence) — continuously
updated, retrieved 2026-09-26). The commands carry no
trailing `#` comments: interactive zsh, the macOS default, does not treat `#` as a comment.

```bash
just mcp-test
claude mcp list | grep kal
```

For manual diagnosis, use the same flags and mounts as §Hermes local:
`docker run --rm -i [same flags and mounts] kal:local src/kal_mcp.py --selftest`
— ⚠ you **must** include `src/kal_mcp.py` after the image name. The image's ENTRYPOINT is
`python`, so passing only the argument runs `python --selftest` with no script.

An empty response is usually one of four things:

1. **There is no knowledge DB** — check with `just status`, and if empty run `just init`
2. **A path is wrong** — use absolute paths everywhere. An agent's working directory is not
   predictable
3. **The wrong python** — name `.venv/bin/python` explicitly. The system python has none of the
   dependencies
4. **`--user` does not match** — an unreadable mount produces an empty result with no error
   (§Hermes local above)

A Hermes Buzz teammate without kal's tools is the intended state, not a fault — read §Buzz-A
before changing `HERMES_ACP_SKIP_CONFIGURED_MCP`.

---

## Install and sync on each device

Multiple machines (a laptop, a desktop, a headless host running Hermes) can keep **one** knowledge
graph in sync through a single private GitHub repository instead of each pushing its own full
export — the design is `docs/DESIGN-GITHUB-SYNC.md` in the private workspace (not shipped here;
`kal/` only carries the code the design produced). Each device runs the same two commands.

### `kal login`

Registers this device with `kal cloud` using the OAuth Device Authorization Grant
([RFC 8628](https://datatracker.ietf.org/doc/html/rfc8628)) — the same shape as `gh auth login`:

```
just login                       # or: python3 src/kal_cli.py login [--url ...] [--name my-mac]
  Open https://app.kallimachos.dev/device and enter code: WDJB-MJHT
  (or open the printed verify_uri_complete link directly)
  logged in as device 'my-mac' on https://app.kallimachos.dev
```

The server URL must use `https://` (plain `http://` is accepted only for a loopback host during
local development); anything else is rejected before any request is sent.

Open the link, approve the device, and this machine gets a **device credential** — not a GitHub
token. It is saved to `~/.config/kal/device.json` (`0700`/`0600`, deliberately **outside**
`~/.kal` — that directory is a docker volume mount on some setups, and a credential does not
belong on a surface that gets backed up, cloned, or imaged alongside the knowledge DB). The
credential's only job is proving "this device" to `kal cloud` later, when it asks for a short-lived
(1 hour) GitHub token — kal never stores or sees a long-lived GitHub credential of yours.

- `just whoami` — shows the login (URL + device name) and nothing else. The token itself is never
  printed, not at login and not at whoami.
- `just logout` — deletes the saved credential. The device stays approved server-side until you
  also revoke it from the web app's Connections screen.

### `kal sync`

```
just sync-github                 # or: python3 src/kal_cli.py sync [--repository ID] [--notify-build]
python3 src/kal_cli.py sync --status     # outcome of the last sync (also hook and autosync runs); exits 1 if it failed
```

With more than one connected repository, pass `--repository ID` (list the IDs with `kal
repositories`); without it `kal sync` stops and asks. `--notify-build` asks the cloud to start a
build after a successful push and is off by default.

One run does, in order: ask `kal cloud` for a 1-hour GitHub token → `git pull` the bundle
(hardened: no hooks, no filesystem-watcher config, no inherited git credential helper, no `ext://`
protocol, no LFS smudge — the same hardening a connected GitHub bundle uses for every git call) →
distil this device's new sessions through the existing `just openwiki-sessions` pipeline (which
already skips sessions still being written — see the settle rule in `distill_sessions.py`) →
extract this device's pending chunks and publish the new knowledge-graph cache lines (the extraction is an **LLM step**: it runs on this device through the Claude CLI backend, `device_extract.py`, and costs the device owner's tokens; `kal sync` refuses while an MCP extraction job holds chunks) → commit **only this device's own
files** (its `personal/sessions/<device>/` folder, its own ledger and extract-cache files — never
another device's) with a `Kal-Host: <device>` trailer → push. If another device pushed in the
meantime, `kal sync` fetches and rebases **this device's own commits** on top and retries (bounded);
a real conflict stops the sync and asks for a manual look rather than resolving blindly — `kal
sync` never force-pushes. The one exception is the generated shared indexes (`index.md`,
`personal/index.md`, `personal/sessions/index.md`): when a rebase conflicts only there, sync takes the
remote copy, finishes the rebase, then regenerates the indexes and commits them.

**Recovery when the remote history was rewritten.** If someone force-pushed the bundle repository,
`kal sync` refuses to rebase or push (it would republish commits the rewrite removed). Once you trust
the rewritten remote, set `B` to the bundle path, then:

- (1) save unpushed work: `git -C "$B" format-patch refs/kal/synced.. -o ~/kal-unpushed` (only if that ref is missing, use `origin/main..`), and save uncommitted files too: `git -C "$B" stash -u` (then `git stash pop` after step 3) or copy them out
- (2) `git -C "$B" update-ref -d refs/kal/synced`
- (3) `git -C "$B" fetch https://github.com/<owner>/<repo>.git main && git -C "$B" reset --hard FETCH_HEAD` (the plain https URL of the repository, with no token: sync's short-lived token is not available to plain git, so this needs your own git credentials), or re-clone
- (4) rerun `kal sync` — it re-records this device's ledger rows from its local markers

Step 4 needs nothing else: sessions this device already distilled keep their local `.done` markers, and
the next run writes any ledger row the reset removed back from those markers, so other devices do not
distil the same sessions again. A checkout that never recorded `refs/kal/synced` is compared against its
own `origin/<branch>`; with neither, a history that has diverged from the remote is refused too.
`kal sync` also rebases unpushed local commits onto the remote *before* distilling, so other devices'
ledger rows are always seen first; a conflicting rebase stops the sync.

The GitHub token this step receives never touches `.git/config`, a command line argument, or a log
line — git receives it only through a `GIT_ASKPASS` helper reading an environment variable set for
that one subprocess (`src/git_askpass.py`).

**Prerequisites**: this device must already have the bundle git-cloned somewhere `KAL_VAULT` points at,
else the `vault` entry in `~/.kal/config.json` (`kal sync` does not clone it for you — see `just openwiki` above to build a
bundle the first time), and your account must have connected a GitHub repository from kal's web
Connections screen first (`kal sync` reports a clear "no GitHub connection" if it has not).

### Other subcommands

| Command | What it does |
|---|---|
| `kal repositories` | Lists the repositories this device may sync, one per line: `ID`, `owner/name`, `enabled`/`disabled`, build status. Use the ID with `--repository`. |
| `kal autosync [--interval SECONDS]` | Runs one sync now, then again every 30 minutes (default `1800`) in the foreground until stopped. Opt-in; accepts `--repository` and `--notify-build`. |
| `kal session-end` | Runs one sync cycle; meant for a Claude Code `SessionEnd` hook. Accepts `--repository` and `--notify-build`. |
| `kal session-end --print-hook` | Prints the hook JSON snippet (30-minute timeout) without installing or running anything. |

### Installing the `kal` command

`kal login`/`kal logout`/`kal whoami`/`kal sync`/`kal autosync`/`kal session-end`/`kal repositories`
are subcommands of `src/kal_cli.py`
(`kal_cli`, `device_auth`, `github_sync` in `src/`). `pyproject.toml` declares the console script
`kal = "kal_cli:main"` and sets `package = true`, so the project builds as a wheel:

- **From a source checkout**: `uv sync` installs the `kal` entry point into the project
  environment; run it as `uv run kal <subcommand>`.
- **Without a checkout**: install the built wheel and `kal <subcommand>` is on your `PATH`.
- **Source-checkout shortcuts** call the same dispatcher: `just login`/`just logout`/`just whoami`/
  `just sync-github`, or `python3 src/kal_cli.py <subcommand>`.

---

## ⚠ Security — read before connecting

An agent with kal attached is **a search engine over your personal knowledge**. In a shared
workspace (Buzz) the attack surface is four axes, not one channel, and the rules below are an
operating posture rather than an enforcement mechanism:

```
 ┌ channel axis     anyone can drive the agent with an @mention        → rule 1
 ├ context axis     kal results already in context leak without tools  → rule 2
 ├ stored injection instructions inside a vault note surface later     → rule 3
 └ host axis        a server deployment puts the token and vault on the host → rule 4
```

1. **Give kal only to a private agent (DM or a private channel).** Someone in a team channel — or
   an instruction inside a pasted document (prompt injection) — can tell the agent "search your
   kal for X and show it", and the agent **posts it to the channel**. There is no enforcement
   mechanism, so the procedure is the control: **removing kal from the profile before adding the
   agent to a channel** is part of the add procedure.
2. **Results already in context leak without any tool call.** Detaching kal does not retract what
   is already in the conversation. Start a new session instead.
3. **A vault note can carry instructions.** External web clippings end up in the vault, and their
   text reaches the agent through search results. Treat retrieved text as data, never as
   instructions.
4. **A server deployment puts the token and the vault on that host.** Anyone with shell access to
   it has both. Keep the token in a file inside a `700` directory
   (`install -m 600 … ~/.kal/cloud.env`), never in the shell history or a committed config.
   That includes the agent's own shell. A token in an agent's `.env` is loaded into the agent's
   environment, and its shell tools inherit it — Hermes and OpenClaw strip only fixed lists of
   names, which do not include `KAL_CLOUD_TOKEN`. Give the agent that holds the token no shell,
   or a sandboxed one (§Hermes above). In OpenClaw, a tool deny such as `kal__*` hides the MCP
   tools only; a channel agent that can run `exec` still reads the token from the gateway's
   environment, so sandbox it (sandboxed exec starts from an empty environment) or deny
   `group:runtime` for it and keep its file tools in the workspace (`fs.workspaceOnly: true`)
   ([`host-env-security.ts`](https://github.com/openclaw/openclaw/blob/4368865c48/src/infra/host-env-security.ts#L141-L151),
   [`bash-tools.shared.ts`](https://github.com/openclaw/openclaw/blob/4368865c48/src/agents/bash-tools.shared.ts#L49-L66) — openclaw 4368865c48,
   committed 2026-09-25, retrieved 2026-09-26).

**Local stdio**

- `kal_mcp.py` **opens no network listener.** That is a design premise (see the header of
  `src/kal_mcp.py`). If you need remote access, use the hosted version — do not edit that file to
  open a port.
- Do not drop the flags from `.mcp.json` when running under docker — mounting the vault `:ro` and
  cutting the network is the whole point of that file.

**Remote**

- The `<credential>` in the URL form *is* the account. Do not paste it into chats, issues, or
  screenshots.
- If you suspect it leaked, **revoke** it on the app's MCP screen and create a new one. Attached
  clients disconnect immediately.
- That address **does not appear in the server access log** either — the MCP host's log format
  records no path at all ([privacy policy](https://kallimachos.dev/privacy) §1).
