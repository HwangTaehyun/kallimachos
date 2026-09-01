# kallimachos — the command list.
#
#   just            what you can do
#   just status     the current state and what to run next
#   just up         the development stack (vite HMR)
#
# Every pipeline step runs without a container (`just extract` and so on).  The container is
# for the web UI, not a prerequisite of the pipeline —— the host venv runs it just the same.

set shell := ["bash", "-uc"]

#  ⚠ `just --list` uses **only the single line directly above a recipe** as its help.  When
#     attaching a multi-line explanation, put **a blank line** after it and keep a separate
#     one-line summary directly above the recipe.  Otherwise the list shows a sentence cut in
#     half —— and the README points people at that list as the recipe reference (deep review 2026-08-25).

#  The standard uv layout —— `pyproject.toml` is the source of truth for dependencies and the
#  environment is `.venv` inside the repo.  It used to be `~/.kal/venv`.  Putting data (`~/.kal`)
#  and code dependencies in one place produces "I moved my vault, why is it downloading torch" —— they are kept apart.
py  := env_var_or_default("KAL_PYTHON", justfile_directory() + "/.venv/bin/python")
#  ⚠ Without an override for `src`, `KAL_PYTHON` alone cannot run in a container ——
#     every recipe is `{{py}} {{src}}/x.py` and `{{src}}` is **a host absolute path**, which
#     does not exist inside the container.  There it is `/app/src`.
#     (deep review 2026-08-23, completeness lens: "goal half #1 is blocked by this one line")
src := env_var_or_default("KAL_SRC", justfile_directory() + "/src")

default:
    @just --list --unsorted

# ── Running in the container ───────────────────────────────────────────
#
#   Why it is needed —— when the thing being fixed is in the container and the thing being
#   checked is on the host, a developer drifts to the host.  Two variables, `KAL_PYTHON` and
#   `KAL_SRC`, run **the same recipe** in the container.  Not maintaining two sets is the point.
#
#   The data is **a host bind** (not a named volume).  That is what lets the host CLI and MCP
#   see what the container indexed —— with a named volume the three look at different DBs, and
#   it appears only as "the results are empty", with no error.

# The same recipe inside the container (for example: just dc index · just dc status)
dc +args:
    @docker image inspect kal:local >/dev/null 2>&1 || just build-kal
    KAL_PYTHON="{{justfile_directory()}}/src/_dcpy.sh" KAL_SRC=/app/src just {{args}}

# The vault path is decided **in one place**.  Undecidable, it **fails loudly**.
#
#   ⚠ The old default was `dirname(the repository)`.  Cloned on its own, that becomes
#     **the folder holding the clone** —— fetched into `~/kal`, it is `$HOME`.
#     Measured (2026-08-25): there are 123,493 `.md` under this machine's home.  With no error
#     it would take all of them as the vault and try to send them through `claude -p`.
#     **Stopping beats guessing.**
_vault:
    #!/usr/bin/env bash
    set -euo pipefail
    #  ① the environment wins (for a one-off)
    #  ⚠ A value present is still **validated.**  The check used to live in `setup` alone, so
    #     `VAULT_DIR=$HOME just _vault` returned home as it was, and that value went into
    #     `sh`, `mcp-plugin-test` and `codex-config` (2026-08-25).
    for v in "${VAULT_DIR:-}" "${KAL_VAULT:-}"; do
      [ -n "$v" ] || continue
      {{py}} {{src}}/vault_path.py --check "$v" >&2 || exit 1
      echo "$v"; exit 0
    done
    #  ② `.env` —— `just setup` and `just vault` write here
    if [ -f "{{justfile_directory()}}/.env" ]; then
      v=$(grep -m1 '^VAULT_DIR=' "{{justfile_directory()}}/.env" | cut -d= -f2- || true)
      if [ -n "$v" ]; then echo "${v/#\~/$HOME}"; exit 0; fi
    fi
    #  ③ nothing —— it does not guess
    echo "  ❌ I do not know the vault path." >&2
    echo "     Please set the folder holding your notes:" >&2
    echo "       just init                # a screen that helps you pick (recommended)" >&2
    echo "       just vault ~/my-notes    # if you already know" >&2
    echo "       KAL_VAULT=~/my-notes just <command>   # this run only" >&2
    exit 1

# A container shell —— for running python inside directly
sh:
    @docker image inspect kal:local >/dev/null 2>&1 || just build-kal
    docker run --rm -it \
      -v "${KAL_DIR:-$HOME/.kal}:/data" \
      -v "$(just _vault):/vault:ro" \
      --entrypoint /bin/bash kal:local

# Build the MCP image
build-kal:
    docker build -t kal:local .

# ★ The acceptance test —— does the plugin **really start**.  It performs a real stdio
#   handshake with the same hardened flags as `.mcp.json` and checks the 5 tools, the version
#   and a clean stdout.  Why it is not called `plugin-test`: in this repository "the plugin"
#   already means `plugin/` (the Obsidian plugin kal-galaxy) —— see `just build-plugin`.

# The acceptance test —— does the container's MCP really start over stdio (5 tools · version · stdout)
mcp-plugin-test:
    @docker image inspect kal:local >/dev/null 2>&1 || just build-kal
    @{{py}} {{src}}/plugin_probe.py \
      --kal "${KAL_DIR:-$HOME/.kal}" \
      --vault "$(just _vault)"

# ── Codex ─────────────────────────────────────────────────────────────
#
#   Codex is **not MCP-only.**  It attaches two ways:
#     · MCP     [mcp_servers.kal] in ~/.codex/config.toml
#     · skills  ~/.agents/skills/<name>/SKILL.md      ← **not** `.codex/skills`
#
#   The widely circulated blog guidance saying `~/.codex/skills` is wrong.  Copy it and you
#   get a silent no-op (official: learn.chatgpt.com/docs/build-skills.md, confirmed 2026-08-23).

# Install the skills for Codex (~/.agents/skills).  It shows what changes before overwriting
codex-skills:
    #!/usr/bin/env bash
    set -euo pipefail
    DEST="$HOME/.agents/skills"
    mkdir -p "$DEST"
    for d in {{justfile_directory()}}/skills/*/; do
      n=$(basename "$d")
      if [ -e "$DEST/$n" ] && ! diff -rq "$d" "$DEST/$n" >/dev/null 2>&1; then
        echo "  ⚠ $n —— it exists with different content.  Overwriting (the original is in the repository)"
      fi
      rm -rf "$DEST/$n"; cp -R "$d" "$DEST/$n"
      echo "  ✅ $n → $DEST/$n"
    done

# Print the MCP configuration snippet for Codex (paste it into ~/.codex/config.toml)
codex-config:
    #!/usr/bin/env bash
    set -euo pipefail
    KAL="${KAL_DIR:-$HOME/.kal}"
    VAULT="$(just _vault)"
    VER=$({{py}} -c "import json;print(json.load(open('{{justfile_directory()}}/.claude-plugin/plugin.json'))['version'])")
    cat <<TOML
    # Paste this into ~/.codex/config.toml.
    [mcp_servers.kal]
    command = "docker"
    args = [
      "run", "--rm", "-i",
      "--network", "none", "--read-only",
      "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
      "--tmpfs", "/tmp",
      "--user", "$(id -u):$(id -g)",
      "-v", "$KAL/db:/data/db:ro",
      "-v", "$VAULT:/vault:ro",
      "ghcr.io/hwangtaehyun/kal:$VER",
    ]
    # ⚠ The 10-second default is not enough —— loading the model takes longer.
    #    Finish the image pull **outside this**, in advance: docker pull …
    startup_timeout_sec = 120
    TOML

# ── Setup ─────────────────────────────────────────────────────────────

# The first time.  **Safe to run repeatedly** —— it leaves what exists alone.
#
#   Why it is a recipe —— this repository ties together a python venv, Docker and node, and
#   which of them a given step needs was scattered across the docs.  If someone who just
#   cloned it cannot find "where do I start", that is the barrier to entry.
#
#   For the venv it uses `uv` when present —— this repository's venv really was made with uv
#   (pyvenv.cfg says `uv = 0.7.13`), and installing with torch is far faster than pip.
#   Without it, it falls back to the standard venv plus pip.

# The first time —— venv, dependencies and .env in one go (safe to run repeatedly)
setup:
    #!/usr/bin/env bash
    set -euo pipefail
    KAL="${KAL_HOME:-$HOME/.kal}"
    echo "▸ kallimachos setup  (KAL_HOME=$KAL)"

    # ① prerequisites —— it also says what will not work without each
    command -v python3 >/dev/null || { echo "  ❌ no python3 (3.11+)"; exit 1; }
    echo "  ✅ python3   $(python3 -V | cut -d' ' -f2)"
    for t in docker node go; do
      if command -v "$t" >/dev/null; then echo "  ✅ $t"
      else case "$t" in
        docker) echo "  △ no docker —— the web UI (just up) is unavailable.  The pipeline runs as usual";;
        node)   echo "  △ no node   —— the web and plugin builds are unavailable";;
        go)     echo "  △ no go     —— the api build is unavailable (a container can do it)";;
      esac; fi
    done

    # ② dependencies —— `pyproject.toml` + `uv.lock` are the source of truth
    if ! command -v uv >/dev/null; then
      echo "  ❌ uv is missing.  Install:  curl -LsSf https://astral.sh/uv/install.sh | sh"
      echo "     (or  brew install uv)"
      exit 1
    fi
    echo "  ✅ uv       $(uv --version | cut -d' ' -f2)"
    echo "  ▸ uv sync —— matching the lock file exactly (the first time downloads torch, a few minutes)"
    uv sync
    .venv/bin/python -c "import lancedb, sentence_transformers, textual" \
      && echo "  ✅ dependencies confirmed (lancedb · sentence-transformers · textual)"

    # ③ .env —— filled with this machine's values.  If it exists, it is left alone.
    if [ -f .env ]; then
      echo "  ✅ .env present (left alone)"
    else
      #  ⚠ **It does not guess.**  The old default `dirname "$(pwd)"` becomes the folder
      #     holding the clone when cloned on its own —— fetched into `~/kal` that is `$HOME`,
      #     and there are 123,493 `.md` under this machine's home.
      #     With no error it would index all of them and try to send them through `claude -p`.
      if [ -z "${KAL_VAULT:-}" ]; then
        echo "  ❌ I do not know where the notes are —— I will not guess."
        echo "     Please set it one of two ways:"
        echo "       just init                    # a screen that helps you pick (recommended)"
        echo "       KAL_VAULT=~/my-notes just setup"
        exit 1
      fi
      VAULT="${KAL_VAULT}"
      [ -d "$VAULT" ] || { echo "  ❌ no such folder: $VAULT"; exit 1; }
      VAULT="$(cd "$VAULT" && pwd -P)"          # resolves symlinks and relative paths
      #  ⚠ The check comes **before the counting**.  The order was the other way round at
      #     first, and picking home made `find` walk 120,000 files and stall for over two
      #     minutes —— it takes longest in exactly the situation it exists to prevent (measured 2026-08-25).
      #  The judgement is made by `vault_path.check()` alone —— the same rule as `just _vault`.
      #  It used to check only `= $HOME` here, so `/Users` and `/` passed (which is worse).
      {{py}} {{src}}/vault_path.py --check "$VAULT" || {
        echo "     Please give a subfolder that holds only notes."; exit 1; }
      #  The counting is **capped** too.  An exact number is not needed —— only "is it 0 /
      #  is it absurdly many".
      #  ⚠ Under `set -o pipefail`, `find | head` makes find die of SIGPIPE (141) when head
      #     closes first, and the whole pipeline fails —— and then **not one line** of the
      #     warning below appears.  Reproduced 3/3 with `KAL_VAULT=/`
      #     (2026-08-25, round 5).  It is switched off for this stretch only.
      set +o pipefail
      n=$(find "$VAULT" -name '*.md' -not -path '*/node_modules/*' 2>/dev/null \
          | head -20000 | wc -l | tr -d ' ')
      set -o pipefail
      if [ "$n" = 0 ]; then
        echo "  ⚠ there is not a single .md under $VAULT —— is the path right?"
      elif [ "$n" -ge 20000 ]; then
        echo "  ⚠ there are 20,000 or more .md ($VAULT).  Index all of them, really?"
      else
        echo "  vault: $VAULT  (${n} .md)"
      fi
      sed -e "s|^UID=.*|UID=$(id -u)|" \
          -e "s|^GID=.*|GID=$(id -g)|" \
          -e "s|^KAL_DIR=.*|KAL_DIR=$KAL|" \
          -e "s|^CLAUDE_DIR=.*|CLAUDE_DIR=$HOME/.claude|" \
          -e "s|^VAULT_DIR=.*|VAULT_DIR=$VAULT|" \
          -e "s|^VAULT_NAME=.*|VAULT_NAME=$(basename "$VAULT")|" \
          .env.example > .env
      echo "  ✅ .env created —— VAULT_DIR=$VAULT"
      echo "     ⚠ Are the notes really there?  If not:  KAL_VAULT=/path just setup"
    fi

    # ④ what next
    echo
    echo "  Next:"
    echo "    just init       ← pick a folder and get to a searchable state (TUI)"
    echo "    just status     the current state and the step to run next"
    echo "    just up         the web UI (docker · http://localhost:5173)"
    echo "    just selftest   every self-check attached to the code"
    echo "    just verify     whether the documented numbers match the real DB"

# ── State ─────────────────────────────────────────────────────────────

# The current state and what to run next
status:
    @{{py}} {{src}}/status.py

# JSON, for machines
status-json:
    @{{py}} {{src}}/status.py --json

# Is the extraction scope the same as the indexing scope (diverge and those documents never enter the KG)
check-scope:
    @{{py}} {{src}}/lr_extract.py --check-scope

# Links only (no knowledge DB —— for the CI runner)
verify-links:
    @{{py}} {{src}}/verify_docs.py --links-only

# Whether the documented numbers and relative links match reality
verify:
    @{{py}} {{src}}/verify_docs.py

# Every self-check attached to the code
#  The whole set, docker included.  Used locally.
#  Where the openwiki bundle and the Obsidian vault live.  Override on the command line:
#     just openwiki ~/other-bundle ~/other-vault
openwiki_dir := env_var_or_default("OPENWIKI_DIR", home_dir() / "github/HwangTaehyun/openwiki")
vault_dir    := env_var_or_default("VAULT_DIR", home_dir() / "github/HwangTaehyun/super-brain")

selftest: mcp-test selftest-py

#  ⚠ The list lives **here and nowhere else**.  CI calls this —— 9 of them were once copied by
#    hand into the CI workflow, diverged from the 27 here, and CI went green while 18 never
#    ran (2026-08-24, found while moving).  A list in two places is guaranteed to diverge.
#    `mcp-test` is left out only because it **opens the real DB** —— it does not use docker
#    (that is what it used to say, and it was wrong.  The one that uses docker is `mcp-plugin-test`).
#
# Every self-check that runs without docker (CI calls this)
selftest-py:
    #  ⚠ A lock out of step with pyproject **kills the container build** —— `uv sync --frozen`
    #    fails with `Could not find root package`.  Locally it goes unnoticed because the
    #    already-built .venv makes everything run: measured (2026-08-25), changing only
    #    `pyproject`'s name left every self-check and test passing while `just up` died.
    #    It is the cheapest check, so it goes first.
    @uv lock --check
    @{{py}} {{src}}/plugin_probe.py --selftest
    @{{py}} {{src}}/kal_lock.py
    @{{py}} {{src}}/entity_resolve.py
    @{{py}} {{src}}/run_log.py
    @{{py}} {{src}}/claude_relay.py --selftest
    @{{py}} {{src}}/test_stale_resolve.py
    @{{py}} {{src}}/alias_suggest.py --selftest
    @{{py}} {{src}}/verify_docs.py --selftest
    @{{py}} {{src}}/db_ready.py --selftest
    @{{py}} {{src}}/vault_path.py --selftest
    @{{py}} {{src}}/fixture_db.py --selftest
    @{{py}} {{src}}/export_kal_graph.py --selftest
    @{{py}} {{src}}/lr_extract.py --selftest
    @{{py}} {{src}}/schema_v3.py --selftest
    @{{py}} {{src}}/kal_search.py --selftest
    @{{py}} {{src}}/kal_config.py --selftest
    @{{py}} {{src}}/status.py --selftest
    @{{py}} {{src}}/estimate.py --selftest
    @{{py}} {{src}}/run.py --selftest
    @{{py}} {{src}}/set_vault.py --selftest
    @{{py}} {{src}}/init_tui.py --selftest
    @{{py}} {{src}}/kal_reset.py --selftest
    @{{py}} {{src}}/kal_migrate.py --selftest
    @{{py}} {{src}}/sync_v3.py --selftest
    @{{py}} {{src}}/ingest_sessions.py --selftest
    @{{py}} {{src}}/ingest_codex_sessions.py --selftest
    @{{py}} {{src}}/remask_docs.py --selftest
    @{{py}} {{src}}/export_graph.py --selftest
    @{{py}} {{src}}/export_webgl.py --selftest
    @{{py}} {{src}}/promote_distilled.py --selftest
    @{{py}} {{src}}/distill_sessions.py --selftest
    @{{py}} {{src}}/okf_convert.py --selftest
    @{{py}} {{src}}/openwiki_emit.py --selftest
    #  ⚠ The OKF version string is frozen at 0.2 while its content moved (2026-08-21
    #     tightened every timestamp rule).  A consumer pinning the version cannot see
    #     that, so the **sha256 is the only live signal**.  Upstream also relocated to its
    #     own repository and declared the old path a frozen snapshot —— which is the worst
    #     case for a watcher, because the abandoned path reports "unchanged" forever.
    #     Offline is not a failure: this warns, it does not block.
    @H=$(curl -sfL --max-time 20 https://raw.githubusercontent.com/GoogleCloudPlatform/open-knowledge-format/main/SPEC.md | shasum -a 256 | cut -d" " -f1); \
     P=$(shasum -a 256 ~/github/HwangTaehyun/openwiki/references/okf-SPEC-v0.2.md 2>/dev/null | cut -d" " -f1); \
     if [ -z "$H" ]; then echo "  ⓘ OKF drift check skipped (offline)"; \
     elif [ -z "$P" ]; then echo "  ⓘ OKF drift check skipped (no pinned copy)"; \
     elif [ "$H" = "$P" ]; then echo "  ✅ pinned OKF spec matches upstream ($(echo $H | cut -c1-12)…)"; \
     else echo "  ⚠ OKF spec MOVED upstream —— pinned $(echo $P | cut -c1-12)… vs live $(echo $H | cut -c1-12)…"; \
          echo "     re-pin: curl -sL https://raw.githubusercontent.com/GoogleCloudPlatform/open-knowledge-format/main/SPEC.md -o ~/github/HwangTaehyun/openwiki/references/okf-SPEC-v0.2.md"; fi
    @{{py}} {{src}}/fm_migrate.py --selftest
    @{{py}} {{src}}/okf_sample.py --selftest
    @{{py}} {{src}}/okf_compare.py --selftest
    @{{py}} {{src}}/entity_resolve.py
    @KAL_HOME=$(mktemp -d) {{py}} {{src}}/kal_lock.py
    @# MCP used to be a separate command —— which is how 59 assertions in kal_mcp went
    @# entirely unrun in one session, and breaking the sensor while fixing it surfaced only after the commit.
    @# It takes 8 seconds.  "Check everything" has to be one command for that phrase to be true.
    @# It is defined in `mcp-test` alone —— the same line written twice eventually changes on one side only.
    @# Having nothing to do is not a failure —— SystemExit("a string") exits 1, so
    @# the web UI showed a healthy run as 'failed' (2026-08-19)
    @#  ⚠ It is given **a temporary vault**.  This is a real run, so `vault_path.vault()`
    @#     legitimately refuses —— what is being checked is "does it exit 0 when there is
    @#     nothing to do", and an empty temporary vault is exactly that state.  Using the
    @#     author's vault would make this check run on that machine alone (2026-08-25).
    @T=$(mktemp -d) && KAL_VAULT="$T" KAL_DIR="$T" {{py}} {{src}}/promote_distilled.py --dry-run >/dev/null && rm -rf "$T" && echo "  ✅ promote --dry-run exits 0"
    @bash -n {{src}}/rebuild_all.sh && echo "  ✅ rebuild_all.sh syntax"
    @#  ⚠ Neither .gitignore nor .dockerignore supports an end-of-line comment —— the whole
    @#     line becomes one pattern and matches nothing.  Both files say so in their own
    @#     headers, and .dockerignore:22 was that shape anyway.  Measured 2026-09-01 with
    @#     two real builds of one context: `api/api          # …` put the binary into the
    @#     build context (`-rw-r--r-- 3000000 api`), the same rule with the comment on its
    @#     own line left the directory empty (`total 8`).  Nothing errors either way ——
    @#     the ignore rule just silently stops existing, so it takes a machine to see.
    @if grep -nE '^[^#[:space:]].*[^[:space:]][[:space:]]+#' .gitignore .dockerignore; then echo "  ❌ end-of-line comment above —— that whole line is one pattern and matches nothing"; exit 1; fi; echo "  ✅ ignore files carry no end-of-line comments"
    @echo "  ── the list above is every self-check (read the list, do not count) ──"

# ── The pipeline ──────────────────────────────────────────────────────

# Distil sessions (~/.claude conversations → vault documents)
#
# ⚠ The default `--workers 8` **must match** what status.py declares.  With no argument the
#   script's own default (10) applied and it ran differently from the UI.  The UI says
#   "run `just distill` on the host" when LLM authentication is missing, so the two paths have to match.
#   (On a long run, more workers means a higher failure rate —— see the measurement at lr_extract.py:125.)
#   An argument given overrides it.  The self-check compares this against the declaration.
distill *args="--workers 8":
    @{{py}} {{src}}/distill_sessions.py {{args}}

# Promote the distilled documents into the vault (~/.kal/distilled → raw/conversations/sessions/ + a commit)
promote *args:
    @{{py}} {{src}}/promote_distilled.py {{args}}

# Extract the knowledge graph (LLM.  The cache means only changed chunks are called)
# The first-run TUI —— pick a folder, see how long it takes, and run it through
init *args:
    @{{py}} {{src}}/init_tui.py {{args}}

# When things get tangled —— choose how far back to go.  With no argument it only shows what would disappear
reset *args:
    @{{py}} {{src}}/kal_reset.py {{args}}

# Come up from the old name (~/.kdb · KDB_*).  With no argument it only shows what it would do
migrate *args:
    @{{py}} {{src}}/kal_migrate.py {{args}}

# Set the vault to index (both the CLI and the container).  With no argument it shows the current value
vault *args:
    @{{py}} {{src}}/set_vault.py {{args}}

# Run a step —— it says how long before starting, and shows signs of life during quiet stretches
run step *args:
    @{{py}} {{src}}/run.py {{step}} {{args}}

# Only see how long it will take (runs nothing).  With no argument, every step
plan *args:
    @{{py}} {{src}}/estimate.py {{args}}

extract *args:
    @{{py}} {{src}}/lr_extract.py {{args}}

# Rebuild the knowledge DB (the whole index)
index *args:
    @{{py}} {{src}}/schema_v3.py {{args}}

#  ── openwiki ──────────────────────────────────────────────────────────────────────────────
#
#  One bundle, two kinds of input, one knowledge DB:
#
#      ~/.claude/projects/  ─┐
#      ~/.codex/sessions/   ─┼─ ingest ─ distil ─┐
#                            │                    ├─ openwiki_emit ─→ openwiki/personal/
#      an Obsidian vault    ─┴────────────────────┘                          │
#                                                                       index ▼
#                                                                    ~/.kal/db
#
#  `openwiki` does the whole chain.  Each half is also runnable on its own, because the two
#  halves fail for completely different reasons: the session half needs an LLM (and on macOS,
#  the host relay), while the vault half is pure conversion and never calls one.

# Everything → the openwiki bundle → the knowledge DB.  VAULT is the Obsidian vault to migrate.
openwiki wiki=openwiki_dir vault=vault_dir:
    @just openwiki-sessions "{{wiki}}"
    @just openwiki-vault "{{wiki}}" "{{vault}}"
    @just openwiki-index "{{wiki}}"

# Agent session logs → the bundle.  Skips what is already distilled, so a re-run is cheap.
openwiki-sessions wiki=openwiki_dir:
    @{{py}} {{src}}/ingest_sessions.py
    @{{py}} {{src}}/ingest_codex_sessions.py
    @{{py}} {{src}}/distill_sessions.py
    @{{py}} {{src}}/openwiki_emit.py --wiki "{{wiki}}"

#  ⚠ `--exclude /conversations/sessions/` is load-bearing.  The vault holds a copy of the
#     distilled session documents, and without this they arrive a second time under
#     personal/raw/ —— the same documents, a second set of doc_ids, both indexed.
# An Obsidian vault → the bundle.  No LLM is called; this is pure conversion.
openwiki-vault wiki=openwiki_dir vault=vault_dir:
    @{{py}} {{src}}/openwiki_emit.py --wiki "{{wiki}}" --from "{{vault}}/wiki"      --into personal/wiki      --force
    @{{py}} {{src}}/openwiki_emit.py --wiki "{{wiki}}" --from "{{vault}}/kg"        --into personal/kg        --force
    @{{py}} {{src}}/openwiki_emit.py --wiki "{{wiki}}" --from "{{vault}}/raw"       --into personal/raw       --exclude /conversations/sessions/ --force
    @if [ -d "{{vault}}/Clippings" ]; then {{py}} {{src}}/openwiki_emit.py --wiki "{{wiki}}" --from "{{vault}}/Clippings" --into personal/clippings --force; fi

#  ⚠ This points KAL_VAULT at the bundle for one command instead of changing the saved setting.
#     `just vault` writes both ~/.kal/config.json and .env, and switching those would leave the
#     CLI and the container indexing the bundle while everything else still says super-brain.
#     The bundle already contains the vault's documents, so nothing is lost by reading it here.
# Index the bundle into the knowledge DB.  The kg export is skipped (KG→note→KG feedback).
openwiki-index wiki=openwiki_dir:
    @echo "  indexing {{wiki}} → ~/.kal/db"
    @KAL_VAULT="{{wiki}}" {{py}} {{src}}/schema_v3.py

# What would be converted and indexed, without doing any of it
openwiki-plan wiki=openwiki_dir vault=vault_dir:
    @{{py}} {{src}}/openwiki_emit.py --wiki "{{wiki}}" --dry-run
    @KAL_VAULT="{{wiki}}" {{py}} -c "import sys;sys.path.insert(0,'{{src}}');\
     import schema_v3 as S,glob,os,collections;\
     W=os.path.expanduser('{{wiki}}');c=collections.Counter();\
     [c.update([S.classify_origin(os.path.relpath(f,W),open(f,encoding='utf-8',errors='replace').read())]) if not S.is_skipped(os.path.abspath(f)) else c.update(['skipped']) for f in glob.glob(W+'/**/*.md',recursive=True)];\
     print('  index plan:', dict(c))"

# Homonym candidates —— a person confirms them into homonyms.yml
homonyms *args:
    @{{py}} {{src}}/homonym_suggest.py {{args}}

# An MCP server check —— it really calls all 5 tools
mcp-test:
    @#  ⚠ This test reads a document for real through `kal_doc`, **so it needs a real vault**.
    @#     `kal_mcp.py` used to hardcode the author's path as a default, so it passed on that
    @#     machine alone —— on anyone else's it died with `{"error":"unreadable"}`.
    @#     Removing the default exposed it (2026-08-25).  It is stated explicitly through `_vault`.
    @KAL_VAULT="$(just _vault)" {{py}} {{src}}/kal_mcp.py --selftest

#  Upload to the cloud —— **the index (db/) only**, never the source text.  The token and address come from the
#  app's MCP screen (app.kallimachos.dev).  The server swaps the tar into the user's folder wholesale (so an
#  interruption leaves the old graph alive), so re-running the same command whenever the index changes is enough.
push:
    #!/usr/bin/env bash
    set -euo pipefail
    : "${KAL_CLOUD_TOKEN:?KAL_CLOUD_TOKEN is required —— create one on the app's MCP screen}"
    URL="${KAL_CLOUD_URL:?KAL_CLOUD_URL is required (for example: https://app.kallimachos.dev)}"
    DB="${KAL_PATH:-${KAL_HOME:-$HOME/.kal}/db}"
    [ -d "$DB" ] || { echo "no index: $DB —— run an index once first (just run index)"; exit 1; }
    echo "uploading: $DB ($(du -sh "$DB" | cut -f1)) → $URL"
    tar -C "$DB" -czf - . | curl -sS --fail-with-body -X POST "$URL/api/graph" \
        -H "Authorization: Bearer $KAL_CLOUD_TOKEN" -H "Content-Type: application/gzip" --data-binary @-
    echo

# Incremental sync (changed documents only)
sync *args:
    @{{py}} {{src}}/sync_v3.py {{args}}

# Refresh the stale KG (extract → build → export)
refresh *args:
    @{{py}} {{src}}/refresh_kg.py {{args}}

# Why KAL_PYTHON is passed: export_all.sh defaults to `python3`, so without it the system
# python is used instead of the venv and lancedb is not found.
#
# Export the graph —— the same thing the web UI's "export the graph" (status.py) runs
export:
    @KAL_PYTHON={{py}} bash {{src}}/export_all.sh

# Clean up old LanceDB versions (they pile up across repeated rebuilds)
vacuum *args:
    @{{py}} {{src}}/vacuum.py {{args}}

# KAL_PYTHON is passed for the same reason as export (the default python3 cannot find the venv)
#
# Everything
rebuild:
    @KAL_PYTHON={{py}} bash {{src}}/rebuild_all.sh

# ── Step-id aliases ───────────────────────────────────────────────────
# When claude is unauthenticated in the container, the web UI and API point at `just <step id>`
# as the only recovery path (web/src/components/StepList.tsx · api/main.go).
# Those ids are src/status.py's STEPS[].id and are spelled differently from the recipe names ——
# without the aliases, following the guidance ends in "Justfile does not contain recipe".

alias refresh_kg    := refresh
alias rebuild_all   := rebuild
alias apply_aliases := aliases-apply

# ── Aliases ───────────────────────────────────────────────────────────

# Alias candidates (nothing is merged automatically — a person writes them into aliases.yml)
aliases *args:
    @{{py}} {{src}}/alias_suggest.py {{args}}

# Apply aliases.yml to the graph
#
# ⚠ It is `--aliases-only`.  It used to be `--force`, which is **the 35-minute path that
#   re-runs extraction** (status.py declares "apply aliases only (fast) · 5 min").  When the
#   container has no claude authentication the UI says "run `just apply_aliases` on the host",
#   and that guidance was sending people to a different command, 7× slower, that also consumes homonyms.yml.
#   (r4-ux, round 5, 2026-08-21.  It has to match status.py's `cmd`, and the self-check enforces it.)

# Apply the alias file (aliases.yml) to the knowledge DB
aliases-apply:
    @{{py}} {{src}}/refresh_kg.py --aliases-only

# ── Search ────────────────────────────────────────────────────────────

search query *args:
    @{{py}} {{src}}/kal_search.py "{{query}}" {{args}}

# ── The host claude relay ─────────────────────────────────────────────
# In a container claude is unauthenticated (macOS keeps credentials in the keychain).
# Leave this running and extraction and distillation become pressable from the web UI.  It does not touch the DB.

relay *args:
    @{{py}} {{src}}/claude_relay.py {{args}}

# ── Containers ────────────────────────────────────────────────────────

# The development stack (vite HMR · api mounts src/)
up:
    @test -f .env || (echo "  there is no .env.  Run  just env  first." && exit 1)
    @#  ⚠ **The value is checked too.**  Passing on the file merely existing let compose take
    @#     the placeholder `/Users/you/vault` that `just env` left behind, and docker
    @#     **silently created** that path, so the screen said "0 documents" (round 6).
    @{{py}} {{src}}/vault_path.py --check "$(grep -m1 '^VAULT_DIR=' .env | cut -d= -f2-)" \
      || (echo "     Set it with  just vault <notes folder>." && exit 1)
    docker compose up --build -d
    @echo "  → http://localhost:$(grep -E '^WEB_PORT=' .env | cut -d= -f2 || echo 5173)"

# The production stack (a static build.  -f is explicit so the override is left out)
#  ⚠ **The same guard** as `up`.  It was missing only here, so calling it without `.env`
#     started anyway through compose's own default substitution (`${VAULT_DIR:-...}`) ——
#     a bind mount silently creates a missing path, so the screen says "0 documents" (2026-08-25).

# The production stack (a static build · without the override)
up-prod:
    @test -f .env || (echo "  there is no .env.  Run  just env  first." && exit 1)
    @#  ⚠ **The value is checked too.**  Passing on the file merely existing let compose take
    @#     the placeholder `/Users/you/vault` that `just env` left behind, and docker
    @#     **silently created** that path, so the screen said "0 documents" (round 6).
    @{{py}} {{src}}/vault_path.py --check "$(grep -m1 '^VAULT_DIR=' .env | cut -d= -f2-)" \
      || (echo "     Set it with  just vault <notes folder>." && exit 1)
    docker compose -f docker-compose.yml up --build -d

# With a domain and TLS (nginx-proxy + acme)
up-tls:
    @test -f .env || (echo "  there is no .env.  Run  just env  first." && exit 1)
    @#  ⚠ **The value is checked too.**  Passing on the file merely existing let compose take
    @#     the placeholder `/Users/you/vault` that `just env` left behind, and docker
    @#     **silently created** that path, so the screen said "0 documents" (round 6).
    @{{py}} {{src}}/vault_path.py --check "$(grep -m1 '^VAULT_DIR=' .env | cut -d= -f2-)" \
      || (echo "     Set it with  just vault <notes folder>." && exit 1)
    docker compose -f docker-compose.yml --profile tls up --build -d

down:
    docker compose down

logs *args:
    docker compose logs -f {{args}}

# Create .env.  uid/gid have to match the host to write into the mounted ~/.kal.
env:
    @test ! -f .env || (echo "  .env already exists.  To start over, rm .env" && exit 1)
    @sed -e "s|^UID=.*|UID=$(id -u)|" \
         -e "s|^GID=.*|GID=$(id -g)|" \
         -e "s|^KAL_DIR=.*|KAL_DIR=$HOME/.kal|" \
         -e "s|^CLAUDE_DIR=.*|CLAUDE_DIR=$HOME/.claude|" \
         .env.example > .env
    @echo "  ✎ .env — check VAULT_DIR and DOMAIN"

# ── Builds ────────────────────────────────────────────────────────────

# Reclaim Docker disk —— it touches **only what can be rebuilt**.
#
#   Docker Desktop's VM disk (117GB) is **separate** from the Mac's disk.  When it fills, a
#   container cannot write to /tmp and LanceDB dies unable to create its spill directory:
#       panicked … failed to create temp directory for LocalSpillStore: StorageFull
#   That really happened on 2026-08-21 (the host had 46GB free while the overlay had 0).
#
#   The two things deleted here **are rebuilt**:
#     · dangling images —— rebuild leftovers referenced by no tag and no container
#     · the build cache —— the next build refills it (only the first build is slower)
#   Tagged images, volumes and stopped containers are **left alone** —— other projects' things
#   are mixed in and deleting them costs a lot to undo.  That is a person's call.

# Reclaim Docker disk (dangling images · the build cache —— both are rebuilt)
prune:
    @echo "  ── before ──"
    @docker system df
    @docker image prune -f
    @docker builder prune -f
    @echo "  ── after ──"
    @docker system df
    @echo ""
    @echo "  To also delete unused tagged images (undoing needs a rebuild or re-pull):"
    @echo "    docker image prune -a"

build-api:
    #  ⚠ **GOROOT is stripped before calling.**  The asdf golang plugin exports GOROOT into the
    #    environment (set-env.zsh), and when the go earlier on PATH is the homebrew one, the
    #    compiler and tool versions disagree and it dies like this:
    #        compile: version "go1.25.1" does not match go tool version "go1.25.3"
    #    In an interactive shell where the asdf shim comes first it is invisible, and it only
    #    blows up elsewhere (login shells, hooks, CI).  go computes GOROOT from its own binary's
    #    location, so **not passing it is right** —— measured: stripped, both toolchains succeed.
    #  ⚠ `gofmt -l .` **only prints** the divergent files and exits 0.
    #    Chained as `gofmt -l . && go vet`, the format check blocks nothing ——
    #    measured: breaking main.go on purpose still made `just build-api` exit 0.
    cd api && env -u GOROOT sh -c 'f=$(gofmt -l .); if [ -n "$f" ]; then echo "  ❌ gofmt is needed:"; echo "$f"; exit 1; fi; go vet ./... && go test ./... && go build -o /tmp/sb-api .' && echo "  ✅ api"

# The galaxy view bundle as a web-app asset (the graph is not inlined — it comes from /api/graph)
build-galaxy:
    cd plugin && node esbuild.web.mjs --assets ../web/public/galaxy

# build-galaxy comes first —— with web/public/galaxy empty the main screen is entirely blank.
# The docker build COPYs web/ wholesale too, so the asset has to exist on the host to enter the image.

# Build the web front end (the galaxy asset is made first)
build-web: build-galaxy
    # `bun run test` first (`bun test` is bun's own runner and never reaches vitest) —— the same order as plugin's build-plugin.  `web/`
    # had 0 tests for a long time, which is why the 6 usability defects the 2026-08-21 review
    # found (one of them data loss, deleting aliases.yml) were caught by no check at all.
    cd web && bun run test && bunx tsc --noEmit && bun run build

build: build-api build-web

# The plugin + the self-contained viewer
build-plugin:
    cd plugin && npm test && npx tsc -noEmit -skipLibCheck && node esbuild.web.mjs && node esbuild.config.mjs production
