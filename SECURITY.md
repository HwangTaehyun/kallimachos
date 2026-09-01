# Reporting a security issue

**Do not open a public issue.** An unfixed vulnerability made public hurts the people using it in
the meantime.

Please open a private report through GitHub
[Security advisories](https://github.com/HwangTaehyun/kallimachos/security/advisories/new).
If that is not possible, contact the repository owner directly.

## Where the risk in this tool actually is

This tool handles personal notes, so **confidentiality** matters most. Three boundaries in
particular get scrutiny.

| Boundary | What is at stake | Where |
|---|---|---|
| **Egress boundary** | Which notes leave this machine via `claude -p` | `src/schema_v3.py` (`is_skipped`) · `src/lr_extract.py` (`NO_LLM`) · `llm_gate` |
| **Local web UI** | There is **no authentication.** Loopback binding plus the `Host` / `Origin` / `Sec-Fetch-Site` checks are the whole defence | `api/main.go` (`guard`, `allowedHost`) |
| **Masking** | Whether credentials in session logs are removed before indexing | `src/ingest_sessions.py` |

The MCP server is stdio-only and opens no network listener. Containers run with
`--network none --read-only --cap-drop ALL` (`.mcp.json`).

## What we do not treat as a vulnerability

- **The fact that the local web UI has no authentication.** That is the design — it binds to
  loopback only. If you want to expose it on a domain, put authentication **in front of it first**
  (`docs/ARCHITECTURE.md`).
- **The two containers that mount `docker.sock`** — `proxy` in the `proxy` profile
  (`docker-compose.yml:138`) and `acme` in the `tls` profile (`:158`). Both amount to host root
  (`:ro` only makes the socket **file** read-only; Docker API calls still go through). Enabling
  them is opt-in and documented in both places. Neither is in the default profile.
- Strings like `sk-ant-…` inside the repository — those are **synthetic values** for the masking
  tests.

## When fixing

A fix comes with a **reproduction**. That is how this repository works: reproduce the attack
first, fix it, show that the same attack is now blocked, and leave that check behind in the
self-tests (see `api/guard_test.go`).
