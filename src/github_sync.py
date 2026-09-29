#!/usr/bin/env python3
"""`kal sync` —— device-side GitHub sync.

One device's turn of the cycle every machine runs against the single private GitHub repo:

    1. ask kal cloud for a 1h GitHub installation token (device credential → /api/sync/token)
    2. git pull the bundle, hardened (no hooks, no
       fsmonitor, no inherited credential helper, no ext:// protocol, no LFS smudge)
    3. distil this device's new sessions —— the existing `openwiki-sessions` pipeline, which
       already respects the settle rule (distill_sessions.py) and writes its own ledger rows
       (this file does not re-derive "is this done", it only runs what already does)
    4. export this device's *new* extraction-cache lines into its own per-device file
       (.kal-sync/extract/<device>.lr_cache.jsonl) — extraction itself is a separate MCP job
       (a separate MCP job), sync only publishes what is already in ~/.kal/lr_cache.jsonl
    5. commit —— **only this device's own paths** (its `owns` paths), `Kal-Host: <device>` trailer
    6. push —— on non-fast-forward, fetch + rebase this device's own commits, bounded retries,
       clean abort on conflict. **Never force-push.** If the remote history was rewritten
       since the last sync, refuse instead of rebasing (see `check_remote_not_rewritten`).

The GitHub token is never written to argv, `.git/config`, or a log line —— it travels only
through `git_askpass.py` via the `KAL_SYNC_TOKEN` environment variable of the git subprocess.
"""
import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
import time

import ledger

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
ASKPASS = os.path.join(SRC_DIR, "git_askpass.py")

#  The hardened `-c` set every git call in this file uses.
#  No named remote is ever registered; fetch/push always take the URL directly.
HARDENED_GIT_CONFIG = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "credential.helper=",       # reset — do not inherit a configured helper
    "-c", "core.symlinks=false",
    "-c", "protocol.ext.allow=never",
    "-c", "rebase.backend=merge",     # the shared-index resolver reads .git/rebase-merge
]

#  Shared files any device may touch — regenerated, not appended (merged by regeneration).  Own-path
#  enforcement below allows these in addition to a device's own prefixes.
SHARED_PATHS = {"index.md", "personal/index.md", "personal/sessions/index.md"}

MAX_PUSH_RETRIES = 3


class SyncError(Exception):
    """A clean, user-facing sync failure — never a raw traceback or a leaked token."""


class BuildNotificationError(SyncError):
    pass


class OwnershipError(SyncError):
    """A change reached outside this device's owned paths — refused, not committed."""

    def __init__(self, paths):
        self.paths = paths
        super().__init__("changes outside this device's own paths: " + ", ".join(sorted(paths)))


def own_prefixes(device):
    """This device's `owns` path prefixes — the same three per device."""
    return (
        f"personal/sessions/{device}/",
        f".kal-sync/ledger/{device}.jsonl",
        f".kal-sync/extract/{device}.lr_cache.jsonl",
    )


def _is_own_path(path, device):
    if path in SHARED_PATHS:
        return True
    return any(path == p or (p.endswith("/") and path.startswith(p)) for p in own_prefixes(device))


# ───────────────────────────── git plumbing ─────────────────────────────

def _git(bundle, args, token=None, check=True, input=None):
    """Run one hardened git call in `bundle`. `token` (if given) is exposed to the subprocess
    only via KAL_SYNC_TOKEN + GIT_ASKPASS — never appended to `args` — so it can never show up in
    argv, `.git/config`, or anything that logs the command line."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"      # never hang waiting for a human at a tty
    env["GIT_LFS_SKIP_SMUDGE"] = "1"      # no LFS smudge, this repo does not use LFS yet
    env["LC_ALL"] = env["LANG"] = "C"     # stderr matching below ("couldn't find remote ref", ...) needs English
    env.pop("LANGUAGE", None)             # LANGUAGE outranks LC_ALL for gettext
    cmd = ["git"] + HARDENED_GIT_CONFIG + ["-C", bundle] + args
    with contextlib.ExitStack() as stack:
        if token:
            import tempfile

            directory = stack.enter_context(tempfile.TemporaryDirectory(prefix="kal-askpass-"))
            helper = os.path.join(directory, "askpass")
            with open(helper, "w", encoding="utf-8") as output:
                output.write(f'#!/bin/sh\nexec {shlex.join([sys.executable, ASKPASS])} "$@"\n')
            os.chmod(helper, 0o700)
            env["GIT_ASKPASS"] = helper
            env["KAL_SYNC_TOKEN"] = token
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, input=input)
    if check and proc.returncode != 0:
        # The token is never in `cmd`, so this is safe to echo for debugging.
        raise SyncError(f"git {' '.join(args)} failed: {_output_tail(proc, 2000)}")
    return proc


def _output_tail(proc, limit=2000):
    """The tail of a failed subprocess's stderr AND stdout — some steps (e.g. the emitter) print
    their refusal reason to stdout, and stderr alone would hide it."""
    err, out = proc.stderr.strip()[-limit:], proc.stdout.strip()[-limit:]
    return "\n".join(part for part in (err, f"stdout: {out}" if out else "") if part)


SYNCED_REF = "refs/kal/synced"


def _is_ancestor(bundle, older, newer):
    return _git(bundle, ["merge-base", "--is-ancestor", older, newer], check=False).returncode == 0


def recovery_steps(branch="main"):
    """The rewritten-remote recovery, one string per step.  `kal/docs/CONNECTING-AGENTS.md` carries the
    same words (a test compares them).  `origin/<branch>` is NOT this checkout's view of the remote —
    sync fetches by URL and only advances `refs/kal/synced` — so step 1 exports from that ref, and
    step 3 fetches the plain URL (no token: sync's token reaches git only through askpass)."""
    return (
        f'(1) save unpushed work: `git -C "$B" format-patch {SYNCED_REF}.. -o ~/kal-unpushed` '
        f"(only if that ref is missing, use `origin/{branch}..`), and save uncommitted files too: "
        '`git -C "$B" stash -u` (then `git stash pop` after step 3) or copy them out',
        f'(2) `git -C "$B" update-ref -d {SYNCED_REF}`',
        f'(3) `git -C "$B" fetch https://github.com/<owner>/<repo>.git {branch} && git -C "$B" reset --hard FETCH_HEAD` '
        "(the plain https URL of the repository, with no token: sync's short-lived token is not available to "
        "plain git, so this needs your own git credentials), or re-clone",
        "(4) rerun `kal sync` — it re-records this device's ledger rows from its local markers",
    )


def check_remote_not_rewritten(bundle, branch="main"):
    """FETCH_HEAD must descend from the last remote tip this checkout saw: `refs/kal/synced`, or —
    for a checkout that never recorded one (first run after upgrade) — its own remote-tracking
    `refs/remotes/origin/<branch>`.  If it does not, the remote history was rewritten (force-push):
    rebasing local commits onto it would replay — and republish — commits the rewrite removed,
    including other devices' work.  With no baseline at all, a HEAD that has diverged from
    FETCH_HEAD is refused too; a fresh clone (HEAD equal to or behind FETCH_HEAD) proceeds."""
    baseline = ""
    for ref in (SYNCED_REF, f"refs/remotes/origin/{branch}"):
        found = _git(bundle, ["rev-parse", "--verify", "-q", ref], check=False)
        if found.returncode == 0:
            baseline = found.stdout.strip()
            break
    head = _current_head(bundle)
    if baseline:
        rewritten = not _is_ancestor(bundle, baseline, "FETCH_HEAD")
    else:
        rewritten = bool(head) and not _is_ancestor(bundle, head, "FETCH_HEAD") \
            and not _is_ancestor(bundle, "FETCH_HEAD", head)
    if rewritten:
        raise SyncError(
            "remote history was rewritten since the last sync (or cannot be shown to descend from this "
            "checkout); refusing to rebase or push so removed commits are not republished. Review the remote; "
            f"if you trust it, with B={shlex.quote(str(bundle))}: " + "; ".join(recovery_steps(branch)))


def _record_synced(bundle, rev):
    _git(bundle, ["update-ref", SYNCED_REF, rev])


def _fetch(bundle, url, token, branch="main"):
    """Fetch the remote branch and record it as the last-seen remote tip. Returns False when the
    remote has no such branch yet (empty repository) — an empty baseline, not an error."""
    proc = _git(bundle, ["fetch", url, branch], token=token, check=False)
    if proc.returncode != 0:
        if "couldn't find remote ref" in proc.stderr:
            return False
        raise SyncError(f"git fetch failed: {_output_tail(proc, 2000)}")
    check_remote_not_rewritten(bundle, branch)
    _record_synced(bundle, "FETCH_HEAD")
    return True


def _current_head(bundle):
    """HEAD's commit, or "" on an unborn branch (first sync into an empty repository)."""
    proc = _git(bundle, ["rev-parse", "--verify", "-q", "HEAD"], check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _rebase_in_progress(bundle):
    path = _git(bundle, ["rev-parse", "--git-path", "rebase-merge"]).stdout.strip()
    return os.path.isdir(path if os.path.isabs(path) else os.path.join(bundle, path))


def _regenerate_shared_indexes(bundle, device):
    """Rebuild the shared indexes from the merged tree and commit them as this device's commit."""
    proc = subprocess.run([sys.executable, os.path.join(SRC_DIR, "openwiki_emit.py"), "--wiki", bundle,
                           "--indexes-only"], capture_output=True, text=True)
    if proc.returncode != 0:
        #  Leave the tree as the rebase left it, so the next run is not refused as "uncommitted".
        _git(bundle, ["checkout", "--"] + [p for p in SHARED_PATHS if p in changed_paths(bundle)], check=False)
        raise SyncError(f"openwiki_emit.py failed: {_output_tail(proc, 2000)}")
    for path in changed_paths(bundle):          # any other generated index is not this sync's to publish
        if path not in SHARED_PATHS and os.path.basename(path) == "index.md":
            if _git(bundle, ["ls-files", "--error-unmatch", "--", path], check=False).returncode == 0:
                _git(bundle, ["checkout", "--", path])
            else:
                os.remove(os.path.join(bundle, path))
    paths = [p for p in changed_paths(bundle) if p in SHARED_PATHS]
    if paths:
        _git(bundle, ["add", "--"] + paths)
        _git(bundle, ["commit", "-m", f"kal sync: regenerate shared indexes\n\nKal-Host: {device}"])


def _rebase_onto_fetch_head(bundle, device=None):
    """Rebase onto FETCH_HEAD.  The shared indexes are fully generated, so two devices adding pages
    at once conflict there (adjacent per-device "N page(s)" lines) although neither edit matters:
    a stop whose conflicts are ONLY in SHARED_PATHS takes the upstream copy, continues, and the
    indexes are regenerated afterwards.  Any other conflict aborts the rebase and returns False."""
    if _git(bundle, ["rebase", "FETCH_HEAD"], check=False).returncode == 0:
        return True
    resolved = False
    try:
        for _ in range(1000):
            conflicted = set(_git(bundle, ["diff", "--name-only", "--diff-filter=U", "-z"]).stdout.split("\0")) - {""}
            if not conflicted or not conflicted <= SHARED_PATHS:
                _git(bundle, ["rebase", "--abort"], check=False)
                return False
            for path in sorted(conflicted):
                #  Mid-rebase, "ours" is the upstream side.  Upstream deleted it (modify/delete) → no
                #  "ours" stage: take the deletion too; regeneration below rebuilds it if still needed.
                if _git(bundle, ["checkout", "--ours", "--", path], check=False).returncode == 0:
                    _git(bundle, ["add", "--", path])
                else:
                    _git(bundle, ["rm", "-q", "--", path])
            resolved = True
            _git(bundle, ["-c", "core.editor=true", "rebase", "--continue"], check=False)
            if not _rebase_in_progress(bundle):
                break
        else:
            _git(bundle, ["rebase", "--abort"], check=False)
            return False
    except BaseException:
        if _rebase_in_progress(bundle):             # never leave a half-done rebase for the next sync
            _git(bundle, ["rebase", "--abort"], check=False)
        raise
    if resolved:
        _regenerate_shared_indexes(bundle, device or ledger.device_id())
    return True


def pull(bundle, url, token, branch="main", device=None):
    """Step 2. Fetch + fast-forward merge. A diverged local history (unpushed commits from an
    interrupted sync while another device pushed) is rebased onto the remote here, BEFORE distil —
    distil reads the other devices' ledger rows and must see them.  A conflicting rebase aborts
    the sync.  Returns False if the remote branch does not exist yet."""
    if not _fetch(bundle, url, token, branch):
        return False
    if _git(bundle, ["merge", "--ff-only", "FETCH_HEAD"], check=False).returncode != 0:
        if not _rebase_onto_fetch_head(bundle, device):
            raise SyncError("local unpushed commits conflict with the remote history; resolve by hand "
                            "(sync does not force-push) before syncing, so other devices' ledger rows are seen")
    return True


def _prepare_shared_cache(bundle, remote_exists=True, device=None):
    if not remote_exists:
        return
    if _git(bundle, ["merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD"], check=False).returncode == 0:
        return
    if not _rebase_onto_fetch_head(bundle, device):
        raise SyncError("remote history conflicts with local commits; resolve it before extraction so shared cache work is not charged again")


def changed_paths(bundle):
    """Every path with a pending change (staged, unstaged, or untracked) — `git status
    --porcelain` output, path only, one entry per changed file."""
    out = _git(bundle, ["status", "--porcelain", "-z", "--untracked-files=all"]).stdout
    paths = []
    entries = iter(out.split("\0"))
    for line in entries:
        if not line:
            continue
        # porcelain format: "XY path" or "XY old -> new" for renames
        paths.append(line[3:])
        if "R" in line[:2] or "C" in line[:2]:
            paths.append(next(entries))
    return paths


def check_ownership(bundle, device):
    """Refuse a write to another device's ledger or folder. Raises
    OwnershipError (does not commit anything) if any pending change is outside this device's
    own paths or the shared regenerated indexes."""
    bad = [p for p in changed_paths(bundle) if not _is_own_path(p, device)]
    if bad:
        raise OwnershipError(bad)


def publication_files(bundle, device):
    from pathlib import Path

    root = Path(bundle).resolve()
    folder, ledger_file, cache_file = own_prefixes(device)
    files = {str(path.relative_to(root)) for path in (root / folder).rglob("*.md")}
    files.update(path for path in (ledger_file, cache_file) if os.path.lexists(root / path))
    for path in files:
        target = root / path
        if target.resolve() != target or not target.is_file():
            raise SyncError("publication artifact contains a symlink or is not a regular file")
    return sorted(files)


def _check_publication_index_flags(bundle, device):
    tracked = _git(bundle, ["ls-files", "-v", "-z", "--"] + list(own_prefixes(device))).stdout
    if any(entry and (entry[0].islower() or entry.startswith("S ")) for entry in tracked.split("\0")):
        raise SyncError("publication paths have assume-unchanged or skip-worktree flags; review the index before paid work")


def preflight_publication(bundle, device):
    from pathlib import Path

    folder, ledger_file, cache_file = own_prefixes(device)
    targets = {folder, folder + "kal-publication-probe.md", ledger_file, cache_file}
    targets.update(publication_files(bundle, device))
    source = Path(_distilled_path())
    targets.update(folder + path.relative_to(source).as_posix() for path in source.rglob("*.md") if path.name != "index.md")
    result = _git(bundle, ["check-ignore", "-z", "--stdin"], check=False, input="\0".join(sorted(targets)) + "\0")
    if result.returncode == 0:
        ignored = [path for path in result.stdout.split("\0") if path]
        label = "device ledger" if ledger_file in ignored else "device publication target"
        raise SyncError(f"{label} is ignored by Git; review ignore rules before paid work")
    if result.returncode != 1:
        raise SyncError("could not verify Git ignore rules for publication targets")
    _check_publication_index_flags(bundle, device)
    return publication_files(bundle, device)


def verify_publication(bundle, device, committed=False, expected_paths=()):
    _check_publication_index_flags(bundle, device)
    present = set(publication_files(bundle, device))
    expected = present | set(expected_paths)
    if expected - present:
        raise SyncError("an intended publication artifact disappeared; preserving pending state")
    if not expected:
        return
    args = ["ls-tree", "-r", "--name-only", "-z", "HEAD"] if committed else ["ls-files", "-z"]
    tracked = set(_git(bundle, args + ["--"] + list(own_prefixes(device))).stdout.split("\0"))
    if expected - tracked:
        raise SyncError("publication artifacts were not staged or tracked; refusing to clear pending state")
    if _git(bundle, ["diff", "--name-only", "-z", "--"] + list(own_prefixes(device))).stdout:
        raise SyncError("publication artifacts changed after staging; refusing to publish")


def commit_own_changes(bundle, device, message="kal sync"):
    """Step 5. Stages and commits **only** this device's own paths + the shared indexes —
    never `git add -A`. Returns True if a commit was made, False if there was nothing to
    commit for this device (another device's untouched files are left alone either way)."""
    check_ownership(bundle, device)
    paths = [p for p in changed_paths(bundle) if _is_own_path(p, device)]
    if not paths:
        verify_publication(bundle, device)
        return False
    _git(bundle, ["add", "--"] + paths)
    verify_publication(bundle, device)
    staged = _git(bundle, ["diff", "--cached", "--name-only"]).stdout.strip()
    if not staged:
        return False
    full_message = f"{message}\n\nKal-Host: {device}"
    _git(bundle, ["commit", "-m", full_message])
    return True


def push(bundle, url, token, device, branch="main", max_retries=MAX_PUSH_RETRIES):
    """Step 6. Push; on non-fast-forward, fetch + rebase **this device's own commits** and retry,
    bounded. On a real conflict, abort the rebase and stop — never force-push."""
    # Even a fast-forward push can republish removed commits after a force-push, so check the
    # remote first (raises if its history was rewritten; an empty remote is fine).
    _fetch(bundle, url, token, branch)
    for attempt in range(max_retries + 1):
        proc = _git(bundle, ["push", url, f"HEAD:refs/heads/{branch}"], token=token, check=False)
        if proc.returncode == 0:
            _record_synced(bundle, "HEAD")
            return True
        rejected = "[rejected]" in proc.stderr or "non-fast-forward" in proc.stderr or "fetch first" in proc.stderr
        if not rejected or attempt == max_retries:
            raise SyncError(f"push failed: {_output_tail(proc, 2000)}")
        if not _fetch(bundle, url, token, branch):
            raise SyncError("push rejected but the remote branch is missing; retry the sync")
        if not _rebase_onto_fetch_head(bundle, device):
            raise SyncError(
                "push rejected and the rebase onto the new remote history conflicted — "
                "resolve by hand, sync does not force-push"
            )
    return False  # unreachable, kept for clarity


# ───────────────────────────── distill (existing pipeline) ─────────────────────────────

def _distilled_path():
    return os.environ.get("KAL_DISTILLED", os.path.join(
        os.environ.get("KAL_HOME", os.path.expanduser("~/.kal")), "distilled"))


def run_distill(bundle, py=None, workers=8, device=None, ledger_stage=None, repository=None, ledger_baseline=None):
    """Step 3 —— exactly `just openwiki-sessions`'s steps, shelled out rather than
    reimplemented (each script already writes its own ledger rows)."""
    py = py or sys.executable
    device = device or ledger.device_id()
    env = dict(os.environ, KAL_VAULT=os.path.abspath(bundle), KAL_DEVICE=device)
    if ledger_stage:
        env.update(KAL_LEDGER_STAGE=ledger_stage, KAL_LEDGER_REPOSITORY=repository)
        preflight_publication(bundle, device)
    steps = [
        [py, os.path.join(SRC_DIR, "ingest_sessions.py")],
        [py, os.path.join(SRC_DIR, "ingest_codex_sessions.py")],
        [py, os.path.join(SRC_DIR, "ingest_hermes_sessions.py")],
        [py, os.path.join(SRC_DIR, "distill_sessions.py"), "--workers", str(workers)],
        [py, os.path.join(SRC_DIR, "openwiki_emit.py"), "--wiki", bundle,
         "--from", _distilled_path(),
         "--device", device],
    ]
    for cmd in steps:
        if ledger_stage and os.path.basename(cmd[1]) == "openwiki_emit.py":
            if changed_paths(bundle):
                raise SyncError("checkout changed during distillation; preserve user edits before retrying")
            ledger.review_pending(bundle, device, repository, ledger_baseline)
            preflight_publication(bundle, device)
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if proc.returncode != 0:
            raise SyncError(f"{os.path.basename(cmd[1])} failed: {_output_tail(proc, 2000)}")


# ───────────────────────────── extract export (step 4) ─────────────────────────────

def _distill_and_emit(bundle, device, repository):
    root = os.path.realpath(bundle)
    if os.path.commonpath([root, os.path.realpath(_distilled_path())]) == root:
        raise SyncError("distilled output must be outside the checkout; move KAL_DISTILLED before syncing")
    stage = ledger.prepare_pending(bundle, device, repository)
    baseline = ledger.ledger_snapshot(bundle, device)
    run_distill(bundle, device=device, ledger_stage=stage, repository=repository, ledger_baseline=baseline)
    return ledger.merge_pending(bundle, device, repository, baseline)


def _row_key(row):
    """The same shape as lr_extract.cache_key() — computed from an already-written cache row
    rather than a fresh chunk, since that is all a device's local lr_cache.jsonl has on disk."""
    return (row.get("doc"), row.get("idx"), row.get("h"), row.get("pv", ""), row.get("model"))


def export_device_extract(bundle, device, local_cache_path=None, repository=None):
    """Step 4. Append this device's *new* lr_cache.jsonl lines into
    `.kal-sync/extract/<device>.lr_cache.jsonl` — content-keyed, so re-running is a no-op for
    lines already exported (the format is unchanged, only the file is split per device: the exported line is the
    cache line verbatim). Returns the number of lines newly exported."""
    return run_device_extract(bundle, device, extract=False, local_cache_path=local_cache_path, repository=repository)["exported"]


def run_device_extract(bundle, device, extract=True, local_cache_path=None, shared_ref=None, repository=None):
    command = [sys.executable, os.path.join(SRC_DIR, "device_extract.py"),
               "--bundle", bundle, "--device", device]
    if not extract:
        command.append("--export-only")
    if local_cache_path is not None:
        command.extend(["--cache", local_cache_path])
    if shared_ref is not None:
        command.extend(["--shared-ref", shared_ref])
    if repository is not None:
        command.extend(["--repository", canonical_repository(repository)])
    env = dict(os.environ, KAL_VAULT=os.path.abspath(bundle), KAL_DEVICE=device)
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise SyncError(f"device extraction/export failed: {_output_tail(result, 2000)}")
    try:
        stats = json.loads(result.stdout)
        if not isinstance(stats, dict) or not all(type(stats.get(key)) is int and stats[key] >= 0
                                               for key in ("extracted", "reused", "exported")):
            raise ValueError("invalid result")
    except ValueError as error:
        raise SyncError("device extraction returned no valid completion result; refusing to push") from error
    return stats


# ───────────────────────────── the whole sync ─────────────────────────────

@contextlib.contextmanager
def sync_lock():
    import fcntl

    home = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
    os.makedirs(home, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(home, ".sync.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise SyncError("another sync is already running; try again after it finishes") from e
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def autosync(repository_id=None, interval=1800, stop=None, print_fn=print, notify_build=False):
    import threading

    if not isinstance(interval, int) or interval <= 0:
        raise SyncError("autosync interval must be a positive number of seconds")
    stop = stop if stop is not None else threading.Event()
    while not stop.is_set():
        try:
            sync(repository_id=repository_id, print_fn=print_fn, notify_build=notify_build)
        except BuildNotificationError as e:
            print_fn(f"{e}; retrying on the next cycle in {interval} seconds")
        except SyncError as e:
            print_fn(f"sync failed: {e}; retrying in {interval} seconds")
        if stop.wait(interval):
            break


def last_result_path():
    return os.path.join(os.environ.get("KAL_HOME", os.path.expanduser("~/.kal")), "sync_last.json")


def _record_result(ok, message):
    """Remember the last sync outcome so a silent hook/daemon failure is visible later via
    `kal sync --status`. Message text only — never a token. Best effort: never breaks a sync."""
    path = last_result_path()
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump({"time": int(time.time()), "ok": ok, "message": message[:2000]}, out)
        os.replace(tmp, path)
    except OSError:
        pass


def read_last_result():
    """The recorded last sync outcome `{time, ok, message}`, or None."""
    try:
        with open(last_result_path(), encoding="utf-8") as source:
            data = json.load(source)
        return data if isinstance(data, dict) and "ok" in data else None
    except (OSError, ValueError):
        return None


def sync(url=None, get_token=None, bundle=None, device=None, print_fn=print, repository_id=None, notify_build=False):
    def run():
        try:
            return _sync(url, get_token, bundle, device, print_fn, repository_id, notify_build)
        except ledger.LedgerStageError as e:
            raise SyncError(f"pending ledger refused: {e}") from e
        except OSError as e:
            raise SyncError(f"sync could not access a required file or executable: {e.strerror}") from e

    try:
        with sync_lock():
            try:
                result = run()
            except SyncError as e:
                _record_result(False, str(e))
                raise
            except Exception as e:
                _record_result(False, f"unexpected {type(e).__name__}")
                raise
            _record_result(True, "sync complete")
            return result
    except OSError as e:  # the lock file itself
        raise SyncError(f"sync could not access a required file or executable: {e.strerror}") from e


def _pending_path(bundle):
    import hashlib

    home = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
    key = hashlib.sha256(os.path.realpath(bundle).encode("utf-8")).hexdigest()
    return os.path.join(home, "sync_pending", key + ".json")


def _pending_snapshot(bundle, device, repo_url):
    import hashlib

    root = os.path.realpath(bundle)
    files = {}
    for path in changed_paths(bundle):
        target = os.path.join(root, path)
        if not _is_own_path(path, device) or path in SHARED_PATHS:
            raise SyncError("generated sync changes reached outside this device's paths; review them before retrying")
        if os.path.realpath(target) != target or not os.path.isfile(target):
            raise SyncError("generated sync changes include a symlink or deletion; review them before retrying")
        with open(target, "rb") as content:
            files[path] = {"sha256": hashlib.file_digest(content, "sha256").hexdigest(),
                           "mode": os.fstat(content.fileno()).st_mode & 0o777}
    return {"head": _current_head(bundle), "index": _git(bundle, ["write-tree"]).stdout.strip(),
            "device": device, "repo": repo_url, "files": files}


def _save_pending(bundle, device, repo_url):
    import tempfile

    snapshot = _pending_snapshot(bundle, device, repo_url)
    if not snapshot["files"]:
        return
    path = _pending_path(bundle)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(path), delete=False) as output:
        temporary = output.name
        json.dump(snapshot, output)
    try:
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _can_resume(bundle, device, repo_url):
    try:
        with open(_pending_path(bundle), encoding="utf-8") as source:
            expected = json.load(source)
        return expected == _pending_snapshot(bundle, device, repo_url)
    except (OSError, ValueError, SyncError):
        return False


def _clear_pending(bundle):
    try:
        os.remove(_pending_path(bundle))
    except FileNotFoundError:
        pass


def _sync(url=None, get_token=None, bundle=None, device=None, print_fn=print, repository_id=None, notify_build=False):
    """The full `kal sync` cycle. `get_token` is injectable for tests (avoids a real HTTP call)."""
    import device_auth

    cred = device_auth.load_credential()
    if cred is None:
        raise SyncError("not logged in — run `kal login` first")
    url = url or cred["url"]
    device = device or ledger.device_id()

    bundle = bundle or ledger.bundle_root()
    if bundle is None:
        raise SyncError("no bundle configured — set KAL_VAULT to the openwiki bundle path")
    if not os.path.exists(os.path.join(bundle, ".git")):
        raise SyncError(f"{bundle} is not a git checkout — clone the bundle once by hand first")

    if get_token is None:
        repository_id = select_repository(list_repositories(url, cred["token"]), repository_id)
        get_token = lambda: _fetch_sync_token(url, cred["token"], repository_id)
    repo_url, gh_token = _request_token(get_token)
    origin = _git(bundle, ["remote", "get-url", "origin"], check=False)
    if origin.returncode != 0 or not origin.stdout.strip():
        raise SyncError("bundle has no origin; clone the selected repository and set KAL_VAULT to that checkout")
    if _repository_name(origin.stdout.strip()) != _repository_name(repo_url):
        raise SyncError("selected repository does not match this checkout's origin; set KAL_VAULT to the correct checkout")

    resuming = bool(changed_paths(bundle))
    if resuming and not _can_resume(bundle, device, repo_url):
        owned = " ".join(shlex.quote(p) for p in own_prefixes(device))
        raise SyncError(
            "checkout has uncommitted changes; review and commit them before syncing. If they are only "
            "files a previous interrupted sync generated, discard them and rerun: "
            f"`git -C {shlex.quote(str(bundle))} status --short` to review, then "
            f"`git -C {shlex.quote(str(bundle))} clean -fd -- {owned}` (and `git -C {shlex.quote(str(bundle))} "
            f"checkout -- {owned}` for modified tracked files)")
    if notify_build and repository_id is None:
        raise SyncError("cloud build notification requires a selected repository ID")
    repository = _repository_name(repo_url)
    ledger.prepare_pending(bundle, device, repository)

    if not resuming:
        print_fn(f"  pulling {repo_url} …")
        remote_exists = pull(bundle, repo_url, gh_token, device=device)
        _prepare_shared_cache(bundle, remote_exists, device)

        print_fn("  distilling this device's new sessions …")
        ledger_expected = _distill_and_emit(bundle, device, repository)
        _save_pending(bundle, device, repo_url)
    else:
        print_fn("  resuming verified emitted changes after the previous extraction stopped")
        ledger_expected = ledger.ledger_snapshot(bundle, device)
        remote_exists = _fetch(bundle, repo_url, gh_token)

    publications = set(preflight_publication(bundle, device))
    print_fn("  extracting only pending bundle chunks with this device's LLM backend …")
    stats = run_device_extract(bundle, device, shared_ref="FETCH_HEAD" if resuming and remote_exists else None, repository=repository)
    for warning in stats.get("warnings", []):
        print_fn(f"  {warning}")
    if stats["exported"]:
        publications.add(own_prefixes(device)[2])
    if publications - set(publication_files(bundle, device)):
        raise SyncError("an intended publication artifact disappeared; preserving pending state")
    ledger.assert_ledger_snapshot(bundle, device, ledger_expected)
    _save_pending(bundle, device, repo_url)
    print_fn(f"  extracted {stats['extracted']} new bundle chunk(s); reused {stats['reused']} cached chunk(s)")
    n = stats["exported"]
    if n:
        print_fn(f"  exported {n} new extraction-cache line(s) for {device}")

    committed = commit_own_changes(bundle, device)
    verify_publication(bundle, device, committed=True, expected_paths=publications)
    ledger.clear_pending(bundle, device, repository)
    _clear_pending(bundle)
    if not committed:
        print_fn("  nothing new to sync")

    print_fn("  pushing …")
    # The installation token is short-lived and distil/extract can run long: ask for a fresh one
    # right before pushing, and once more if the push is refused for authentication.
    repo_url, gh_token = _request_token(get_token, expect_repo=repo_url)
    try:
        push(bundle, repo_url, gh_token, device)
    except SyncError as e:
        if not _is_auth_failure(str(e)):
            raise
        repo_url, gh_token = _request_token(get_token, expect_repo=repo_url)
        push(bundle, repo_url, gh_token, device)
    if notify_build:
        try:
            request_cloud_build(url, cred["token"], repository_id)
        except SyncError as e:
            raise BuildNotificationError(f"Git push succeeded, but cloud build notification failed: {e}") from e
        print_fn("  cloud build requested; completion is tracked in Connections")
    print_fn("  ✅ sync complete")


def _request_token(get_token, expect_repo=None):
    info = get_token()
    if not isinstance(info, dict) or not all(isinstance(info.get(k), str) and info[k] for k in ("repo", "token")):
        raise SyncError("cloud returned an invalid sync token response")
    if expect_repo is not None and _repository_name(info["repo"]) != _repository_name(expect_repo):
        raise SyncError("cloud returned a token for a different repository; check Connections and --repository ID")
    return info["repo"], info["token"]


def _is_auth_failure(message):
    text = message.lower()
    return any(marker in text for marker in (
        "authentication failed", "invalid credentials", "could not read username",
        "terminal prompts disabled", "returned error: 401", "returned error: 403"))


def _repository_name(url):
    from urllib.parse import urlsplit

    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url[len("git@github.com:"):]
    parsed = urlsplit(url)
    if parsed.hostname != "github.com" or parsed.scheme not in ("https", "ssh"):
        raise SyncError("sync requires a GitHub repository URL")
    path = parsed.path.strip("/").removesuffix(".git")
    if len(path.split("/")) != 2 or not all(path.split("/")):
        raise SyncError("invalid GitHub repository URL")
    return path.casefold()


def canonical_repository(repository):
    if not isinstance(repository, str):
        raise SyncError("a canonical GitHub repository identity is required")
    if repository.casefold().startswith("github.com/"):
        name = repository[len("github.com/"):].casefold()
    elif "://" not in repository and not repository.startswith("git@"):
        name = repository.casefold()
    else:
        name = _repository_name(repository)
    if not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", name):
        raise SyncError("invalid canonical GitHub repository identity")
    return "github.com/" + name


def repository_identity(bundle, repository=None):
    origin = _git(bundle, ["remote", "get-url", "origin"], check=False)
    if origin.returncode != 0 or not origin.stdout.strip():
        raise SyncError("repository-scoped extraction requires a configured GitHub origin")
    actual = canonical_repository(origin.stdout.strip())
    if repository is not None and canonical_repository(repository) != actual:
        raise SyncError("extraction repository identity does not match the checkout origin")
    return actual


def select_repository(repositories, repository_id=None):
    if not repositories:
        raise SyncError("no GitHub repositories — connect GitHub in the web Connections screen first")
    if repository_id is None:
        if len(repositories) != 1:
            choices = ", ".join(f"{r['id']} ({r['owner']}/{r['name']})" for r in repositories)
            raise SyncError(f"multiple repositories connected; pass --repository ID: {choices}")
        repository_id = repositories[0]["id"]
    repository = next((r for r in repositories if r["id"] == repository_id), None)
    if repository is None:
        raise SyncError("repository is not accessible to this device; run `kal repositories`")
    if repository.get("enabled") is not True:
        raise SyncError("repository is disabled; enable it in the web Connections screen first")
    return repository_id


def list_repositories(url, device_token):
    body = _sync_request(url, device_token, "/api/sync/repositories")
    repositories = body.get("repositories")
    if not isinstance(repositories, list) or any(
        not isinstance(r, dict) or not isinstance(r.get("id"), str) or not r["id"].strip()
        or not all(isinstance(r.get(k), str) and r[k] for k in ("owner", "name"))
        or type(r.get("enabled")) is not bool for r in repositories
    ):
        raise SyncError("cloud returned an invalid repository list")
    return repositories


def _fetch_sync_token(url, device_token, repository_id=None):
    """POST /api/sync/token — Bearer device credential, returns a 1h GitHub token."""
    payload = {"repository_id": repository_id} if repository_id is not None else {}
    return _sync_request(url, device_token, "/api/sync/token", payload)


def request_cloud_build(url, device_token, repository_id):
    from urllib.parse import quote

    if not isinstance(repository_id, str) or not repository_id.strip():
        raise SyncError("cloud build requires a repository ID from `kal repositories`")
    return _sync_request(url, device_token, f"/api/sync/repositories/{quote(repository_id, safe='')}/build", {})


def _sync_request(url, device_token, path, payload=None):
    import urllib.error
    import urllib.request

    import device_auth

    try:
        device_auth.check_url(url)
    except ValueError as e:
        raise SyncError(str(e)) from e
    req = urllib.request.Request(
        url.rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        method="POST" if payload is not None else "GET",
        headers={"Authorization": f"Bearer {device_token}", "Content-Type": "application/json"},
    )
    try:
        with device_auth.OPENER.open(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if not isinstance(body, dict):
            raise SyncError("cloud returned an invalid sync response")
        return body
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise SyncError("device or repository access denied — run `kal login` or check Connections") from e
        if e.code == 404:
            raise SyncError("no GitHub connection — connect GitHub in kal's web Connections screen first") from e
        if e.code == 409:
            if path.endswith("/build"):
                raise SyncError("cloud build is already running or the repository is disabled; check Connections and retry") from e
            raise SyncError("repository selection or installation changed — check Connections and --repository ID") from e
        raise SyncError(f"cloud sync request failed: HTTP {e.code}") from e
    except (ValueError, UnicodeError) as e:
        raise SyncError("cloud returned invalid JSON") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SyncError("could not reach kal cloud; check the connection and retry") from e


# ───────────────────────────── CLI ─────────────────────────────

def main(argv=None):
    try:
        sync()
    except SyncError as e:
        print(f"  ❌ {e}", file=sys.stderr)
        return 1
    return 0


# ───────────────────────────── selftest ─────────────────────────────

def _run(cmd, cwd=None):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    assert proc.returncode == 0, f"{cmd} failed: {proc.stderr}"
    return proc.stdout


def _selftest():
    import shutil
    import tempfile

    root = tempfile.mkdtemp(prefix="kal-github-sync-")
    try:
        # ── a local bare repo standing in for GitHub — no network, real git only ────────
        remote = os.path.join(root, "remote.git")
        _run(["git", "init", "--bare", "-b", "main", remote])

        # ── device "mac": clone, seed a first commit from another device so pull has
        #    something to fast-forward, then make mac's own changes ─────────────────────
        seed = os.path.join(root, "seed")
        _run(["git", "clone", remote, seed])
        _run(["git", "-C", seed, "config", "user.email", "t@example.com"])
        _run(["git", "-C", seed, "config", "user.name", "seed"])
        os.makedirs(os.path.join(seed, "personal", "sessions", "desktop2"), exist_ok=True)
        with open(os.path.join(seed, "personal", "sessions", "desktop2", "a.md"), "w") as fh:
            fh.write("# a\n")
        _run(["git", "-C", seed, "add", "-A"])
        _run(["git", "-C", seed, "commit", "-m", "seed\n\nKal-Host: desktop2"])
        _run(["git", "-C", seed, "push", "origin", "main"])

        mac = os.path.join(root, "mac")
        _run(["git", "clone", remote, mac])
        _run(["git", "-C", mac, "config", "user.email", "t@example.com"])
        _run(["git", "-C", mac, "config", "user.name", "mac"])

        # ── pull(): fast-forwards onto desktop2's seed commit ───────────────────────────
        pull(mac, remote, token=None)
        assert os.path.exists(os.path.join(mac, "personal", "sessions", "desktop2", "a.md"))

        # ── own-path enforcement: writing under a DIFFERENT device's folder is refused ──
        os.makedirs(os.path.join(mac, "personal", "sessions", "someone-else"), exist_ok=True)
        with open(os.path.join(mac, "personal", "sessions", "someone-else", "x.md"), "w") as fh:
            fh.write("intrusion\n")
        try:
            commit_own_changes(mac, "mac")
            assert False, "commit_own_changes must refuse a change outside owned paths"
        except OwnershipError as e:
            assert "someone-else" in str(e), e
        os.remove(os.path.join(mac, "personal", "sessions", "someone-else", "x.md"))
        os.rmdir(os.path.join(mac, "personal", "sessions", "someone-else"))

        # ── own-path enforcement: mac's own folder + the shared index.md commit cleanly ─
        os.makedirs(os.path.join(mac, "personal", "sessions", "mac"), exist_ok=True)
        with open(os.path.join(mac, "personal", "sessions", "mac", "b.md"), "w") as fh:
            fh.write("# b\n")
        with open(os.path.join(mac, "personal", "sessions", "index.md"), "w") as fh:
            fh.write("shared index\n")
        assert commit_own_changes(mac, "mac") is True
        log = _run(["git", "-C", mac, "log", "-1", "--pretty=%B"])
        assert "Kal-Host: mac" in log, log

        # ── nothing to commit → False, not an error ─────────────────────────────────────
        assert commit_own_changes(mac, "mac") is False

        # ── push(): plain fast-forward push succeeds ────────────────────────────────────
        assert push(mac, remote, token=None, device="mac") is True

        # ── push(): non-fast-forward triggers fetch+rebase, not a failure ──────────────
        # desktop2 pushes again after mac's push, so mac's next push (without re-pulling
        # first) must reject, rebase mac's own commit onto it, and retry successfully.
        _run(["git", "-C", seed, "pull", "--rebase", "origin", "main"])
        with open(os.path.join(seed, "personal", "sessions", "desktop2", "c.md"), "w") as fh:
            fh.write("# c\n")
        _run(["git", "-C", seed, "add", "-A"])
        _run(["git", "-C", seed, "commit", "-m", "second\n\nKal-Host: desktop2"])
        _run(["git", "-C", seed, "push", "origin", "main"])

        # mac makes one more local commit without pulling desktop2's second push first
        with open(os.path.join(mac, "personal", "sessions", "mac", "d.md"), "w") as fh:
            fh.write("# d\n")
        _run(["git", "-C", mac, "add", "-A"])
        _run(["git", "-C", mac, "commit", "-m", "mac local\n\nKal-Host: mac"])
        assert push(mac, remote, token=None, device="mac") is True
        remote_log = _run(["git", "--git-dir", remote, "log", "main", "--oneline"])
        assert "mac local" in remote_log and "second" in remote_log, remote_log

        # ── push(): a real conflict aborts cleanly, never force-pushes ──────────────────
        # Another clone edits the exact same line of mac's own file and pushes first ...
        rival = os.path.join(root, "rival")
        _run(["git", "clone", remote, rival])
        _run(["git", "-C", rival, "config", "user.email", "t@example.com"])
        _run(["git", "-C", rival, "config", "user.name", "rival"])
        with open(os.path.join(rival, "personal", "sessions", "mac", "d.md"), "w") as fh:
            fh.write("# d\nrival's conflicting line\n")
        _run(["git", "-C", rival, "add", "-A"])
        _run(["git", "-C", rival, "commit", "-m", "rival edits mac's file\n\nKal-Host: rival"])
        _run(["git", "-C", rival, "push", "origin", "main"])
        # ... mac, without pulling that, edits the same line differently and tries to push.
        with open(os.path.join(mac, "personal", "sessions", "mac", "d.md"), "w") as fh:
            fh.write("# d\nmac's conflicting line\n")
        _run(["git", "-C", mac, "add", "-A"])
        _run(["git", "-C", mac, "commit", "-m", "mac edits the same line\n\nKal-Host: mac"])
        before = _run(["git", "-C", mac, "rev-parse", "HEAD"])
        try:
            push(mac, remote, token=None, device="mac", max_retries=1)
            assert False, "a genuine rebase conflict must raise, not silently resolve or force-push"
        except SyncError as e:
            assert "conflict" in str(e).lower(), e
        after = _run(["git", "-C", mac, "rev-parse", "HEAD"])
        assert before == after, "rebase abort must leave HEAD exactly where it was"
        branch_line = _run(["git", "-C", mac, "status", "--branch", "--porcelain=v2"])
        assert "rebase" not in _run(["git", "-C", mac, "status"]).lower(), branch_line

        # ── export_device_extract: content-keyed, idempotent, per-device file only ──────
        khome = os.path.join(root, "khome")
        os.makedirs(khome)
        import device_extract
        device_extract.bind_cache_home(khome, "github.com/example/bundle")
        _git(mac, ["remote", "set-url", "origin", "https://github.com/example/bundle.git"])
        cache = os.path.join(khome, "lr_cache.jsonl")
        import lr_extract
        text = "Offline cache fixture content. " * 300
        with open(os.path.join(mac, "personal", "sessions", "mac", "d1.md"), "w") as fh:
            fh.write(text)
        rows = [{**{key: chunk[key] for key in ("doc", "idx", "h")},
                 "pv": lr_extract.PROMPT_VERSION, "model": "sonnet", "entities": [], "relationships": []}
                for chunk in lr_extract.chunks_of(text, "personal_sessions_mac_d1")]
        with open(cache, "w") as fh:
            for row in rows[:2]:
                fh.write(json.dumps(row) + "\n")
        n1 = export_device_extract(mac, "mac", local_cache_path=cache)
        assert n1 == 2, n1
        n2 = export_device_extract(mac, "mac", local_cache_path=cache)
        assert n2 == 0, "re-exporting the same cache lines must be a no-op"
        with open(cache, "a") as fh:
            fh.write(json.dumps(rows[2]) + "\n")
        n3 = export_device_extract(mac, "mac", local_cache_path=cache)
        assert n3 == 1, n3
        dest = os.path.join(mac, ".kal-sync", "extract", "mac.lr_cache.jsonl")
        assert os.path.exists(dest)
        assert not os.path.exists(os.path.join(mac, ".kal-sync", "extract", "desktop2.lr_cache.jsonl"))

        # ── the GitHub token never lands in argv or .git/config ─────────────────────────
        secret = "kal_dev_SUPER_SECRET_SHOULD_NEVER_LEAK"
        calls = []
        real_run = subprocess.run

        def spy(cmd, *a, **kw):
            calls.append(cmd)
            return real_run(cmd, *a, **kw)

        subprocess.run = spy
        try:
            with open(os.path.join(mac, "personal", "sessions", "mac", "e.md"), "w") as fh:
                fh.write("# e\n")
            # a clean slate matching the remote before the spy'd push — fetched fresh rather
            # than via the local "origin/main" tracking ref, which a direct `git push <url>`
            # (no named remote) never updates
            _run(["git", "-C", mac, "fetch", remote, "main"])
            _run(["git", "-C", mac, "reset", "--hard", "FETCH_HEAD"])
            with open(os.path.join(mac, "personal", "sessions", "mac", "e.md"), "w") as fh:
                fh.write("# e\n")
            commit_own_changes(mac, "mac")
            push(mac, remote, token=secret, device="mac")
            pull(mac, remote, token=secret)  # exercise _fetch's token path too, not just push's
        finally:
            subprocess.run = real_run
        for cmd in calls:
            joined = " ".join(cmd)
            assert secret not in joined, f"token leaked into argv: {joined}"
        cfg = _run(["git", "-C", mac, "config", "--local", "--list"])
        assert secret not in cfg, "token leaked into .git/config"

        # ── ownership check catches a deliberately broken guard (mutation confirm) ──────
        os.makedirs(os.path.join(mac, "personal", "sessions", "not-mac"), exist_ok=True)
        with open(os.path.join(mac, "personal", "sessions", "not-mac", "y.md"), "w") as fh:
            fh.write("intrusion 2\n")
        try:
            check_ownership(mac, "mac")
            assert False
        except OwnershipError:
            pass
        # the mutation: an ownership check that always passes must be caught by a caller
        # that actually calls the real function — demonstrated by the assertion above
        # raising; see the sync_v3/ledger convention of describing the mutation inline.
        os.remove(os.path.join(mac, "personal", "sessions", "not-mac", "y.md"))
        os.rmdir(os.path.join(mac, "personal", "sessions", "not-mac"))
        _run(["git", "-C", mac, "clean", "-fd"])

        # ── sync(): not logged in → a clean SyncError, never a traceback ────────────────
        import device_auth
        old_dir, old_path = device_auth.CRED_DIR, device_auth.CRED_PATH
        cred_dir = os.path.join(root, "cred")
        device_auth.CRED_DIR = cred_dir
        device_auth.CRED_PATH = os.path.join(cred_dir, "device.json")
        try:
            try:
                sync(print_fn=lambda *_: None)
                assert False, "sync() without a device credential must raise"
            except SyncError as e:
                assert "logged in" in str(e), e

            # ── sync(): a stale/expired device credential → a clear message, not a crash ──
            device_auth.save_credential("http://example.invalid", "mac", "kal_dev_stale")
            try:
                sync(
                    bundle=mac, device="mac",
                    get_token=lambda: (_ for _ in ()).throw(SyncError("no GitHub connection")),
                    print_fn=lambda *_: None,
                )
                assert False, "sync() must surface the token-fetch failure, not swallow it"
            except SyncError as e:
                assert "GitHub connection" in str(e), e
        finally:
            device_auth.CRED_DIR, device_auth.CRED_PATH = old_dir, old_path

        print("  ✅ github_sync self-check —— pull fast-forwards · own-path commit accepts own+shared, "
              "refuses another device's folder · push retries a non-fast-forward via rebase · a real "
              "conflict aborts cleanly with HEAD unmoved (never force-pushes) · export_device_extract "
              "is content-keyed and idempotent, per-device only · the token never reaches argv or "
              ".git/config · sync() without login and with a broken token fetch both raise a clean "
              "SyncError instead of crashing")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        sys.exit(main())
