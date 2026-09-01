# Architecture — kallimachos

## Overview

A system that pulls entities and relations out of a vault's 376 markdown documents with an LLM,
puts them in LanceDB, and runs and shows the result on the web.  The only channel off the machine
is the Claude API; embedding, indexing and search are all local.  Only the LLM call goes through a
relay on the host —— that is what lets the container use the macOS keychain credentials while keeping a single writer on the DB.

---

## 1. High-Level Integration (Component Diagram)

### 1a. C4 L1 — system context

```plantuml
@startuml
title kallimachos — C4 L1 system context

skinparam defaultTextAlignment center
skinparam componentStyle rectangle

actor "taehyun\n<<person>>" as U

package "Kallimachos\n<<software system>>" {
  component "Knowledge DB pipeline + web UI\nvault → knowledge graph → search" as SYS
}

component "Obsidian\n<<external system>>" as OB
cloud "Anthropic API\n<<external system>>\nvia claude -p" as AN
folder "~/.claude/projects\n<<external system>>\nthe raw conversation logs" as SESS

U --> OB : writes notes
U --> SYS : runs the pipeline · reads status (browser)
U --> SYS : searches (CLI · the kal-search skill)

OB --> SYS : provides the vault's .md
SYS --> OB : puts the graph artifacts back\n(kal-graph.json · the plugin)
SYS --> AN : sends chunks, receives entities and relations (HTTPS)
SESS --> SYS : reads the conversation logs (read-only)
@enduml
```

- **Read this node first** — the vault is both the input and the output.  A person writes notes,
  and the system puts the graph artifacts back inside the same vault.
- **The most important edge** — `SYS --> AN` is the single channel off the machine.  Embedding,
  BM25 and search are all local, so search keeps working when that line is cut.
- **The surprise** — the session logs (`~/.claude/projects`) are **a first-class input** alongside
  the vault.  278 of the 376 documents came from there.

### 1b. C4 L2 — containers

```plantuml
@startuml
title kallimachos — C4 L2 containers

skinparam componentStyle rectangle
skinparam defaultTextAlignment center

actor taehyun as U

node "docker compose" {
  component "web\n<<nginx:alpine · 62MB>>\nstatic React · proxies /api" as WEB
  component "api\n<<Go + a Python runtime · 6.5GB>>\nrun orchestration · SSE" as API
  component "pipeline\n<<Python subprocesses>>\nextract · embed · index · export" as PIPE
}

node "host (macOS)" {
  component "claude-relay\n<<Python · 127.0.0.1:8791>>\nstands in for claude -p" as RELAY
  component "just / CLI\n<<Python>>\nthe same code without a container" as CLI
  component "kal-mcp\n<<Python · stdio>>\n5 tools · no network listener" as MCP
  database "LanceDB\n~/.kal/db · 8 tables" as LANCE
  folder "vault\n376 markdown documents" as VAULT
  folder "cache\nlr_cache.jsonl · lr_kg.json" as CACHE
}

cloud "Anthropic API" as AN
component "Claude Code\n<<the kal plugin>>" as CC

U --> WEB : HTTPS 127.0.0.1:5173
U --> CLI : just status / search
CC --> MCP : stdio (JSON-RPC) — search·entity·timeline·neighbors·doc
WEB --> API : /api · SSE (proxy_buffering off)
API --> PIPE : spawns processes · streams stdout

PIPE --> VAULT : reads the .md
PIPE --> LANCE : replaces the tables (mode=overwrite)
PIPE --> CACHE : reads and writes the extraction results
PIPE --> VAULT : writes the graph artifacts
CLI --> LANCE : reads (search)
CLI --> VAULT : reads
MCP --> LANCE : **reads only** — documents · lr_entities · lr_relations
MCP --> VAULT : source text and refs (by doc_id only · the slug is validated)

PIPE --> RELAY : POST /run — sends a prompt, receives text
RELAY --> AN : claude -p (keychain authentication)
@enduml
```

- **Read this node first** — `PIPE` is the only thing that writes to `LANCE`.  The relay is a
  pure text function that knows nothing of the DB, so there stays exactly one writer.
- **The most important edge** — `PIPE --> RELAY`.  That one hop is what lets the container use
  the LLM without credentials of its own.  macOS keeps them in the keychain, so mounting `~/.claude` fails.
- **The surprise** — `CLI` and `API` call **the same Python code**.  The container exists for the
  web UI; it is not a premise of the pipeline.
- **`MCP` is the second transmission boundary** — `SKIP` decides "index this locally?" and
  `no_llm` decides "send this off the machine?".  MCP is where what was indexed is opened to an
  LLM, so it re-reads `no_llm` on every query.  Derived text (`description`, `timeline`) has to
  be blocked too —— filtering the document list alone leaves **sensitive sentences with no source**.
- **The risk** — flock does not cross the container boundary (measured).  Overlap a host
  `just index` with a web run and the same LanceDB is overwritten concurrently and **silently corrupted**.
- **`MCP` does not write** — being read-only, it takes no part in that race.  Its cache is dropped
  by `fresh()` on the mtime of `documents.lance/_versions`, so **no restart is needed.**  (The
  older version watched the directory's mtime and sat 3.3 days stale, measured —— files only ever
  accumulated below it.  An in-place update that does not move the mtime is still missed.)

---

## 2. Incepto-Side Infra Usage

```plantuml
@startuml
title Our own infrastructure — containers · ports · bind mounts

skinparam componentStyle rectangle

node "macOS host" {

  node "docker compose (name: kallimachos)" {
    component "web\n127.0.0.1:5173 -> 80\nhealthcheck /healthz" as WEB
    component "api\n127.0.0.1:8080 -> 8080 (override)\nuser 501:20\nhealthcheck /api/health" as API
    node "profiles: proxy | tls" {
      component "nginx-proxy\n80 · 443" as PROXY
      component "acme-companion\nLet's Encrypt" as ACME
    }
  }

  component "claude-relay\n127.0.0.1:8791\nX-Relay-Token · 8 at a time" as RELAY

  folder "~/.kal  ->  /data/kal\n(rw)" as KAL
  folder "super-brain vault  ->  /vault\n(rw)" as VAULT
  folder "~/.claude  ->  /data/kal/container-home/.claude\n(ro)" as CLAUDE
}

WEB --> API : proxy /api\nread_timeout 24h
API --> KAL : LanceDB · the runs/ record
API --> VAULT : reads .md · writes artifacts
API --> CLAUDE : the settings only (authentication does not follow)
API --> RELAY : host.docker.internal:8791
PROXY --> WEB : VIRTUAL_HOST
ACME --> PROXY : certificate renewal
@enduml
```

- **What is owned locally** — the data sets (`~/.kal` and the vault) are all host files, bind
  mounted.  Not wrapped in volumes because they outlive the container and Obsidian writes them too.
- **The ports and mounts that matter** — every published port is on `127.0.0.1`.  That is
  **because there is no authentication**.  Opened on `0.0.0.0`, anyone on the same network could
  run the pipeline and edit the vault.  In the default composition two are published to the host:
  `web` (5173) and `api` (8080, from the override).  That invariant is now **pinned by a check**
  —— guard ⑨ in `status.py --selftest` confirms every published port across `docker-compose*.yml`
  is either `127.0.0.1:` or `${VAR:-127.0.0.1}`.  A human eye used to be the only defence, and
  the `proxy` profile really did sit there bypassing the rule wholesale with `"80:80"` (2026-08-21).
- **Shared vs isolated** — `HOME` is `/data/kal/container-home`.  The image's `/home/app` is
  owned by uid 1000 while the container runs as `.env`'s UID (501 on macOS), so it could not even be entered.
- **Optional** — the `proxy` and `tls` profiles do not start by default.  Turn them on only for a domain.

---

## 3. End-to-End Sequence (Sequence Diagram)

The representative request — **pressing "extract the knowledge graph" on the web**.  It involves more moving parts than anything else here.

```plantuml
@startuml
title Knowledge-graph extraction — from the browser to LanceDB

actor taehyun as U
participant "web\n(nginx)" as WEB
participant "api\n(Go)" as API
participant "lr_extract\n(Python)" as EX
database "lr_cache.jsonl" as CACHE
participant "claude-relay\n(host)" as RELAY
participant "claude -p" as CLI
participant "Anthropic API" as AN
database "lr_kg.json" as KG

U -> WEB : POST /api/runs {"step":"extract"}
WEB -> API : proxy
API -> API : argument allowlist check
API -> API : LLM precheck (down the claude_cli path)
API --> WEB : 202 {run_id}
WEB --> U : the log screen (subscribes to SSE)

API -> EX : spawns in a new process group (Setpgid)
EX -> EX : cuts the whole vault into 2,400-char chunks (905 of them)
EX -> CACHE : looks up by the hash of the chunk body
CACHE --> EX : 873 hits · only 20 newly called

loop for each new chunk (14 workers)
  EX -> RELAY : POST /run + X-Relay-Token
  RELAY -> RELAY : acquires the semaphore (8 at a time)
  RELAY -> CLI : claude -p --disallowed-tools ...
  CLI -> AN : HTTPS (keychain authentication)
  AN --> CLI : the response
  CLI --> RELAY : text
  RELAY --> EX : {"text": ...}
  EX -> EX : parses the JSON · validates the schema
  EX -> CACHE : appends only what succeeded
end

EX -> EX : in-document merge + entity profile summary
EX -> KG : the results + the vault hash at extraction time (doc_hashes)
EX --> API : the last line of stdout · the exit code
API --> WEB : event: done
WEB --> U : done

note over EX
  If **every** newly called chunk fails, exit 1.
  Going quietly empty is the worst outcome --
  that document shows up in search with no entities.
end note
@enduml
```

- **The trigger** — the run button in the browser.  The API returns 202 at once and the real work
  runs behind it.  Not holding a terminal hostage is why the web UI exists.
- **Where it blocks** — the semaphore in `RELAY` (8 at a time).  Without it, 14 workers put 14
  claude processes straight onto the host.
- **The final durable write** — `lr_kg.json`.  That alone does not reach search; an index rebuild
  then has to read this file into LanceDB.
- **The cache is most of the value** — 873 of 905 are skipped.  The cache key is the hash of the
  chunk body, so an unchanged chunk never calls the LLM.

---

## 4. Service Dependency Map (Service Catalog)

### 4a. C4 L3 — inside the api container

```plantuml
@startuml
title C4 L3 — the api container's components

skinparam componentStyle rectangle

package "api (Go)" {
  component "logging\nrequest log" as LOG
  component "guard\nCSRF · a 2MB body cap\nSec-Fetch-Site · Origin · Content-Type" as GRD
  component "HTTP mux\nnet/http" as MUX
  component "lock gate\nlockHolder() — **fails closed**" as LOCK
  component "runner\na single-run mutex\nprocess-group cancellation" as RUN
  component "SSE streamer\nsequence numbers · Last-Event-ID" as SSE
  component "LLM precheck\nasks down the claude_cli path" as PROBE
  component "alias editor\nrefuses YAML anchors and duplicate keys" as ALIAS
  component "step catalog\nloaded once at boot" as STEPS
}

component "pipeline scripts\n(Python subprocesses)" as PY
component "status.py\nstaleness verdict · recommendation" as STAT
database "~/.kal/runs/\nthe run record + logs" as RUNS
file "~/.kal/.write.lock\nflock + the holder as text" as LK
folder "aliases.yml" as YML

LOG --> GRD : every request
GRD --> MUX : only what passes
MUX --> RUN : POST /api/runs
RUN --> LOCK : is this a DB-writing step
LOCK --> LK : reads the contents to see the holder
RUN --> PY : spawn (only when the lock is free)
RUN --> RUNS : writes run.json
MUX --> SSE : GET /runs/{id}/log
SSE --> RUNS : replays the file, then appends live
MUX --> PROBE : GET /api/llm
PROBE --> PY : a round trip through claude_cli.run
MUX --> ALIAS : GET/PUT /api/aliases
ALIAS --> YML : validates, then writes
MUX --> STAT : GET /api/status
STEPS --> STAT : fetches the STEPS definition (once at boot)
@enduml
```

- **The root node** — now `LOG → GRD`, not `MUX`.  `guard` was once defined and then left
  unwired as `Handler: logging(mux)`, and because Go **does not treat an unused function as an
  error** the build passed cleanly.  It was grep that found it, not the compiler.
- **The most important dependency** — `STEPS --> STAT`.  The pipeline step definitions live in
  `status.py` alone.  A copy on the Go side would inevitably drift, so it is fetched at boot.
- **The node that has to fail closed** — `LOCK`.  Treat **unreadable** and **absent** the same
  and it passes as "no lock", putting two writers on the DB.  Only an absent file counts as
  normal (2026-08-21).
- **The branch** — `PROBE` splits relay/local on whether `KAL_CLAUDE_RELAY` is set.  Calling
  `claude` directly always says "Not logged in" in a container, closing the gate wrongly.

### 4b. Service dependencies — the whole picture

```plantuml
@startuml
title Service dependencies — what calls what

skinparam componentStyle rectangle

component "web (nginx)" as WEB
component "api (Go)" as API
component "status.py" as STAT
component "lr_extract.py" as EX
component "schema_v3.py" as IDX
component "sync_v3.py" as SYNC
component "export_kal_graph.py" as EXP
component "kal_search.py" as SRCH
component "entity_resolve.py" as EM
component "claude_cli.py" as CC
component "claude-relay" as RELAY
component "kal_lock.py" as LOCK
database "LanceDB" as DB
cloud "Anthropic API" as AN
component "sentence-transformers\ne5-small" as ST
component "networkx\nLouvain" as NX

WEB --> API : proxies /api
API --> STAT : status · the step definitions
API --> EX : runs
API --> IDX : runs
API --> SYNC : runs
API --> EXP : runs

EX --> CC : the LLM call
CC --> RELAY : when a relay is configured
CC --> AN : otherwise directly
RELAY --> AN : claude -p
EX --> EM : the merge key

IDX --> EM : the merge key · the representative name
IDX --> ST : chunk and entity embeddings
IDX --> LOCK : exclusive write
IDX --> DB : rewrites the tables
SYNC --> LOCK : exclusive write
SYNC --> DB : incremental update
EXP --> NX : community detection
EXP --> CC : community labels (LLM)
EXP --> DB : reads
SRCH --> DB : the 4-component search
SRCH --> ST : the query embedding
STAT --> DB : row counts · meta · stale
@enduml
```

- **The root node** — `api`.  Except that `kal_search` does not go through the API at all: the CLI and the skill read the DB directly.
- **The riskiest external dependency** — the `Anthropic API`.  Cut it and extraction, distillation
  and community labelling stop.  Search and indexing carry on.
- **The common foundation** — `entity_resolve.py` is used by **both** extraction and indexing.
  Let the merge key drift and relations point at entities that do not exist.
- **The optional branch** — `claude_cli --> RELAY` only when `KAL_CLAUDE_RELAY` is set.

---

## 5. Data Flow Through Storage (Data Flow Diagram)

```plantuml
@startuml
title Where the data rests — from the vault to the graph artifacts

skinparam componentStyle rectangle

folder "~/.claude/projects\nconversation logs (raw)" as SESS
folder "~/.kal/distilled/\ndistilled (staged outside the vault)" as DIST
folder "vault **/*.md\n376 documents (the source of truth)" as VAULT

queue "2,400-char chunks\n(in memory)" as C24
queue "500-char chunks\n(in memory)" as C5

database "lr_cache.jsonl\nchunk hash -> LLM response\n**the heart of resumption**\n+ at (extraction time)\nappend-only = the edit history" as CACHE
database "lr_kg.json\n7,710 entities · 12,515 relations\n(pre-merge · after the index merges names, 7,620 · 12,246)\n+ first_seen/last_seen\n+ timeline (what changed)\n+ events (verbatim · earlier versions included)\n+ doc_hashes" as KG

database "LanceDB ~/.kal/db" as DB {
}
folder "documents · chunks\nix_terms · ix_postings · ix_doclen\nlr_entities · lr_relations · meta" as TABLES

folder "artifacts\ngraphml · graph3d.html\nkal-graph.json · the plugin" as ART

SESS --> DIST : distill_sessions\n(LLM distillation + credential masking)
DIST --> VAULT : promote_distilled\n(move + git commit)

VAULT --> C24 : lr_extract chunking
C24 --> CACHE : hash lookup -- a hit skips the LLM
CACHE --> KG : merge + profile summary
C24 --> KG : only the misses make the LLM round trip

VAULT --> C5 : schema_v3 chunking
C5 --> TABLES : embeddings (e5-small) + the BM25 inverted index
KG --> TABLES : global merge + entity and relation vectors
TABLES --> ART : export (Louvain + LLM labels)
ART --> VAULT : the plugin artifacts go back inside the vault

note bottom of CACHE
  Failures are not cached --
  the next run retries them naturally.
end note
@enduml
```

- **Where it first lands** — `~/.kal/distilled/`.  Built **outside** the vault first.  It enters
  the vault only after passing masking verification.
- **Persisted** — the vault's `.md` is the source of truth and LanceDB is a derivative that can be
  regenerated in full.  That is why old LanceDB versions can be cleaned, and a rebuild does clean them.
- **Reruns and idempotency** — `lr_cache.jsonl` is keyed on the chunk body hash, so an unchanged
  chunk never calls the LLM again.  A full re-extraction is needed only when the chunk size or the prompt changes.
- **Watch the feedback loop** — the artifacts come back inside the vault.  That is why
  `schema_v3.SKIP` excludes `/kg/` — otherwise a KG→note→KG cycle forms.
- **The two chunkings never meet** — 2,400 and 500 chars have strides (2,200 vs 450) that do not
  divide, so no boundary lines up.  `chunk_ids` used to reconstruct that after the fact, but
  nothing read it and it was removed on 2026-08-19.  Search joins on `doc_ids` instead.

---

## Appendix A — sequences by use case

§3 covered the representative path (extraction).  The remaining use cases are below.

### UC1 — checking status

```plantuml
@startuml
title UC1 checking status -- four kinds of staleness, each measured differently

actor taehyun as U
participant "api" as API
participant "status.py" as S
collections "vault" as V
database "LanceDB" as D
database "lr_kg.json" as KG

U -> API : GET /api/status
alt a 20-second cache hit
  API --> U : the cached JSON (X-Cache: hit)
else computed fresh
  API -> S : python status.py --json
  S -> D : table row counts · meta · stale_docs
  S -> V : reads every .md, first 16 chars of sha256
  note right of S
    Compared by **content**, not mtime.
    A touch does not fool it, and an editor
    that leaves mtime alone is not missed.
  end note
  S -> KG : compares doc_hashes against the vault now
  S -> S : artifact mtime vs meta.built_at
  S --> API : status + a recommendation (by severity)
  API --> U : the 4 kinds of staleness · what to run next
end
@enduml
```

- Staleness is not one thing — vault↔index, extraction↔vault, KG display, and artifacts↔DB are measured separately.
- Each is fixed differently, so the UI shows all four separately too.

### UC2 — running a step and streaming the log

```plantuml
@startuml
title UC2 running the pipeline -- SSE · cancellation

actor taehyun as U
participant "api" as API
participant "pipeline" as P
database "runs/*.log" as F

U -> API : POST /api/runs {"step":"index"}
API -> API : argument allowlist · the lock file's holder · the LLM precheck
alt already running
  API --> U : 409 (it says what is running)
else
  API -> P : spawn (Setpgid -- a new process group)
  API --> U : 202 {run_id}
end

U -> API : GET /runs/{id}/log (SSE)
API -> API : **subscribe first**, then replay the file
note right of API
  Reverse the order and lines in between are lost.
  Duplicates can be filtered by sequence number,
  but a loss cannot be undone.
end note
API -> F : replays the accumulated lines (id: n)
loop while running
  P --> API : one line of stdout
  API -> F : records it
  API --> U : data: the line (id: n) -- numbers already sent are skipped
end

opt cancel
  U -> API : POST /runs/{id}/cancel
  API -> P : SIGTERM to the process **group**
  note right of API
    Kill only the direct child and a grandchild python
    keeps hold of the pipe, so Wait never returns.
  end note
end
P --> API : exit
API --> U : event: done
@enduml
```

- Only one runs at a time — because the pipeline replaces the same LanceDB with `mode="overwrite"`.
- On reconnect, `Last-Event-ID` picks up where it left off.  Without it, tens of thousands of lines are pushed again.

### UC3 — confirming an entity alias

```plantuml
@startuml
title UC3 aliases -- taehyun and its two Hangul spellings as one person

actor taehyun as U
participant "the alias screen" as W
participant "api" as API
participant "alias_suggest.py" as SG
entity "aliases.yml" as Y
participant "refresh_kg" as RK
database "LanceDB" as D

U -> W : the alias tab
W -> API : GET /api/aliases/suggestions
API -> SG : --json
SG -> D : all of lr_entities
SG -> SG : romanisation folding · abbreviations · joined spellings
note right of SG
  Embedding similarity is not used --
  all 2,315 pairs at cosine >= 0.93
  were wrong.
end note
SG --> W : 30 candidate pairs (nothing merged automatically)

U -> W : "add" a candidate
W -> W : slots it under the existing key when there is one
note right of W
  Appending a block duplicates the top-level key
  and safe_load keeps only the last --
  the earlier variants disappear silently.
end note
U -> W : save
W -> API : PUT /api/aliases
API -> API : refuses anchors · aliases · duplicate keys
API -> Y : writes

U -> W : apply to the graph
W -> API : POST /api/runs refresh_kg --force
API -> RK : extract -> index -> export
RK -> D : build_canon reads aliases.yml and merges
note right of RK
  The representative spelling is **what the person wrote**.
  Picking the most frequent spelling would ignore
  the name the user actually entered.
end note
D --> U : 4 spellings -> one "Taehyun Hwang"\n(17 documents · degree 42)
@enduml
```

- Nothing is merged automatically.  Candidates are ranked and shown; a person confirms by writing the file.
- A wrong automatic merge quietly poisons the graph; a wrong candidate in a list is simply skipped.

### UC4 — search

```plantuml
@startuml
title UC4 search -- a weighted sum of 4 components

actor taehyun as U
participant "kal_search.py" as S
participant "e5-small" as M
database "LanceDB" as D

U -> S : "Louvain communities"
S -> M : the query embedding ("query: ...")
par the 4 components at once
  S -> D : BM25 (FTS ngram 2,3)
else
  S -> D : chunk vectors
else
  S -> D : entity vectors
else
  S -> D : relation vectors + a 1-hop expansion
end
S -> S : min-max normalisation per component
S -> S : weighted sum 0.18 / 0.05 / 0.18 / 0.59
note right of S
  The relation component is the largest.
  The evidence for that is weak, though --
  see the PRESETS comment in kal_search.py.
end note
S -> D : snippets from the top documents
S --> U : documents · scores · snippets
@enduml
```

- The chunk-vector weight is the smallest at 0.05.  Measured one component at a time, chunks were the weakest.
- The basis for these weights sits inside this corpus's resolution limit — do not read them as settled.

### UC5 — incremental sync and the life of staleness

```plantuml
@startuml
title UC5 incremental sync -- reflect it fast, leave the KG for later

actor taehyun as U
participant "Obsidian" as O
participant "sync_v3" as SY
database "LanceDB" as D
database "stale_docs" as ST
participant "refresh_kg" as RK

U -> O : edits a note
U -> SY : incremental sync (~1 min)
SY -> D : updates chunks, vectors and the inverted index for the changed documents only
note right of SY
  It cannot fix the KG -- that needs an LLM.
end note
SY -> ST : marks "this document's KG is stale"

U -> U : search returns the new content\nentities and relations are still the old ones

opt when staleness passes 20%, or when there is time
  U -> RK : refresh the stale KG (~35 min)
  RK -> RK : extract (thanks to the cache, only the changed chunks)
  RK -> D : rebuild the index
  RK -> ST : clears **only the snapshot taken at the start**
  note right of RK
    Marks sync_v3 added during the rebuild
    are kept.  Those documents were not
    part of this extraction.
  end note
  RK -> RK : export
end
@enduml
```

- Incremental sync **cannot fix the KG.**  Leaving a mark is the whole nature of this step.
- The 20% threshold is justified by cost, not by the size of the loss — a rebuild takes a few
  minutes on cache hits, so running it often costs little.  See the `refresh_kg.WARN_RATIO` comment.

---

## Appendix B — what to watch out for in this structure

| | Why |
|---|---|
| **One writer on the DB** | flock does not cross the container boundary (measured both ways).  Do not overlap a host `just index` with a web run |
| **The relay reads its flags at boot** | edit `claude_cli.py` and `just relay` has to be restarted |
| **There is no authentication** | loopback binding is the only defence.  Basic auth or forward-auth is required before attaching a domain |
| **An extraction failure can be silent** | a total failure exits 1.  A partial failure is retried as a cache miss on the next run |
| **Indexing reads what extraction produced** | there is an order.  `schema_v3` does not create entities; it moves `lr_kg.json` across |

Related documents — [`STACK.md`](./STACK.md) (containers and the relay in operation) ·
[`PIPELINE.md`](./PIPELINE.md) (step by step) · [`ERD.md`](./ERD.md) (table schemas) ·
[`ENTITY_RESOLVE.md`](./ENTITY_RESOLVE.md) (the merge rules)
