#!/usr/bin/env python3
"""GIT_ASKPASS helper —— hands git the sync token without it ever touching argv, `.git/config`,
or a log line (the installation token is never stored and never printed).

git invokes this as `git_askpass.py 'Username for "https://..."'` / `'Password for "..."'` and
reads its single line of stdout. The token itself travels only through the `KAL_SYNC_TOKEN`
environment variable of the *subprocess git runs this under* — never as a CLI argument, so it
never appears in `ps`, shell history, or a URL that gets persisted to `.git/config`.

# ponytail: an env var is visible to anything running as this uid (e.g. via /proc/<pid>/environ
# on Linux) for the few seconds the git subprocess is alive — a stronger version would pass the
# token over a pipe fd instead. The spec explicitly allows either; env var is the one-file version.
# Upgrade to an fd if a threat model requires hiding it from same-uid processes.
"""
import os
import sys


def answer(prompt):
    if "username" in prompt.lower():
        return "x-access-token"
    return os.environ.get("KAL_SYNC_TOKEN", "")


if __name__ == "__main__":
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    print(answer(prompt))
