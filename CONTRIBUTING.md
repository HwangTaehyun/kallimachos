# Contributing

## CLA — once, on your first PR

This repository requires signing the **[CLA](https://gist.github.com/HwangTaehyun/fd42a3ed0bb566d176a273d4fd339159)**. Open your first PR and a bot will comment.
It is **one click with your GitHub account** — no paperwork, no email. Every PR after that passes
automatically.

**Why we ask for it** — copyright in the code you contribute stays with **you** by default. In that
state the project cannot later be offered under a commercial licence, and changing the licence
would mean re-obtaining consent from every contributor. The CLA keeps that door open.

**You lose nothing** — the copyright remains yours, and you stay free to reuse your contribution
however and wherever you like (see [CLA](https://gist.github.com/HwangTaehyun/fd42a3ed0bb566d176a273d4fd339159) §2, "What this means in plain terms").

> If you are not comfortable with the fact that this project **may be licensed commercially**, do
> not sign. The [CLA](https://gist.github.com/HwangTaehyun/fd42a3ed0bb566d176a273d4fd339159) spells out that difference under "How this differs from the
> Apache ICLA".

---

## Development setup

```bash
#  ① Dependencies — skip this and every command below fails with `.venv/bin/python: No such file`
KAL_VAULT=~/my-notes just setup   # uv sync · creates .env.  You must choose a vault
#     or:  just init              # a screen that lets you pick the folder

#  ② Web and plugin dependencies (the pre-PR checks need them)
cd web && bun install && cd ..
cd plugin && npm ci && cd ..

#  ③ To run it in containers
just up
```

⚠ **`.env` is read by docker compose only.** Host commands such as `just index` look at your
**shell environment** and `~/.kal/config.json` — `just vault <path>` sets all three together. This
is the trap people hit most often. Please read [`README.md`](README.md) § "Where `.env` goes"
first.

⚠ **Do not simply copy `.env.example` as-is.** `VAULT_DIR=/Users/you/vault` is a placeholder, and
a Docker bind mount will **silently create** a directory that does not exist, leaving the UI
showing "0 documents". `just up` checks that value and refuses to start.

---

## Before opening a PR

```bash
just selftest-py                              # every self-check that needs no Docker or live DB
cd web && bunx tsc --noEmit && bun run test   # web      (bun install first)
cd plugin && npm test                         # plugin   (npm ci first)
cd api && go test ./...                       # Go
just verify-links                             # doc links, citations, folder listings
```

These five are **the same** thing CI runs (the `checks` job) — green here means green there.

- **Comments say *why*.** The code already says what. Most comments in this repository record
  "what broke when we did not do it this way"; please keep that habit.
- **When you cite a measurement, give the file and line** (like `docs/STACK.md:294`).
- Commit messages may be in Korean or English.

## What not to send

This repository handles a personal knowledge database, so **real names and secrets get mixed in
easily.**

Do not work around what `.gitignore` already blocks — `bench/` (real names in the answer key),
`viewer/` (person and organization names in plain text in generated output),
`docs/entity_resolve_groups.txt` (real-name entities), `.env` (the relay token).

**Never put real vault content into a test fixture.**
