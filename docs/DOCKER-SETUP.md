# Docker setup — from nothing to a running web UI

The container is **a tool running on top of host data**, not a place data lives. The knowledge DB
(`~/.kal`) and your vault stay on the host and are mounted in, because Obsidian and the CLI write
the same files at the same time.

For *why* each piece is shaped this way — the Go/Python split, the relay, the security review —
read [`STACK.md`](STACK.md). This page is the **order to do things in**, and what breaks when you
skip a step.

---

## 1. Once, on the host

```bash
just env                    # writes .env from .env.example, with your uid/gid filled in
just vault /absolute/path   # writes VAULT_DIR and VAULT_NAME, and ~/.kal/config.json
```

`just env` refuses to overwrite an existing `.env`. `just vault` is the only thing that should
write the vault path, because **the path lives in two files** and setting one by hand leaves them
disagreeing with no error at all.

Then check the four values that actually stop the container from starting:

| Key | Why it stops you |
|---|---|
| `UID` / `GID` | must match the host, or the container cannot **write** into the mounted `~/.kal` |
| `VAULT_DIR` | compose fails outright: `VAULT_DIR is not set` |
| `VAULT_NAME` | same — it is what the galaxy view's `obsidian://open?vault=…` deep link uses |
| `KAL_DIR` · `CLAUDE_DIR` | absolute host paths. **`~` is not expanded in `.env`** |

## 2. Start it

```bash
just up          # development — vite HMR, api mounts src/
just up-prod     # production — static build
just up-tls      # a domain with Let's Encrypt (nginx-proxy + acme profiles)

just logs        # follow
just down        # stop
```

`just up` opens **http://127.0.0.1:5173**.

> ⚠ **Loopback, always.** There is no authentication ([`STACK.md`](STACK.md) §7). Binding `0.0.0.0`
> lets anyone on the same network run the pipeline, edit the vault and wipe the DB. The `proxy`
> profile publishes ports too, and it defaults to loopback for the same reason — opening it
> outward (`PROXY_BIND=0.0.0.0`) means putting authentication in front first.

## 3. The relay — LLM steps from inside a container

```bash
just relay       # on the HOST.  Put the URL and token it prints into .env
```

`claude` is not authenticated inside the container. On macOS the credentials live in the keychain,
so mounting `~/.claude` read-only brings the settings but **not the login**. The API checks before
running and blocks with a **412** rather than failing halfway.

The relay takes a prompt and returns text. It never touches the DB, which keeps the container the
single writer of LanceDB.

| | |
|---|---|
| `KAL_CLAUDE_RELAY` | the URL `just relay` prints (usually `http://host.docker.internal:8791`) |
| `KAL_RELAY_TOKEN` | the token it prints. Keep `.env` at `chmod 600` |

**The token is generated if you do not supply one.** Verified: started with `KAL_RELAY_TOKEN`
unset, an unauthenticated `POST /run` gets **401** — there is no window in which the relay serves
without checking. `/health` stays open, because that is what the client uses to read
`inflight_max`.

Two ways to run it:

| | |
|---|---|
| **generated** (default) | `just relay`, copy both printed lines into `.env`. ⚠ Restarting the relay makes a **new** token, and the container's old one then gets 401 |
| **fixed** | put a value in `.env` first, then `KAL_RELAY_TOKEN="$(grep '^KAL_RELAY_TOKEN=' .env \| cut -d= -f2-)" just relay` — it survives restarts |

**Making a fixed token.** The relay uses `secrets.token_urlsafe(24)` — 24 bytes of entropy, 32
characters. Match it:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

Or without Python:

```bash
openssl rand -base64 24 | tr '+/' '-_' | tr -d '=\n'
```

To write one straight into `.env` without it appearing on screen or in your shell history:

```bash
python3 -c "
import re, secrets, pathlib
p = pathlib.Path('.env'); s = p.read_text()
p.write_text(re.sub(r'^KAL_RELAY_TOKEN=.*$',
                    'KAL_RELAY_TOKEN=' + secrets.token_urlsafe(24), s, flags=re.M))
print('  written')
"
```

> ⚠ `KAL_RELAY_TOKEN=` with **no value** is not a fixed token —— it reads as unset, and the relay
> generates a fresh one on every start. The point of the fixed form is that the value is there
> before the relay looks.

The cancel path (`DELETE /job/<id>`) checks the same token, so nobody can kill someone else's run
without it. The comparison is `hmac.compare_digest`, not `==`: a plain comparison leaks how many
characters matched through its timing.

On a Linux host the credentials are a file, and the mount is enough — no relay needed.

---

## 4. Paths are different inside

```
   host                     container            variable
   ~/.kal               →   /data/kal            KAL_HOME
   ~/.kal/db            →   /data/kal/db         KAL_PATH
   <your vault>         →   /vault               KAL_VAULT
   ~/.claude            →   /data/kal/container-home/.claude   (read-only)
                            HOME lives here too, and must be writable
```

> ⚠ A script with a hard-coded host path is **silently wrong** inside the container. Measured: with
> `KAL_VAULT` unset the vault could not be found and all 376 documents were reported as
> **deleted**. Use `KAL_HOME` · `KAL_VAULT` · `KAL_PATH` in anything new.

Two variables exist only to undo the mount's flattening:

- `KAL_VAULT_HOST` — the mount's **source** path. Inside, the vault is only ever `/vault`, so
  comparing against the `meta.vault_path` recorded when the DB was built needs the real one.
  Without it the screen cannot tell you which vault built this DB.
- `KAL_VAULT_NAME` — `/vault`'s basename is not the vault's name, and the Obsidian deep link needs
  the real one.

---

## 5. Changing the vault — the step people miss

The vault path lives in **three** places, and they are updated by three different actions:

```
   ~/.kal/config.json     CLI · MCP          ─┐
   .env  VAULT_DIR        compose            ─┴─  just vault <path>   (writes both)
   the running container's mount             ───  docker compose up -d
```

`just vault` (and `just openwiki-adopt`, which calls it) writes the first two. **The container keeps
its old mount until it is recreated.** Nothing errors; the symptom is *"search finds things the
screen cannot open"* — the DB holds one vault and the UI reads another.

```bash
just openwiki-adopt          # or: just vault <path>
docker compose up -d         # ← recreates api with the new mount
```

Check it:

```bash
docker compose ps
docker inspect $(docker compose ps -q api) --format '{{range .Mounts}}{{.Source}} → {{.Destination}}
{{end}}' | grep /vault
```

---

## 5b. What actually builds the knowledge DB

**Indexing calls no LLM.** That is the whole answer to "what do I need to set" — the DB is built
from Markdown by `schema_v3.py`, and every value it needs is already in `.env`:

| Value | What it is inside the container | Why the build stops without it |
|---|---|---|
| `VAULT_DIR` | mounted at `/vault`, passed as `KAL_VAULT` | with no vault the indexer finds nothing and reports **every** document deleted (the viewer does not need it — [§5c](#5c-what-the-vault-mount-is-actually-for)) |
| `KAL_DIR` | mounted at `/data/kal`, passed as `KAL_HOME` / `KAL_PATH` | LanceDB has nowhere to live |
| `UID` / `GID` | the container's user | it can read the mount but not **write** the DB into it |
| `VAULT_NAME` | `KAL_VAULT_NAME` | compose refuses to start without it (`${VAULT_NAME:?…}`) |

That is enough for **Rebuild knowledge DB** and **Incremental sync** in the UI, and for
`just openwiki-index` on the host. Nothing else is required.

### The steps that do need more

Six of the ten pipeline steps call `claude -p`, and the API blocks them with a **412 before
they start** rather than letting them fail chunk by chunk for half an hour:

| Step | LLM | Writes the DB | Runs in the container? |
|---|:--:|:--:|---|
| Distill sessions | ✅ | | needs the relay |
| Extract knowledge graph | ✅ | | needs the relay |
| Export graph | ✅ | | needs the relay |
| Refresh stale KG · Apply aliases · Full rebuild | ✅ | ✅ | needs the relay |
| Promote to vault | | | rewrites the **vault**, not the DB —— refused on a bundle |
| **Rebuild knowledge DB** | | ✅ | **yes, on its own** |
| **Incremental sync** | | ✅ | **yes, on its own** |
| Verify docs | | | yes |

> ⓘ `openwiki-enrich` calls an LLM too, but it is a **justfile recipe, not a step in
> `src/status.py`** —— the 412 gate does not cover it, and the Settings screen does not
> list it.  Run it on the host.

To make the LLM steps work from the container, add the two relay values — and start the relay on
the **host**, because that is where the credentials are:

```bash
just relay                       # on the host.  It prints both values
# .env
KAL_CLAUDE_RELAY=http://host.docker.internal:8791
KAL_RELAY_TOKEN=<what it printed>
```

> ⚠ On macOS, mounting `~/.claude` read-only brings the settings but **not the login** — the
> credentials are in the keychain. On a Linux host they are a file and the mount is enough.

> ⚠ Running a step on the **host** while `KAL_CLAUDE_RELAY` is set in the environment sends its
> calls to a relay meant for the container. `KAL_CLAUDE_RELAY` belongs in `.env` for compose;
> leave it unset in a host shell.

### Two paths, and what each needs

```
   A  agent sessions   →  distil (LLM · relay)   →  bundle
   B  any Markdown     →  convert (no LLM)       →  bundle
                          fill metadata (LLM)    →  bundle      openwiki-enrich
                                                       │
                                bundle  →  index (no LLM)  →  knowledge DB
                                              │
                                      extract (LLM · relay)  →  entities + relations
```

Only the boxes marked LLM need the relay. **Path B end to end — convert, index, search — runs with
nothing but the four values in the table above.**

## 5c. What the vault mount is actually for

Since 2026-09-02 the galaxy graph is read from **`KAL_HOME`**, not from inside the vault. That was
the api container's only use of the mount — it mounted a whole vault to serve one 6MB file. So:

| Surface | Needs `/vault`? |
|---|---|
| galaxy view · search results · Settings · Paths | **no** |
| the pipeline steps (index · extract · distil · status) | **yes** — they read the Markdown |

The api process no longer reads the vault at all —— the fallback that briefly looked there was
removed on 2026-09-03. A missing graph is a **404 that says to re-export**, not a stale file
served from a second location.

Nothing in `kal-graph.json` justified living in the vault: it is built from LanceDB
(`source: "lancedb"`) and its document references are **vault-relative** — 34,814 references in `entities[].docs[]` and 37,009 in `relations[].docs[]`, spanning
1,116 documents —— none absolute. Only the Obsidian plugin needs a
copy inside a vault, because a plugin cannot open a file outside its own; `export_kal_graph.py`
writes that copy **only when the vault has a `.obsidian/`**, so an openwiki bundle no longer gets a
plugin folder it will never use.

### Running without a vault at all

```bash
just up-viewer      # docker compose -f docker-compose.yml -f docker-compose.viewer.yml up -d
```

**The container needs no vault settings.** `VAULT_DIR` and `VAULT_NAME` still have to *exist*
for Compose to interpolate the base file —— `--env-file /dev/null` alone still fails on them ——
which is why `just up-viewer` supplies throwaways.  Nothing in the container reads them. The recipe supplies throwaway values because Compose interpolates the base
file *before* merging an override, so `${VAULT_DIR:?…}` makes the variable required even where both
the mount and the environment entry are replaced. Dropping the `:?` from the base would take the
loud failure away from the stacks that genuinely need it, and an unset mount source becomes a
silently empty anonymous volume. So the throwaway lives in the one mode that has no vault, and
`KAL_VAULT`, `KAL_VAULT_HOST` and `KAL_VAULT_NAME` all resolve to `""` in the container.

No `/vault` mount. Measured on the running stack: `/api/health` · `/api/steps` · `/api/config` ·
`/api/status` all 200, and the galaxy view works because the graph is read from `KAL_HOME`.

What you give up: the Settings screen's **Run** buttons. They still appear and will fail — the step
has no Markdown to read. Run the pipeline on the host (`just openwiki`, `just openwiki-kg`), which
on macOS is the only place its LLM steps work anyway.

The Status screen then reports:

```
  vault_absent: true · deleted: 0 · indexed: 1115
  → vault · "the index holds 1115 documents but the vault yielded none — it is empty,
             not mounted, or VAULT_DIR points elsewhere.  This is not 'everything was
             deleted', and running sync would empty the index"
```

> ⚠ Before that message existed this configuration printed **`deleted: 1115`** and advised running
> sync — which would have rebuilt the index down to nothing. The honest reading is what makes a
> vault-less container safe to offer.

> ⓘ **In the default stack no `.env` value became removable.** `VAULT_DIR` mounts the Markdown the pipeline reads;
> `KAL_VAULT_NAME` still fills the `obsidian://open?vault=…` deep link; `KAL_VAULT_HOST` is still
> what the Paths screen compares against the DB. What changed is a *dependency*, not a *setting* —
> a container that only serves the viewer no longer touches the vault at run time.

## 6. Keeping notes off the machine

Two different gates, both read from `.env`:

| | Scope | Effect |
|---|---|---|
| `no_llm: true` in a note's frontmatter | one document | indexed and searchable locally, never sent |
| `KAL_NO_LLM=Private:work/Finance` | path fragments, colon-separated | same, for whole paths |
| `KAL_SKIP=…` | folders, vault-relative | left out of **indexing** as well |

> ⚠ `KAL_SKIP` is a **transmission boundary**. A note in a folder that is not listed goes off the
> machine through `claude -p` during extraction. `.git` · `.obsidian` · `node_modules` and the like
> are excluded automatically.

---

## 7. When it does not work

| Symptom | Cause | Fix |
|---|---|---|
| `VAULT_DIR is not set` on `up` | no `.env`, or the key is missing | `just env`, then `just vault <path>` |
| container starts, cannot write `~/.kal` | `UID`/`GID` do not match the host | `echo "UID=$(id -u)" >> .env`, same for `GID`, `docker compose up -d` |
| every document shows as **deleted** | `KAL_VAULT` not reaching the process | it is set in compose; a script bypassing it is the bug ([§4](#4-paths-are-different-inside)) |
| search finds documents the screen cannot open | the mount is older than `VAULT_DIR` | `docker compose up -d` ([§5](#5-changing-the-vault--the-step-people-miss)) |
| LLM steps return **412** | no relay, or the wrong token | `just relay` on the host, copy both values into `.env` |
| indexing works but extraction does not | that is by design —— indexing needs no LLM, extraction does | see [§5b](#5b-what-actually-builds-the-knowledge-db) |
| `api` never becomes healthy | the health check is `GET /api/health` | `just logs api` — it is usually a mount permission |
| the page loads but the API 403s behind a domain | `DOMAIN` is not in the Host allowlist | set `DOMAIN` in `.env`, `docker compose up -d` |
| a `~` in `.env` | the shell does not expand it there | write the path absolute |

---

## See also

- [`STACK.md`](STACK.md) — what runs where, the relay's design, the security review and what remains
- [`OPENWIKI-PIPELINE.md`](OPENWIKI-PIPELINE.md) — the pipeline whose `openwiki-adopt` step needs §5
- `docker-compose.yml` — every decision above is commented at the line that makes it
