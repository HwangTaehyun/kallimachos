# Design — Docker-first development · agent plugin distribution

> **Status: Round 1 revision** (deep-review 8-person panel, 9 blockers and 17 majors reflected).
> Two goals — ① make development run in containers by default, ② ship as a plugin that installs
> easily into Claude Code and Codex.
>
> Of the draft's 5 unverified host-API claims, **4 were resolved against primary sources** and the
> remaining one was resolved **by measurement** after building it. Remaining debt is in §7.

---

## 1. State before the changes (measured 2026-08-23)

> Below is the state at the **start** of this review. Some of the actions in §5 and §6 have since
> been applied — what actually changed is in §9.

```
docker-compose.yml            api · web (+ proxy · acme — behind profiles)
docker-compose.override.yml   dev: web=vite HMR · api mounts src/
docker-compose.kal.yml        pipeline and MCP standalone (image: kal:local)
Dockerfile                    ★ the MCP image.  ENTRYPOINT python · CMD src/kal_mcp.py
api/Dockerfile                Go api + /opt/venv (torch cpu)

images  kallimachos-api 2.52GB · kallimachos-web 283MB · kal:local 2.26GB
skills  skills/kal-recall/SKILL.md                              1
        ~/.claude/skills/kal-search/SKILL.md   ← **a second installed surface** (docs/kal-search-skill.md)
manifest .claude-plugin/plugin.json  name·version·description·author·license·homepage
MCP config .mcp.json                 command: ${KAL_PYTHON:-python3}
        README.md:690-699            ← a docker-form mcpServers example **already exists**
```

### 1.1 The container MCP does actually start (measured 2026-08-23)

The draft said "already exists: the MCP image", but **`kal:local` had never been built** up to
that point. It was built and measured:

```
docker build -t kal:local .                          → 2.26GB · about 3 minutes
docker run --rm -i -v ~/.kal:/data:ro -v <vault>:/vault:ro kal:local
  initialize                                          →  2s · ok
  tools/list                                          →  5 (search·entity·timeline·neighbors·doc)
  first tools/call including model load               → 11s
  stdout contamination                                →  0 lines (stays JSON-RPC only)
  stderr                                              → progress output only
```

> ⚠ **A probing trap** — piping a file gives stdin an immediate EOF, so the server answers only
> `initialize` and exits. The unanswered `tools/list` was nearly misdiagnosed as a product
> defect. Always measure with stdin held open:
> `{ cat probe.jsonl; sleep 25; } | docker run -i …`

**Remaining defect**: `serverInfo.version` is an **empty string**. The host cannot read the
version.

### 1.2 The two images follow different container path conventions

| Image | `KAL_HOME` | `KAL_PATH` | Who follows it |
|---|---|---|---|
| **kal** (root `Dockerfile`) | `/data` | `/data/db` | `docker-compose.kal.yml` · README:698 · `VOLUME ["/data"]` |
| **api** (`api/Dockerfile`) | `/data/kal` | `/data/kal/db` | `docker-compose.yml` · `docs/STACK.md` §73-86 |

Each is **internally consistent**. What ships MCP is the **kal image**, so the canonical form is
`/data` · `/data/db`. The draft's §B1 applied the api convention to the kal image.

## 2. The problem — why "installs easily" does not hold

`.mcp.json` looks like this:

```json
"command": "${KAL_PYTHON:-python3}",
"args": ["${CLAUDE_PLUGIN_ROOT}/src/kal_mcp.py"]
```

What the installer **must already have**:

| # | Prerequisite | Solved by containers? |
|---|---|---|
| 1 | python 3.11+ | ✅ |
| 2 | torch · sentence-transformers · lancedb (venv 1,389MB — `du -sm ~/.kal/venv`, 2026-08-23) | ✅ |
| 3 | embedding model weights | ✅ (baked into the image) |
| 4 | **an indexed LanceDB** | ❌ **no** — see §B0 |

**Three of four go away.** The fourth does not — containers do not create your data.

Two more things:

- The `python3` default is **almost always wrong** (the system python has no lancedb).
  And the failure mode is bad — an unset variable is not a load failure but a **literal
  pass-through** (evidence in §6), so it silently starts on the wrong path.
- The `KAL_VAULT` default has a personal absolute path baked in
  (`${HOME}/github/HwangTaehyun/super-brain`).

## 3. Docker-first development

### 3.1 The edit→check loop stays on mounts

| What | How | Loop |
|---|---|---|
| Python | bind-mount `src/` (override) | immediate |
| Front end | mount `web/` + vite HMR | immediate |
| **Go** | the container binary is the `gobuild` output of `api/Dockerfile` | **`just up` (= `--build`)** |

> ⚠ `just build-api` drops into `/tmp/sb-api` for **host-side verification** (gofmt · vet ·
> test). Running only that leaves the container on the old binary.

### 3.2 Open the path to running inside the container

```
just up            dev stack
just sh            a container shell
just dc <recipe>   the same recipe, in the container   (e.g. just dc index)
just dc selftest   ★ self-checks in the container — aligns where you fix with where you verify
```

All 9 scripts under `just selftest` go through the host `{{py}}`. If you fix in the container but
verify on the host, developers drift back to the host.

### 3.3 The host prerequisite branches from one place

```
justfile:12   py := env_var_or_default("KAL_PYTHON", $HOME + "/.kal/venv/bin/python")
                ↑ **the root.** status · selftest · sync · refresh · export · rebuild · vacuum and 20+ recipes all pass through here
justfile:34   just setup — builds the host venv
justfile:270  creates .env
.env.example:9,10,14  personal absolute paths (`/Users/<you>/…`)
src/*.py      ~30 occurrences of expanduser("~/.kal")
```

**It is not "3 remaining prerequisites"** — it is every pipeline recipe. That is exactly why the
fact that a single `KAL_PYTHON` swaps them all matters.

### 3.4 `just setup` currently blocks a docker-only entry

`justfile:39` exits 1 without python3, and `justfile:66`'s
`import lancedb, sentence_transformers` sits under `set -euo pipefail`, so failing kills the
recipe. The README presents this as **the first step, 64 seconds** — meaning a new user
**must first** build the very venv §2 identified as the problem.

**To fix**: drop the hard python3 failure, provide a docker-only path that ends at `just env`,
and split the README's first step in two. The cost is not "one more recipe" but **testing both
paths**.

### 3.5 Update the scope statement

`justfile:7-8` says "containers are for the web UI, not a prerequisite of the pipeline". The
moment the default MCP path becomes docker, containers become **a prerequisite of a third thing
(MCP distribution)** and that sentence becomes false. The update step is in §6.

## 4. First run (§B0) — without it, installation is broken

Calling a tool against an empty DB **does not return empty; it dies**:

```
KAL_PATH=<empty path> python src/kal_mcp.py
  → ValueError: Table 'chunks' was not found      (src/kal_mcp.py:75, no guard)
```

`docker run` does not hand you an indexed DB. So **bootstrapping has to be part of the install
procedure**:

```bash
# ① the image (outside the handshake — Codex cannot do it within 10 seconds, §5.3)
docker pull ghcr.io/hwangtaehyun/kal@sha256:<digest>

# ② index the vault   (as far as the stages that run without an LLM)
docker run --rm -v ~/.kal:/data -v <vault>:/vault:ro \
  ghcr.io/hwangtaehyun/kal@sha256:<digest> src/schema_v3.py

# ③ verify
docker run --rm -i -v ~/.kal:/data:ro -v <vault>:/vault:ro \
  ghcr.io/hwangtaehyun/kal@sha256:<digest> --selftest
```

**Fix it in code too** — put a guard in `tbl()` that turns an empty DB into the sentence "run
indexing first". A traceback reads as an installation failure and the user has no idea what to do.

## 5. Plugin distribution

### 5.1 MCP in a container (hardened form) — **applied**

This is the actual content of `.mcp.json` (copied verbatim so the docs do not run ahead of the
file):

```json
{
  "mcpServers": {
    "kal": {
      "type": "stdio",
      "command": "docker",
      "args": ["run", "--rm", "-i",
               "--network", "none", "--read-only",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--tmpfs", "/tmp",
               "--user", "${user_config.run_as}",
               "-v", "${user_config.kal_dir}/db:/data/db:ro",
               "-v", "${user_config.vault_dir}:/vault:ro",
               "ghcr.io/hwangtaehyun/kal:0.1.0"]
    }
  }
}
```

| What | Why | Evidence |
|---|---|---|
| path `/data/db` | the kal image convention (§1.2). Uses the image defaults with no `-e` | measured image env |
| `db` only, `:ro` | giving all of `~/.kal` as rw lets the image overwrite `venv/bin/*`, which is **host code execution** | MCP only opens tables |
| `--network none` | no HTTP client · weights baked in · offline enforced — and yet it was attached to the default bridge | search succeeded with it on, measured |
| **`--tmpfs /tmp`** | **without it lance panics** — `LocalSpillStore: ReadOnlyFilesystem`. The user sees only `rust future panicked: unknown error` | reproduced by removing it |
| `--read-only` + 3 others | least privilege | verified working with them on |
| `--user ${user_config.run_as}` | the image is uid 1000, the host is 501. macOS papers over it but **Linux gives EACCES** | uid measured |

**`VOLUME ["/data"]` was removed from the Dockerfile.** `--read-only` **does not cover a declared
VOLUME** — an anonymous volume mounts rw and whatever is written there disappears silently with
`--rm`. Measured: `touch /data/probe` succeeded, `dd 20MB` succeeded. Writes confirmed blocked
after removal.

**`${user_config.*}` substitution was settled by measurement** (Claude Code 2.1.241, reproduced
with an isolated config):

| Manifest state | In `args` |
|---|---|
| declared + `default` | substituted (the default) |
| declared + user value | substituted (the user value) |
| `required:true` · unset | **the server does not start at all** ← safe |
| `required:false` · no default · unset | **empty string** → `/db:/data/db:ro` ← silent misbehaviour |
| key not declared (typo) | the server does not start |
| no `userConfig` block at all | **literal pass-through** ← the worst |

The fourth row is the dangerous one. `plugin_probe.userconfig_safety()` enforces "every key
referenced is either required or has a default".

> **Pre-release blocker**: `:0.1.0` is a mutable tag. It becomes `@sha256:` the moment it goes to
> GHCR. The reason it is a tag today is **that the image does not exist yet, so there is no
> digest** (§7 #3). `plugin_probe.manifest_drift()` prevents the tag and the `plugin.json`
> version from diverging, and skips that check for a digest pin.

### 5.2 Claude Code plugin

`plugin.json` can carry `mcpServers`, and it is **merged** with `.mcp.json`.
`skills` · `commands` · `agents` · `hooks` can be declared too, and **only `name` is required**
([plugins reference](https://code.claude.com/docs/en/plugins-reference) — continuously updated, retrieved 2026-08-23).

- Skills are **auto-discovered** at `skills/<name>/SKILL.md`.
- **Without** a frontmatter `name` it falls back to the install directory name, and for a
  marketplace install that is a **version string**, so the invocation name changes on every
  update. New skills must always declare `name`.
- Marketplace: `.claude-plugin/marketplace.json` (`name` · `owner` · `plugins[]`).

### 5.3 Codex

The `[mcp_servers.<id>]` table in `~/.codex/config.toml`
([Codex config reference](https://learn.chatgpt.com/docs/config-file/config-reference) — continuously updated, retrieved 2026-08-23):

```toml
[mcp_servers.kal]
command = "docker"
args = ["run", "--rm", "-i", "--network", "none", "--read-only",
        "-v", "/Users/me/.kal/db:/data/db:ro", "-v", "/Users/me/notes:/vault:ro",
        "ghcr.io/hwangtaehyun/kal@sha256:<digest>"]
startup_timeout_sec = 120     # ★ the 10-second default is not enough
```

> **`startup_timeout_sec` defaults to 10 seconds.** A measured warm start passes at 2 seconds,
> but **a pull happening inside the handshake will certainly exceed it.** That is why the
> `docker pull` in §4 ① is an installation prerequisite.

**Codex is not MCP-only** — it reads `SKILL.md` skills from `$HOME/.agents/skills`,
`$CWD/.agents/skills`, `$REPO_ROOT/.agents/skills`, and `/etc/codex/skills`
([Build skills](https://learn.chatgpt.com/docs/build-skills.md) — continuously updated, retrieved 2026-08-23).
The required frontmatter is `name` + `description` — **the same shape as ours**.

> ⚠ The widely-circulated blog guidance saying `~/.codex/skills` is **wrong.** The official path
> is `.agents/skills`. Copying it wrongly produces silent no-op.

### 5.4 Skills — how they are delivered

**Skills have to live in the plugin directory (host side), not in the image.**
`Dockerfile:48` puts them in the image with `COPY skills/ ./skills/`, but the host running
`docker run` has no way to read that. The copy inside the image only matters when running the
pipeline in the container.

```
Claude Code   <plugin root>/skills/<name>/SKILL.md      auto-discovered
Codex         ~/.agents/skills/<name>/SKILL.md          copy or symlink
```

| Skill | What | Status |
|---|---|---|
| `kal-recall` | search and recall (the selection rules for the 5 tools) | exists |
| `kal-ingest` | outside documents into the vault | new |
| `kal-maintain` | pipeline status and refresh | new |

`Assumption —` one skill carrying the **selection rules** for several tools is better than one
skill per tool. The evidence is the single `kal-recall` case, with no independent verification.
Coverage is 5/5, but that does not mean "search is sufficient" — that would be a retrieval-quality
judgement.

**To clean up**: `~/.claude/skills/kal-search/` is alive as a **second installed surface**
(`docs/kal-search-skill.md`). The triggers overlap, so it has to be retired or migrated.

## 6. Steps

| # | What | Done when |
|---|---|---|
| 0 | ~~build and confirm the kal image starts~~ | ✅ **done** (§1.1) |
| 1 | empty-DB guard in `tbl()` + fill `serverInfo.version` | `--selftest` fails with a sentence on an empty DB |
| 2 | multi-arch build and push, `buildx --platform linux/amd64,linux/arm64` | both arch manifests present |
| 3 | replace `.mcp.json` with §5.1 (keep the host-python form as a comment) | kal healthy in `claude mcp list` |
| 4 | declare `mcpServers` and `skills` in `plugin.json` + `marketplace.json` | local install succeeds |
| 5 | Codex `config.toml` example + `.agents/skills` distribution | 5 tools in Codex |
| 6 | add 2 skills · retire `kal-search` | frontmatter `name` present |
| 7 | `just dc selftest` · docker-only `just setup` path | self-checks pass in the container |
| 8 | README install section · update the `justfile:7-8` scope statement | — |
| 9 | ~~`just plugin-test`~~ → **`just mcp-plugin-test`** — a real `docker run -i` handshake | ✅ **done** (§9) |
| 10 | new `.github/workflows/` — build image → `just mcp-plugin-test` | the workflow is green |
| 11 | **security debt 4** — an empty `no_llm` set passes unconditionally. First-run warning + `KAL_NO_LLM` | removed from the debt list |
| 12 | **security debt 5** — `SKIP` is a substring of a folder name. It induces self-exclusion on the install path | excluded even when cloned under a different name |
| 13 | **security debt 6** — make the `CLAUDE_DIR` mount opt-in (state that it is dev-compose only) | reflected in docs and defaults |

**Version rule**: semver. `plugin.json`'s `version` == the image tag == the git tag.
`plugin.json` says `0.1.0` today while there is **no git tag at all**.

**Upgrade and removal**: `meta.schema_version` is written by `schema_v3.py:875` but read only by
`status.py:203` and the web display — **nobody enforces it.** Attaching a new image to an old DB
diverges silently. A startup gate goes in.

## 7. Remaining debt

| # | What | Why still open | Status |
|---|---|---|---|
| 1 | ~~nested `${A:-${B}}` in `.mcp.json`~~ | that syntax is no longer used | **resolved (moot)** |
| 1' | whether `${user_config.*}` substitutes inside `args` | — | ✅ **settled by measurement** (§5.1 table) |
| 2 | cold start (including pull), measured | can only be measured once it is on a registry | open |
| 3 | the `ghcr.io/hwangtaehyun/kal` repository does not exist | has to be created. `kal_mcp.py`'s guidance text **already names this image to users** | open |
| 4 | the `no_llm` gate ships effectively disabled | 0 documents in the vault have `no_llm: true` → `llm_gate` always passes (`schema_v3.py:760`) | **§6 step 11** |
| 5 | `SKIP` is coupled to this machine's folder name | substring matching (`schema_v3.py:197` — `SKIP_ANY` is at `:85`). Cloning as `kal/` gets it indexed and sent | **§6 step 12** |
| 6 | the `CLAUDE_DIR` mount is credentials on Linux | currently **dev-compose only** — `.mcp.json` does not mount it | **§6 step 13** |
| 7 | stdout contamination from `docker run` itself | 0 lines in our environment. Other docker versions and platforms unverified | open |
| 8 | `required` prompt behaviour when loaded via `--plugin-dir` | in headless `-p` it **silently does not start the server** (measured). Interactive unverified | open |
| 9 | personal absolute paths remain as defaults in 10 places in the source | `kal_mcp.py:44` · `kal_config.py:248` · `lr_extract.py:33` … harmless in a container but leaks the author's layout to host users | open |

## 8. Judgements deliberately left unverified

- The skill decomposition (§5.4) generalizes from one case.
- `requirements.txt` is **not stale** — checked against PyPI, 6 of 8 are current and only
  `sentence-transformers` had a major release 5 days ago. The header comment's deliberate pin is
  correct.

## 9. What actually changed (2026-08-23)

| File | What | Verification |
|---|---|---|
| `src/kal_mcp.py` | empty DB → a `NotIndexed` guidance sentence · `MCPServer(version=)` | measured inside the container |
| `src/plugin_probe.py` (new) | acceptance test — a real stdio handshake · manifest drift · userConfig safety | 7 self-checks |
| `src/_dcpy.sh` (new) | the container python stand-in for `just dc` | `just dc status` measured |
| `justfile` | `KAL_SRC` override for `src` · `dc` · `sh` · `build-kal` · `mcp-plugin-test` | `just dc status` works |
| `Dockerfile` | removed `VOLUME ["/data"]` | write blocking measured |
| `.mcp.json` | docker + 6 hardening flags + `${user_config.*}` | `plugin validate` · probe passes |
| `.claude-plugin/plugin.json` | 3 `userConfig` entries · `keywords` · `repository` | mutations 4/4 |
| `docker-compose.kal.yml` | named volume → host bind | compose config passes |

`just selftest` 27 → **36**.
