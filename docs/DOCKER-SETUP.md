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
| `api` never becomes healthy | the health check is `GET /api/health` | `just logs api` — it is usually a mount permission |
| the page loads but the API 403s behind a domain | `DOMAIN` is not in the Host allowlist | set `DOMAIN` in `.env`, `docker compose up -d` |
| a `~` in `.env` | the shell does not expand it there | write the path absolute |

---

## See also

- [`STACK.md`](STACK.md) — what runs where, the relay's design, the security review and what remains
- [`OPENWIKI-PIPELINE.md`](OPENWIKI-PIPELINE.md) — the pipeline whose `openwiki-adopt` step needs §5
- `docker-compose.yml` — every decision above is commented at the line that makes it
