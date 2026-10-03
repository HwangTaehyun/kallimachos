import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys


class ExtractionError(Exception):
    pass


def collect_bundle(bundle):
    import lr_extract
    import schema_v3

    root = Path(bundle).resolve()
    if not root.is_dir() or not (root / ".git").exists():
        raise ExtractionError("device extraction requires a bundle Git checkout")
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name != ".git"]
        for name in dirs + files:
            if (Path(directory) / name).is_symlink():
                raise ExtractionError("bundle contains a symlink; review it before extraction or cache export")
    previous = lr_extract.VAULT, schema_v3.VAULT
    lr_extract.VAULT = schema_v3.VAULT = str(root)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            return lr_extract.collect()
    finally:
        lr_extract.VAULT, schema_v3.VAULT = previous


def _identity(row):
    return row["doc"], row["idx"], row["h"], row.get("pv", "")


def result_identity(row):
    fields = ("repository", "doc", "idx", "h", "pv", "model", "entities", "relationships")
    return hashlib.sha256(json.dumps({key: row.get(key) for key in fields}, sort_keys=True,
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def current_rows(rows):
    return {_identity(row): row for row in rows}


def repository_identity(bundle, repository=None):
    import github_sync

    try:
        return github_sync.repository_identity(bundle, repository)
    except github_sync.SyncError as error:
        raise ExtractionError(str(error)) from error


def bind_cache_home(home, repository, adopt_existing=False):
    import github_sync

    repository = github_sync.canonical_repository(repository)
    home = Path(home).absolute()
    if home.exists() and home.is_symlink():
        raise ExtractionError("repository cache home must not be a symlink")
    marker = home / ".repository.json"
    expected = {"version": 1, "repository": repository}
    if marker.is_symlink():
        raise ExtractionError("repository cache binding must not be a symlink")
    if marker.exists():
        try:
            bound = json.loads(marker.read_text(encoding="utf-8"))
        except (ValueError, OSError) as error:
            raise ExtractionError("repository cache binding is unreadable; review before extraction") from error
        if bound != expected:
            raise ExtractionError("cache home is bound to a different repository; choose its scoped KAL_HOME")
        return home
    if not adopt_existing and any(home.glob("lr_cache*.jsonl")):
        raise ExtractionError("unscoped legacy cache cannot be published; explicitly bind a reviewed cache home first")
    home.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(expected, output)
    return home


def scoped_cache_home(repository, home=None):
    import github_sync

    repository = github_sync.canonical_repository(repository)
    base = Path(home or os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))).resolve()
    if (base / ".repository.json").exists() or (base / ".repository.json").is_symlink():
        return bind_cache_home(base, repository)
    scope = base / "repositories" / hashlib.sha256(repository.encode()).hexdigest()
    if scope.resolve() != scope:
        raise ExtractionError("repository cache scope contains a symlink")
    return bind_cache_home(scope, repository)


def _rows(path=None, text=None):
    import io
    import lr_extract

    if text is None:
        if not path.exists():
            return []
        if path.is_symlink() or not path.is_file():
            raise ExtractionError("extraction cache must be a regular file")
        stream = path.open(encoding="utf-8")
    else:
        stream = io.StringIO(text)
    rows = []
    with stream as source:
        while line := source.readline(2_000_001):
            if len(line) > 2_000_000:
                raise ExtractionError("extraction cache contains an oversized row")
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as error:
                raise ExtractionError("extraction cache contains invalid JSON; review it before syncing") from error
            if not isinstance(row, dict):
                raise ExtractionError("extraction cache contains an invalid row")
            if row.get("failed") or "h" not in row:
                continue
            if not (isinstance(row.get("doc"), str) and row["doc"]
                    and type(row.get("idx")) is int and row["idx"] >= 0
                    and isinstance(row.get("h"), str) and row["h"]
                    and isinstance(row.get("pv", ""), str)
                    and isinstance(row.get("model", lr_extract.MODEL), str)
                    and lr_extract.valid_shape(row)):
                raise ExtractionError("extraction cache contains an invalid result shape")
            bounded = lr_extract.publishable_shape(row)      # server contract: see lr_extract
            clean = {"doc": row["doc"], "idx": row["idx"], "h": row["h"],
                     "pv": row.get("pv", ""), "model": row.get("model", lr_extract.MODEL),
                     "at": str(row.get("at", ""))[:32],
                     "entities": bounded["entities"], "relationships": bounded["relationships"]}
            for key in ("repository", "revision", "origin_device", "import_id", "import_result"):
                if key in row:
                    clean[key] = row[key]
            if (("repository" in clean and not isinstance(clean["repository"], str))
                    or ("revision" in clean and (type(clean["revision"]) is not int or clean["revision"] < 0))
                    or ("origin_device" in clean and not isinstance(clean["origin_device"], str))):
                raise ExtractionError("extraction cache provenance is invalid")
            if any(key in clean and (not isinstance(clean[key], str) or not re.fullmatch(r"[0-9a-f]{64}", clean[key]))
                   for key in ("import_id", "import_result")):
                raise ExtractionError("extraction cache receipt is invalid")
            if clean.get("import_id") and clean.get("import_result") != result_identity(clean):
                clean.pop("import_id", None)
                clean.pop("import_result", None)
            rows.append(clean)
    return rows


def _pulled_rows(bundle, ref):
    import github_sync

    tree = github_sync._git(bundle, ["ls-tree", "-r", "-z", ref, "--", ".kal-sync/extract"], check=False)
    if tree.returncode != 0:
        raise ExtractionError("could not inspect the pulled extraction cache snapshot")
    streams = {}
    for entry in tree.stdout.split("\0"):
        if not entry:
            continue
        metadata, path = entry.split("\t", 1)
        if Path(path).parent.as_posix() != ".kal-sync/extract" or not path.endswith(".lr_cache.jsonl"):
            continue
        mode, kind, oid = metadata.split()
        if kind != "blob" or mode not in ("100644", "100755"):
            raise ExtractionError("pulled extraction cache is not a regular file")
        blob = github_sync._git(bundle, ["cat-file", "blob", oid], check=False)
        if blob.returncode != 0:
            raise ExtractionError("could not read a pulled extraction cache file")
        streams[Path(path).name] = _rows(text=blob.stdout)
    return streams


def _event_id(row, device, position):
    return hashlib.sha256(json.dumps([row["repository"], device, position,
                                     row["revision"], result_identity(row)]).encode()).hexdigest()


def _revision_order(row):
    return row.get("revision", 0), row.get("origin_device", ""), row.get("import_id", "")


def merge_shared_rows(streams, repository, report=None):
    import github_sync

    repository = github_sync.canonical_repository(repository)
    ordered = []
    ignored = 0
    for name, rows in sorted(streams.items()):
        device = name.removesuffix(".lr_cache.jsonl")
        if not name.endswith(".lr_cache.jsonl") or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", device):
            raise ExtractionError("shared extraction filename does not identify a device")
        clocks = {}
        for position, row in enumerate(rows):
            if row.get("repository") != repository:
                ignored += 1
                continue
            if row.get("origin_device", device) != device:
                raise ExtractionError("shared extraction row has a foreign device origin")
            revision = row.get("revision", 0)
            if type(revision) is not int or revision < 0:
                raise ExtractionError("shared extraction revision is invalid")
            key = _identity(row)
            revision = max(revision, clocks.get(key, 0) + 1)
            clocks[key] = revision
            exported = {**row, "revision": revision, "origin_device": device}
            exported["import_id"] = _event_id(exported, device, position)
            exported["import_result"] = result_identity(exported)
            ordered.append(exported)
    if ignored:
        message = f"Ignored {ignored} unscoped or foreign-repository shared cache row(s)"
        if report is None:
            print(message, file=sys.stderr)
        else:
            report(message)
    return sorted(ordered, key=_revision_order)


def read_shared_rows(bundle, repository, shared_ref=None, report=None):
    repository = repository_identity(bundle, repository)
    directory = Path(bundle) / ".kal-sync/extract"
    streams = {path.name: _rows(path) for path in sorted(directory.glob("*.lr_cache.jsonl"))}
    if shared_ref:
        for name, pulled in _pulled_rows(bundle, shared_ref).items():
            local = streams.get(name, [])
            overlap = min(len(local), len(pulled))
            if local[:overlap] != pulled[:overlap]:
                raise ExtractionError("shared cache history diverged; review the append-only history before retrying")
            if len(pulled) >= len(local):
                streams[name] = pulled
    return merge_shared_rows(streams, repository, report=report)


def _masked(value):
    """Mask every string in an extraction result with the ingest masker.  The model can re-create a
    credential-shaped string the masked page never contained (2026-09-30: a page about masking tests
    came back as `postgresql://user:…@` in an entity description), and one such cached row stopped
    every later export."""
    from ingest_sessions import mask

    if isinstance(value, str):
        return mask(value)[0]
    if isinstance(value, list):
        return [_masked(item) for item in value]
    if isinstance(value, dict):
        return {key: _masked(item) for key, item in value.items()}
    return value


def _append(path, rows):
    from ingest_sessions import find_leaks

    if not rows:
        return
    rows = [{**row, "entities": _masked(row.get("entities")), "relationships": _masked(row.get("relationships"))}
            if isinstance(row, dict) and ("entities" in row or "relationships" in row) else row for row in rows]
    encoded = [json.dumps(row, ensure_ascii=False) + "\n" for row in rows]
    if any(find_leaks(line) for line in encoded):
        raise ExtractionError("extraction results failed secret masking verification; nothing was exported")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as output:
        for line in encoded:
            output.write(line)
            output.flush()


def _check_backend(pending, cache_home):
    import claude_cli
    import lr_extract

    wanted = {f"{c['doc']}:{c['idx']}:{c['h']}" for c in pending}
    jobs = {path for home in (Path(lr_extract.KAL_HOME), cache_home)
            for path in (home / "extract_jobs").glob("*.json")}
    for path in jobs:
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ExtractionError("could not inspect an existing agent extraction job; review it before syncing") from error
        keys = job.get("all_keys", job.get("pending_chunks", {})) if isinstance(job, dict) else None
        if not isinstance(keys, (list, dict)) or not all(isinstance(key, str) for key in keys):
            raise ExtractionError("an existing agent extraction job is malformed; review it before syncing")
        if not job.get("finished_at") and wanted.intersection(keys):
            raise ExtractionError("an agent extraction job already owns pending chunks; finish that job before syncing")
    if not claude_cli.RELAY and not shutil.which("claude"):
        raise ExtractionError("device extraction backend unavailable: sign in to the existing Claude CLI, configure your own KAL_CLAUDE_RELAY, or finish extraction with the agent MCP tools")


@contextlib.contextmanager
def _cache_lock(home):
    import fcntl
    import stat

    fd = os.open(home / ".write.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise ExtractionError("repository cache lock is not a regular file")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ExtractionError("repository extraction cache is busy; finish the scoped agent writer first") from error
        try:
            lock.seek(0)
            lock.truncate()
            lock.write(f"device_extract {os.getpid()}\n")
            lock.flush()
            yield
        finally:
            lock.seek(0)
            lock.truncate()
            lock.flush()
            fcntl.flock(lock, fcntl.LOCK_UN)


def process(bundle, device, extract=True, local_cache_path=None, shared_ref=None, repository=None):
    import github_sync
    import kal_lock

    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", device):
        raise ExtractionError("invalid device slug")
    try:
        with contextlib.ExitStack() as locks:
            locks.enter_context(kal_lock.db_lock("device_extract", on_conflict="raise"))
            return _process(bundle, device, extract, local_cache_path, shared_ref, repository_identity(bundle, repository), locks)
    except kal_lock.LockBusy as error:
        raise ExtractionError("the local extraction cache is busy; retry after the other writer finishes") from error
    except github_sync.SyncError as error:
        raise ExtractionError(str(error)) from error


def _process(bundle, device, extract, local_cache_path, shared_ref, repository, locks):
    import github_sync
    import kal_lock
    import lr_extract

    github_sync.preflight_publication(bundle, device)
    chunks = collect_bundle(bundle)
    live = {(c["doc"], c["idx"], c["h"]) for c in chunks}
    if local_cache_path is not None:
        cache = Path(local_cache_path).absolute()
        if not (cache.parent / ".repository.json").is_file():
            raise ExtractionError("unscoped explicit cache refused; bind a reviewed cache home before publishing")
        home = bind_cache_home(cache.parent, repository)
    else:
        home = scoped_cache_home(repository)
        cache = home / "lr_cache.jsonl"
    if Path(bundle).resolve() in cache.resolve().parents:
        raise ExtractionError("repository extraction cache must live outside the checkout")
    if (home / ".write.lock").resolve() != Path(kal_lock.LOCK_PATH).resolve():
        locks.enter_context(_cache_lock(home))
    ignored_legacy = int(Path(lr_extract.CACHE).exists() and Path(lr_extract.CACHE).resolve() != cache.resolve()
                         and not (Path(lr_extract.CACHE).parent / ".repository.json").exists())
    warnings = []
    if ignored_legacy:
        warnings.append(f"Unscoped legacy cache ignored; repository agents must use KAL_HOME={home}")
    local = []
    for row in _rows(cache):
        if row.get("repository", repository) != repository:
            raise ExtractionError("local cache contains a foreign-repository row; refusing publication")
        if (row["doc"], row["idx"], row["h"]) in live and row["pv"] == lr_extract.PROMPT_VERSION:
            local.append({**row, "repository": repository})
    shared = [row for row in read_shared_rows(bundle, repository, shared_ref, report=warnings.append)
              if (row["doc"], row["idx"], row["h"]) in live and row["pv"] == lr_extract.PROMPT_VERSION]
    for warning in warnings:
        print(warning, file=sys.stderr)
    known = {row.get("import_id") for row in local}
    imported = [row for row in shared if row["import_id"] not in known]
    before = current_rows(local)
    ranked = current_rows(sorted([row for row in local if row.get("import_id")] + shared, key=_revision_order))
    incoming = {row["import_id"] for row in imported}
    desired = dict(before)
    for key, row in ranked.items():
        if key not in desired or row.get("import_id") in incoming or desired[key].get("import_id"):
            desired[key] = row
    after_import = current_rows(local + imported)
    restore = [row for key, row in desired.items() if after_import.get(key) != row]
    _append(cache, imported + restore)
    local.extend(imported + restore)
    current = current_rows(local)
    pending = list({lr_extract._done_key(c): c for c in chunks
                    if lr_extract._done_key(c) not in current}.values())
    extracted = []
    if extract and pending:
        _check_backend(pending, home)
        for chunk in pending:
            try:
                row = lr_extract.call(chunk)
            except Exception as error:
                raise ExtractionError("device extraction backend failed or refused a chunk; no commit or push was attempted") from error
            if row.get("failed") or not lr_extract.valid_shape(row):
                raise ExtractionError("device entity extraction failed; check your CLI subscription, authentication or relay and retry; successful chunks remain cached")
            row = {**lr_extract.publishable_shape(row), "repository": repository}
            _append(cache, [row])
            local.append(row)
            extracted.append(row)
    fresh = {(c["doc"], c["idx"], c["h"]) for c in collect_bundle(bundle)}
    if fresh != live:
        raise ExtractionError("bundle content changed during extraction; cached work was retained but nothing was exported")
    github_sync.preflight_publication(bundle, device)
    directory = Path(bundle) / ".kal-sync/extract"
    destination = directory / f"{device}.lr_cache.jsonl"
    prior = _rows(destination)
    shared_current = current_rows(shared)
    delta = []
    receipts = []
    clocks = {}
    for row in local + shared:
        key = _identity(row)
        clocks[key] = max(clocks.get(key, 0), row.get("revision", 0))
    for key, row in current_rows(local).items():
        previous = shared_current.get(key)
        if previous and result_identity(previous) == result_identity(row):
            if not row.get("import_id"):
                receipts.append(previous)
            continue
        revision = clocks.get(key, 0) + 1
        published = {**row, "repository": repository, "revision": revision, "origin_device": device}
        published.pop("import_id", None)
        published.pop("import_result", None)
        receipt = {**published, "import_id": _event_id(published, device, len(prior) + len(delta)),
                   "import_result": result_identity(published)}
        delta.append(published)
        receipts.append(receipt)
    _append(destination, delta)
    _append(cache, receipts)
    return {"chunks": len(chunks), "extracted": len(extracted), "reused": len(chunks) - len(pending),
            "imported": len(imported), "exported": len(delta), "ignored_unscoped_cache": ignored_legacy,
            "cache_home": str(home), "repository": repository, "warnings": warnings}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Extract pending bundle chunks with the existing device LLM backend and export a scoped delta")
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--export-only", action="store_true")
    parser.add_argument("--cache", default=None)
    parser.add_argument("--shared-ref", choices=["FETCH_HEAD"], default=None)
    parser.add_argument("--repository", default=None)
    args = parser.parse_args(argv)
    os.environ["KAL_VAULT"] = str(Path(args.bundle).resolve())
    try:
        result = process(args.bundle, args.device, extract=not args.export_only,
                         local_cache_path=args.cache, shared_ref=args.shared_ref, repository=args.repository)
    except (ExtractionError, OSError, ImportError) as error:
        print(f"device extraction/export stopped: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
