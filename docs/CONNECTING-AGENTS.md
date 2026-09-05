# Connecting to agent runtimes — Buzz · Hermes · others

kal is an **MCP server**. It exposes five tools, all **read-only**:

| Tool | What |
|---|---|
| `kal_search` | natural-language exploration (the main entry point) |
| `kal_entity` | what a known name is + its sources + a change summary |
| `kal_timeline` | the whole history of changes |
| `kal_neighbors` | one hop in the graph |
| `kal_doc` | citation verification — the source text |

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

### Local stdio (self-hosted · free)

Carry over the hardening flags from the repository root's [`.mcp.json`](../.mcp.json):

```yaml
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
      "ghcr.io/hwangtaehyun/kal:0.1.0",
    ]
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

Hermes supports headers, so use **Bearer instead of the URL form**:

```yaml
mcp_servers:
  kal-remote:
    url: "https://mcp.kallimachos.dev/mcp"
    headers:
      Authorization: "Bearer ${env:KAL_CLOUD_TOKEN}"
    tools:
      include: [kal_search, kal_doc, kal_entity, kal_neighbors, kal_timeline]
```

Put `KAL_CLOUD_TOKEN` in the profile's `.env` (never commit it — `.gitignore`, `chmod 600`).
The default transport is Streamable HTTP, so no `transport` key is needed.

To keep both local and remote, split the keys into `kal-local` and `kal-remote` (reusing one key
silently overwrites the other) but **do not enable both at once** — the same five tool names
appear from both sides and there is no guarantee which the agent calls. Leave one at
`enabled: false` and flip that single line to switch.

### Project instructions

Writing the usage into the project instructions file (`AGENTS.md`, or `~/.hermes/SOUL.md`
globally) tells the agent when to reach for the tools:

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
| Hermes | §Hermes above, unchanged. Buzz Desktop shows it **automatically** under Settings → Runtimes if Hermes is installed. On the server side, the Buzz channel connection (relay bridge) links it via `hermes acp` (stdio) — **if you plan to run this on a server, read the host axis in the security section first.** ⚠ If `HERMES_ACP_SKIP_CONFIGURED_MCP=1` is set, kal in config.yaml is silently skipped ([ACP Host Integration](https://hermes-agent.nousresearch.com/docs/user-guide/features/acp) — retrieved 2026-08-31) |
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

```bash
just mcp-test                     # actually calls all five tools (needs a real vault — kal_doc reads a document)
claude mcp list | grep kal        # on Claude Code → ✔ Connected
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

If you are a Buzz teammate and the tools do not appear, check
`HERMES_ACP_SKIP_CONFIGURED_MCP` first (§Buzz-A).

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
