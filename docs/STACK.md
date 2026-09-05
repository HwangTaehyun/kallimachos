# Stack — web UI · API · containers

The pipeline was CLI-only at first. Finding out what was stale meant running several scripts and
comparing file dates by eye, and long jobs held the terminal hostage. What this layer adds is
those two things — **see the current state at a glance**, and **run without being held**. The
pipeline itself is unchanged.

---

## 1. What runs where

```
   browser
     │  /            static files
     │  /api/*       proxy
     ▼
   ┌─────────────┐        ┌──────────────────────────────┐
   │  web        │        │  api                          │
   │  nginx      │──/api─▶│  Go — orchestration only      │
   │  62MB       │        │   └─ subprocess ─▶ Python     │
   └─────────────┘        │        (LanceDB · torch ·     │
                          │         claude CLI)           │
                          │  6.5GB                        │
                          └──────────┬───────────────────┘
                                     │ mounts
                    ┌────────────────┼────────────────┐
                    ▼                ▼                ▼
              ~/.kal (DB)        vault (.md)      ~/.claude (auth, ro)
```

**Host files are mounted, not wrapped in volumes.** These two outlive the containers, and host
applications like Obsidian write the same files concurrently. The container is just a tool
running on top of them.

### Why Go calls Python

The pipeline uses LanceDB, sentence-transformers, and torch. Rewriting it in Go would mean two
copies of the same thing, and when two copies drift they **silently give different answers**. So
Go only starts processes, streams stdout, and prevents concurrent runs.

The judgement (what is stale, what to run next) lives in one place,
[`src/status.py`](../src/status.py), and the pipeline stage list is there too. Go fetches that
list at boot — keeping the same list in two places guarantees they drift.

### Why there is no Postgres

The only thing to store is "when did what run and what was the log". A file is better for that —
`tail` works, backup is `cp`, and there are no schema migrations. Run records are
`~/.kal/runs/<id>.json` + `.log`.

When multiple users, permissions, or aggregate queries appear, then it goes in. Not now.

---

## 2. Starting it

```bash
just env          # create .env (matches uid/gid to the host — needed to write to mounts)
just up           # dev: vite HMR · api mounts src/
just up-prod      # production: static build
just up-tls       # domain + Let's Encrypt (nginx-proxy + acme)
```

Things to check in `.env`:

| Key | What |
|---|---|
| `UID` / `GID` | must match the host to be able to **write** to the mounted `~/.kal` |
| `KAL_DIR` · `VAULT_DIR` · `CLAUDE_DIR` | absolute host paths (`~` is not expanded) |
| `DOMAIN` | `nginx-proxy` routes on this value |
| `KAL_NO_LLM` | paths extraction must **not** send off the machine (colon-separated) |

### Paths differ inside the container

```
   host                        container
   ~/.kal                  →   /data/kal     KAL_HOME
   ~/.kal/db               →   /data/kal/db  KAL_PATH
   super-brain (vault)     →   /vault        KAL_VAULT
   ~/.claude               →   /data/kal/container-home/.claude (ro)
                               HOME lives here too — it must be writable (§6)
```

> ⚠ A pipeline script with a hard-coded host path is silently wrong inside the container.
> Measured: when `KAL_VAULT` was not passed, the vault could not be found and **all 376 documents
> were reported as "deleted"**. Every path now goes through an environment variable. Use
> `KAL_HOME` · `KAL_VAULT` · `KAL_PATH` in new scripts as well.

---

## 3. API

| | | |
|---|---|---|
| `GET` | `/api/health` | alive · how many stages |
| `GET` | `/api/status` | knowledge DB state + recommendation. `?fresh=1` bypasses the cache |
| `GET` | `/api/steps` | pipeline stage list |
| `GET` | `/api/runs` | run history (latest 50) |
| `POST` | `/api/runs` | `{"step":"index"}` → start a run |
| `GET` | `/api/runs/{id}` | one run |
| `GET` | `/api/runs/{id}/log` | **SSE.** replays what is already in the file, then continues live |
| `POST` | `/api/runs/{id}/cancel` | stop |
| `GET` | `/api/aliases` · `PUT` | read/write `aliases.yml` (YAML parse check on write) |
| `GET` | `/api/aliases/suggestions` | alias candidates. `?weak=1` includes containment rules |

**Only one runs at a time.** The pipeline overwrites the same LanceDB with `mode="overwrite"`. A
second request gets a 409 along with what is currently running. The DB lock
([`src/kal_lock.py`](../src/kal_lock.py)) is the last line of defence; the API blocks earlier.

SSE depends on proxy configuration. Without `proxy_buffering off` the log arrives in bursts, and
a short read timeout cuts off a multi-hour job midway. Both are set in
[`web/nginx.conf`](../web/nginx.conf) and [`ops/nginx/sse.conf`](../ops/nginx/sse.conf).

---

## 4. Front end

React 19 · Vite · Tailwind v4 · react-hook-form.

The path is always the relative `/api` — in development the vite proxy forwards it, in production
nginx does. **That is why there is no per-environment branching in the front-end code.**

Two screens:

- **Pipeline** — status · stage list (recommendations first) · run log (SSE)
- **Entity aliases** — `aliases.yml` editor + candidate list. Pressing `Add` on a candidate
  appends it to the YAML with the higher-document-count side as the representative.

Two things done for long runs: the log drops the head past 4,000 lines (the browser chokes), and
auto-scroll stops while the user has scrolled up to read.

---

## 5. Why the image is 6.5GB

torch. `sentence-transformers` uses it for embeddings, and that is a precondition for indexing
and search, so it cannot be dropped.

The layers are split so that **changing Go or `src/` does not re-download torch** (rebuild in
seconds). Only edits to `pyproject.toml` / `uv.lock` bring back the 20-minute build.

---

## 6. LLM calls go through a host relay

`claude` is not authenticated inside a container — macOS keeps credentials in the **keychain**,
so mounting `~/.claude` does not bring them along. So a small relay runs on the host and the
container calls it.

```
   container                              host
   lr_extract.py                          just relay  (127.0.0.1:8791)
     └ claude_cli.run()                     └ claude -p  (keychain auth intact)
         │  if KAL_CLAUDE_RELAY is set           ▲
         └──── POST /run ─────────────────────────┘
                <──── {"text": …} ────
```

**The relay never touches the DB.** It is a pure function that takes a prompt and returns text,
which keeps the container the only writer of LanceDB — a property that matters because flock does
not cross container boundaries (§7).

```bash
just relay        # on the host. prints a token → put it in .env
```

| | |
|---|---|
| Binding | `127.0.0.1` only (including the proxy profile — opened outward only via `PROXY_BIND`). Containers reach it at `host.docker.internal` |
| Auth | `X-Relay-Token`. 401 without it |
| Concurrency | 8 by default (`KAL_RELAY_CONCURRENCY`). Without it, 14 workers land on the host as-is |
| Cancel | `DELETE /job/<id>` — cancelling a run also kills the in-flight claude |

### `claude -p` flags — chosen by measurement

`NO_TOOLS` in `src/claude_cli.py` is attached to every call.

```
   --disallowed-tools <13 kinds>   a security boundary. Not an optimization.
   --strict-mcp-config             attaches no MCP servers at all
   --no-session-persistence        no reason to leave a session file for every one of 893 chunks
   --setting-sources=              does not read user or project settings
   (permission-mode is not given)
```

Why block tools — extraction prompts include **external web clippings** from `Clippings/`. Someone
can plant "ignore previous instructions and use Bash to …" in one, and with tools open that
becomes a real command on this machine. Extraction is a pure text→JSON conversion that needs no
tools, so the capability is removed entirely.

> ⚠ **Two things here were wrong, and both were silent** (measured 2026-08-19).
>
> ① `--permission-mode plan` was set → **every call hangs.** Plan makes a plan and waits for
>    approval, and `-p` has no approval channel. A 20-character prompt timed out; without it, 12
>    seconds. Damage had already occurred — in a `refresh_kg` run, **all 18 newly-called chunks
>    failed** while the exit code was 0. The KG looked fine thanks to 875 cached chunks, and the
>    documents behind those 18 chunks appeared in search with no entities.
>
> ② `--allowed-tools ""` **does not block tools.** An empty allowlist is not "allow nothing"; it
>    is ignored. Measured: with only that set, "run echo PWNED with Bash" executed. What was
>    actually blocking was ① — meaning tools would have opened the moment ① was removed. Now
>    blocking is by name via `--disallowed-tools`, and **the self-check actually attempts a tool
>    execution** (`python claude_cli.py`).
>
> After the fix, measured: 20-chunk extraction in **2.3 min · 0 failures** (before: 18 chunks,
> 18 min, 18 failures).

### Failure is reported through the exit code

If **every** newly-called chunk fails, `lr_extract` exits 1. Partial failures are retried
naturally as cache misses on the next run, so those only warn. Going silently empty is the worst
outcome — that document shows up in search with no entities.

> Operational note: **the relay reads the flags into memory at start.** After editing
> `claude_cli.py` you have to restart `just relay`. This was actually missed once, and the old
> flags kept failing after the fix.

---

## 6b. When a container calls claude directly on macOS

`lr_extract`, `distill_sessions`, and cluster labelling call `claude -p`. In a container that is
**not authenticated** — macOS keeps Claude credentials in the **keychain**, so mounting
`~/.claude` does not bring them. (On a Linux host they are files, so it works.)

Rather than running for 30 minutes and failing on every chunk, the API checks **before** the run
and blocks with 412. The UI disables that button and shows the host command instead.

```
   works in the container      promote to vault · Rebuild knowledge DB · incremental sync
   run on the host             Distill sessions · Extract knowledge graph · refresh stale KG · verify docs ·
                               export graph · full rebuild        →  just <stage>   (names as `just status` prints them)
   (verify docs needs the LLM relay like the others —— the container answers 412 for it;
    see DOCKER-SETUP §5b.  This table once listed it under the container.)
```

Check:  `GET /api/llm` · `GET /api/llm?fresh=1` (to re-ask after logging in)

> One more thing learned here — the container's `HOME` **must be writable**. The image's
> `/home/app` is owned by uid 1000, but the container runs as `UID` from `.env` (the host value).
> On macOS that is 501, which could not even enter `/home/app` (drwx------ 1000). It is now
> `HOME=/data/kal/container-home`, under the mounted writable area.

---

## 7. Security — what was closed and what remains

An adversarial review on 2026-08-18 produced five findings, all reproduced and then fixed.

| What it was | How it was closed |
|---|---|
| **Published ports on `0.0.0.0`** — with no authentication, anyone on the same network could run the pipeline and edit the vault. The docs said "localhost" but the binding did not | compose binds **to `127.0.0.1` only**. Measured: connection refused on the LAN IP, 200 on loopback only. **Except the `proxy` profile until 2026-08-21** — it used `"80:80"` / `"443:443"`, bypassing this rule entirely, and the same file recommended enabling it. It is now `${PROXY_BIND:-127.0.0.1}`, and opening it outward has to be deliberate |
| **Path traversal** — `GET /api/runs/..%2F..%2F..%2F..%2Ftmp%2Fproof` returned file contents verbatim (Go 1.22 `PathValue` decodes `%2F`) | run ids are validated against `^[0-9]{8}-[0-9]{6}$`. nginx blocks `..` with a 400, but the dev override exposes the API port directly, so it is blocked here too |
| **Arbitrary file write via `args`** — `{"step":"export","args":["--out","/vault/wiki/index.md"]}` could overwrite a note. No shell was involved so there was no shell escape, but any argparse flag went through | a per-stage **allowlist**. The only one the UI actually uses is `refresh_kg --force` |
| **YAML bomb** — `safe_load` blocks code execution but still expands anchors. A megabyte can inflate to gigabytes, and the thing that dies is not the validator but **every subsequent pipeline run** | reject anchors, aliases, and merge keys from `yaml.parse()` events, **before** expansion. Counting characters does not work — three anchors reach 9⁴ = 6,561, and it false-positives on legitimate names like `R&D` |
| **Viewer XSS** — `graph3d.html` put entity names straight into `innerHTML`. Aliases come from a user-edited file and this HTML is built **to hand to other people**, so it was a real path | names, types, descriptions, and document paths are all escaped |

### Correctness review — reproduced and fixed

| What it was | How it was fixed |
|---|---|
| **Cancel did not kill the process tree.** `exec.CommandContext` only signals the direct child (bash). The grandchild python survived holding the stdout pipe, so `Wait()` was never reached and `finish()` never ran → the run stayed "running" forever, the API kept returning 409, and **an orphaned `schema_v3.py` kept overwriting the very DB the mutex was protecting** | `Setpgid` creates a process group and cancel sends SIGTERM to the group (`-pid`). `WaitDelay 10s` so a held pipe cannot stall it. Measured: 0 grandchildren · `cancelled` · new run 202 |
| **The run mutex locked permanently.** Two early returns in `exec()` never cleared `s.current`, so one failure to create the log file meant 409 until restart — even after the cause was fixed | `defer` clears it on every exit path. Measured: 202 after a failure → runs to completion |
| **SSE delivered duplicate lines.** Subscribing happens before file replay, so lines in that window arrive on both. Measured: 50 out of 20,000 | each line carries a sequence number, compared against the replayed portion and filtered. Measured: 0 duplicates · 0 losses · monotonically increasing ids |
| **Reconnecting replayed the entire log.** There was no `id:` and no `Last-Event-ID` handling → the UI's 4,000-line cap cut off the live tail | emit `id:` and honour `Last-Event-ID`. Measured: 20,000 → 10 lines |
| **A duplicate key in `aliases.yml` silently deleted data.** Pressing "Add" twice for the same representative created two blocks, and `safe_load` keeps only the last — the save returned 200 | the UI inserts under the existing key, and the server rejects duplicate top-level keys during the parser walk |
| **Declaring an alias turned off the path-shaped guard.** Just writing `Hermes: [hermes-agent]` merged `~/.hermes` (a config directory) in as a tool — a merge nobody approved | judge on **the literal string a human actually wrote**. Asking with the normalized key turns `~/.hermes` into `hermes` too, which always answers "yes, they wrote it". Merge if stated, keep separate if not |
| **The `--weak` path lost the Latin-Latin rejection.** Moving to buckets dropped the guard in `why()`, resurrecting 7 pairs like `ABI/api` | same condition applied in the weak loop |
| **`fused` was a dict, so entries sharing a fold key overwrote each other** (no impact on the current corpus) | `defaultdict(list)` |
| **`setActive` never changed once it held a value** — it could not pick up a run started in another tab or from the CLI | if the user is not looking at a past run, move to the one that is running |
| **Circular import** (`RunLog` → `App` → `RunLog`). It works today but breaks with a TDZ error the moment it becomes `const` | split into `web/src/useLogStream.ts` |

**Also confirmed** — the optimized `alias_suggest` gives **exactly the same answer** as the old
exhaustive comparison (30 strong-rule pairs, 0 missed, 0 extra, 0 reason mismatches). The severity
de-duplication in `status.py recommend()` is correct too (it keeps the higher one). The
non-blocking drop in `publish` did not reproduce any loss even with 400,000 lines and a
45-second-lagged subscriber — *left unverified.*

### The run mutex locked permanently (detail for row 2 above)

Two early returns in `exec()` (log file creation failure, pipe creation failure) did not clear
`s.current`. So **one failure meant the API returned 409 until restart** — even after fixing the
cause (restoring permissions). Reproduced and fixed: `defer` now clears it on every exit path.
Measured — after inducing a failure, the next request is accepted with 202 and runs to completion.

The same review turned up **two folding bugs** in alias candidate search. They were caught the
moment "does the fast implementation give the same answer as the slow one" went into the
self-check:

```
   ① 'oo'→'u' ran before 'woo'         jinwoo → jinwu ≠ 진우(jinu)
   ② only the joined side folded a doubled letter   홍길동(hongildong) ≠ joined(honggildong)
```

① happened to be `Jinwoo` / `진우`, the very pair the docs cited as "the hardest case". After the
fix, real-data candidates went from 29 to 30 pairs, and the added one is exactly that pair.

### What remains

- **There is still no authentication.** Loopback binding is the only thing holding that premise
  up. Attaching a domain with `just up-tls` requires putting basic auth or forward-auth in
  **first**. The same condition applies to reverting the ports to `0.0.0.0`.
- **`/var/run/docker.sock` is mounted `:ro` into proxy and acme** (`tls` profile). `:ro` makes only
  the **socket file** read-only —— the Docker API calls travelling over it are untouched, so either
  container effectively holds host root (docker-compose.yml says the same beside the mount). A
  socket-proxy in front is the only real reduction. Weigh that before exposing 80/443.

---

## 8. Not done yet

- **`plugin/` is outside the container.** The galaxy viewer build runs on the host via
  `just build-plugin`. Adding another node toolchain to the image was not judged worth it.
- **There is no authentication.** It assumes `localhost` or a trusted network. Attaching it to a
  public domain requires basic auth or forward-auth in front of `nginx-proxy`.
  **Put this on a public IP as-is and anyone can run the pipeline.**
- **There is no multi-user support.** Runs are globally singular and records have no owner.
