#!/usr/bin/env python3
"""The one writer/reader of kal's cross-device progress ledger.

DESIGN-GITHUB-SYNC.md §3 —— a document's "how far did we get" answer used to live in at least
three places (a `.done` marker, `lr_cache.jsonl` itself, `lr_kg.json.doc_hashes`) and none of them
were visible outside the machine that wrote them.  This file is the single place that answers it
now, backed by one append-only JSONL per device under the bundle: `.kal-sync/ledger/<device>.jsonl`
(§3.2).  A line is a document's state; `(source_type, source_id)` repeated in a later line — same
device or another — wins over an earlier one, exactly like `pick_current()` already does for
extraction cache lines (§2.3, §3.3: "the same function, extended").

⚠ **Every writer in this pipeline calls `append()`, every reader calls `status_of()`/`read_all()`.**
   Re-deriving "is this done" anywhere else is precisely the mistake §3.1 documents kal already made
   three times over.

Never writes to the DB and never calls an LLM —— this is a side file next to the openwiki bundle,
not a schema change to ~/.kal/db.
"""
import os, json, re, socket

#  Where the bundle lives.  KAL_VAULT is the name every other file in this pipeline already uses
#  for "the openwiki bundle / vault checkout" (kal_mcp.py, lr_extract.py, eval_sessions.py, …) ——
#  reused rather than inventing a second env var for the same directory.
LEDGER_SUBDIR = os.path.join(".kal-sync", "ledger")


def bundle_root():
    """The openwiki bundle's root directory, or None if none is configured.

    None (not an exception) on purpose: a device that has not connected to GitHub sync yet still
    runs `distill_sessions.py`/`schema_v3.py` today, and every writer in this file treats "no
    bundle configured" as "nothing to append to" rather than a hard failure — sync is additive.
    """
    v = os.environ.get("KAL_VAULT") or os.environ.get("VAULT_DIR")
    return os.path.abspath(v) if v else None


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def device_id():
    """This machine's device name for the `<device>.jsonl` filename and the `device` field.

    Precedence: `KAL_DEVICE` (explicit override, e.g. for tests) → the name recorded by
    `kal login` (`~/.config/kal/device.json`'s `device_name`, DESIGN-GITHUB-SYNC.md §5.2 — the
    same name this device's `owns` prefixes and `Kal-Host:` trailer use in `github_sync.py`) →
    the hostname (fallback for a device that has not logged in yet, e.g. running the pre-sync
    local pipeline). A device once logged in stays identified by its login name even if the
    hostname later changes.
    """
    raw = os.environ.get("KAL_DEVICE")
    if not raw:
        try:
            import device_auth
            cred = device_auth.load_credential()
            if cred:
                raw = cred.get("device_name")
        except Exception:
            raw = None
    raw = raw or socket.gethostname().split(".")[0]
    slug = _SLUG_RE.sub("-", raw.lower()).strip("-")
    return slug or "unknown"


def ledger_path(device=None, root=None):
    root = root if root is not None else bundle_root()
    if root is None:
        return None
    return os.path.join(root, LEDGER_SUBDIR, f"{device or device_id()}.jsonl")


def append(row, device=None, root=None):
    """Append one row to this device's ledger file.  Returns the written row (with `seq` and
    `device` filled in), or None if no bundle is configured (see bundle_root()).

    `seq` is the row's 0-based line number in the file — the tiebreaker §3.2 defines for same-
    device rows sharing a `(source_type, source_id)` (a continuation page's second row must
    outrank its first).  It is derived by counting existing lines, which is exactly right for a
    single device appending to its own file one run at a time.
    # ponytail: not safe against two processes on the SAME device appending concurrently (a
    # classic read-count-then-write race) — the sync design has one `kal sync` per device running
    # at a time, so this is not exercised yet.  If concurrent writers on one device appear, take
    # an flock (kal_lock.py already has one) around the read-count-then-append.
    """
    if os.environ.get("KAL_LEDGER_STAGE"):
        return _append_pending(row, device or device_id(), root or bundle_root())
    path = ledger_path(device, root)
    if path is None:
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    seq = 0
    if os.path.exists(path):
        with open(path, "rb") as fh:
            seq = sum(1 for _ in fh)
    out = dict(row)
    out["seq"] = seq
    out["device"] = device or device_id()
    line = json.dumps(out, ensure_ascii=False, sort_keys=True) + "\n"
    #  One O_APPEND write() per row: the kernel places it at the end of the file in one step.  (PIPE_BUF
    #  is a pipe guarantee and says nothing about files; rows carrying `pages` can pass 512 bytes.  Two
    #  writers on one device's file are not supported anyway — see the note above.)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return out


def append_many(rows_in, device=None, root=None):
    """`append` for a batch.  In sync's staged mode the pending file is rewritten once, not per row."""
    if not rows_in:
        return []
    if not os.environ.get("KAL_LEDGER_STAGE"):
        return [append(row, device, root) for row in rows_in]
    device, root = device or device_id(), root or bundle_root()
    repository = os.environ.get("KAL_LEDGER_REPOSITORY")
    identity = _pending_identity(root, device, repository)
    path = pending_path(device, repository)
    if os.environ["KAL_LEDGER_STAGE"] != path:
        raise LedgerStageError("pending ledger path does not match the configured repository and device")
    rows = staged = _read_pending(path, identity)
    result = []
    for row in rows_in:
        out = {**row, "device": device, "seq": len(rows)}
        same = next((p for p in rows if p.keys() == out.keys()
                     and all(p.get(k) == v for k, v in out.items() if k != "seq")), None)
        if same is None:
            rows = rows + [out]
        result.append(same or out)
    if rows is not staged:
        _write_pending(path, identity, rows)
    return result


class LedgerStageError(RuntimeError):
    """Refuse changed staging state or a conflicting append to the device ledger."""


def _pending_identity(root, device, repository):
    if (not root or not isinstance(device, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", device)
            or not isinstance(repository, str) or not repository):
        raise LedgerStageError("pending ledger requires a bundle, repository and valid device identity")
    return {"bundle": os.path.realpath(root), "device": device, "repository": repository}


def pending_path(device, repository):
    import hashlib

    home = os.path.realpath(os.environ.get("KAL_HOME", os.path.expanduser("~/.kal")))
    key = hashlib.sha256(json.dumps([repository, device]).encode()).hexdigest()
    return os.path.join(home, "sync_ledger", key + ".json")


def _private_pending_path(path, directory=False):
    import stat

    metadata = os.lstat(path)
    if (os.path.realpath(path) != path or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != (0o700 if directory else 0o600)
            or not (stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode))
            or (not directory and metadata.st_nlink != 1)):
        raise LedgerStageError("pending ledger permissions or file identity changed; review before retrying")


def _pending_digest(identity, rows):
    import hashlib

    return hashlib.sha256(json.dumps({"identity": identity, "rows": rows}, sort_keys=True,
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _validate_pending_rows(rows, device):
    required = {"source_type", "source_id", "content_hash", "source_updated_at",
                "distilled_through_last_ts", "no_llm", "device", "seq"}
    if not isinstance(rows, list):
        raise LedgerStageError("pending ledger rows are invalid")
    for seq, row in enumerate(rows):
        if (not isinstance(row, dict) or not required <= row.keys() or row.keys() - required - {"continues", "pages"}
                or row["device"] != device or type(row["seq"]) is not int or row["seq"] != seq
                or not isinstance(row["source_type"], str) or not row["source_type"].endswith("_session")
                or not isinstance(row["source_id"], str) or not row["source_id"]
                or len(row["source_id"]) > 512 or any(c in row["source_id"] for c in ("/", "\\", "\0"))
                or not isinstance(row["content_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["content_hash"])
                or type(row["no_llm"]) is not bool
                or type(row["distilled_through_last_ts"]) not in (type(None), int, float)
                or type(row["source_updated_at"]) not in (type(None), str, int, float)
                or ("continues" in row and row["continues"] is not None and not isinstance(row["continues"], str))
                or ("pages" in row and not (isinstance(row["pages"], list)
                                            and all(isinstance(p, str) for p in row["pages"])))):
            raise LedgerStageError("pending ledger contains an unrecognised or foreign row")


def _read_pending(path, identity):
    _private_pending_path(os.path.dirname(path), directory=True)
    _private_pending_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, encoding="utf-8") as source:
            _private_pending_path(path)
            metadata = os.fstat(source.fileno())
            current = os.stat(path, follow_symlinks=False)
            if any(getattr(metadata, field) != getattr(current, field)
                   for field in ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink")):
                raise LedgerStageError("pending ledger file identity changed while opening")
            data = json.load(source)
        if not isinstance(data, dict) or data.keys() != {"identity", "rows", "digest"}:
            raise LedgerStageError("pending ledger envelope is invalid")
        if data["identity"] != identity:
            raise LedgerStageError("pending ledger repository, checkout or device identity changed")
        rows = data["rows"]
        _validate_pending_rows(rows, identity["device"])
        if data.get("digest") != _pending_digest(identity, rows):
            raise LedgerStageError("pending ledger content changed outside the writer; review before retrying")
        return rows
    except (ValueError, KeyError, TypeError) as error:
        raise LedgerStageError("pending ledger data is invalid; review before retrying") from error


def _write_pending(path, identity, rows):
    import tempfile

    _private_pending_path(os.path.dirname(path), directory=True)
    if os.path.lexists(path):
        _private_pending_path(path)
    _validate_pending_rows(rows, identity["device"])
    data = {"identity": identity, "rows": rows, "digest": _pending_digest(identity, rows)}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(path), delete=False) as output:
            temporary = output.name
            json.dump(data, output, ensure_ascii=False, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.remove(temporary)


def prepare_pending(root, device, repository):
    """Create or validate private repository/device staging, outside the checkout."""
    identity = _pending_identity(root, device, repository)
    path = pending_path(device, repository)
    if os.path.commonpath([identity["bundle"], path]) == identity["bundle"]:
        raise LedgerStageError("pending ledger must be outside the checkout; move KAL_HOME before syncing")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    _private_pending_path(os.path.dirname(path), directory=True)
    if os.path.lexists(path):
        _read_pending(path, identity)
    else:
        _write_pending(path, identity, [])
    return path


def _append_pending(row, device, root):
    return append_many([row], device, root)[0]


def ledger_snapshot(root, device):
    import stat

    path = ledger_path(device, os.path.realpath(root))
    if os.path.realpath(path) != path:
        raise LedgerStageError("device ledger contains a symlink; review before syncing")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != os.getuid():
            raise LedgerStageError("device ledger is not an owned regular file")
        return {"data": source.read(), "mode": stat.S_IMODE(metadata.st_mode), "inode": metadata.st_ino,
                "filesystem": metadata.st_dev, "owner": metadata.st_uid, "group": metadata.st_gid}


def assert_ledger_snapshot(root, device, expected):
    if ledger_snapshot(root, device) != expected:
        raise LedgerStageError("device ledger bytes, mode or file identity changed during the pipeline; preserve user edits")


def review_pending(root, device, repository, expected):
    """Review an append-only delta against unchanged ledger bytes and file identity."""
    assert_ledger_snapshot(root, device, expected)
    rows = _read_pending(pending_path(device, repository), _pending_identity(root, device, repository))
    original = expected["data"] if expected else b""
    lines = original.splitlines()
    try:
        existing = [json.loads(line) for line in lines if line.strip()]
    except ValueError as error:
        raise LedgerStageError("existing device ledger contains invalid JSON; review before merging") from error
    if any(not isinstance(row, dict) or row.get("device", device) != device for row in existing):
        raise LedgerStageError("existing device ledger contains foreign rows; review before merging")
    delta = []
    for row in rows:
        fields = {key: value for key, value in row.items() if key != "seq"}
        if any(all(prior.get(key, device if key == "device" else None) == value for key, value in fields.items())
               for prior in existing):
            continue
        for prior in existing:
            if (prior.get("source_type"), prior.get("source_id")) != (row["source_type"], row["source_id"]):
                continue
            progress = prior.get("distilled_through_last_ts")
            if type(progress) in (int, float) and progress >= (row["distilled_through_last_ts"] or 0):
                raise LedgerStageError("pending ledger conflicts with existing reviewed progress; no rows were replaced")
        written = {**fields, "seq": len(lines) + len(delta)}
        delta.append(json.dumps(written, ensure_ascii=False, sort_keys=True).encode() + b"\n")
        existing.append(written)
    return (b"\n" if delta and original and not original.endswith(b"\n") else b"") + b"".join(delta)


def merge_pending(root, device, repository, expected):
    """Append reviewed rows, preserving existing bytes and permissions; replay is idempotent."""
    suffix = review_pending(root, device, repository, expected)
    if not suffix:
        return expected
    path = ledger_path(device, os.path.realpath(root))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    assert_ledger_snapshot(root, device, expected)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | (os.O_CREAT | os.O_EXCL if expected is None else 0), 0o600)
    with os.fdopen(fd, "ab") as output:
        metadata = os.fstat(output.fileno())
        if expected is not None and (metadata.st_ino, metadata.st_dev, metadata.st_mode & 0o7777,
                                     metadata.st_uid, metadata.st_gid, metadata.st_nlink) != (
                expected["inode"], expected["filesystem"], expected["mode"], expected["owner"], expected["group"], 1):
            raise LedgerStageError("device ledger file identity changed before append")
        output.write(suffix)
        output.flush()
        os.fsync(output.fileno())
    merged = ledger_snapshot(root, device)
    if merged["data"] != (expected["data"] if expected else b"") + suffix:
        raise LedgerStageError("device ledger changed concurrently; review before committing")
    return merged


def pending_keys():
    """`(source_type, source_id)` of every row already staged in this sync run ([] outside one)."""
    if not os.environ.get("KAL_LEDGER_STAGE"):
        return set()
    identity = _pending_identity(bundle_root(), device_id(), os.environ.get("KAL_LEDGER_REPOSITORY"))
    return {(row["source_type"], row["source_id"])
            for row in _read_pending(os.environ["KAL_LEDGER_STAGE"], identity)}


def clear_pending(root, device, repository):
    path = pending_path(device, repository)
    if os.path.lexists(path):
        identity = _pending_identity(root, device, repository)
        _read_pending(path, identity)
        _write_pending(path, identity, [])


def read_all(root=None):
    """Every row in every `<device>.jsonl` under the bundle's ledger dir.  [] if unconfigured or
    the ledger dir does not exist yet (a fresh bundle, or a device that never synced)."""
    root = root if root is not None else bundle_root()
    if root is None:
        return []
    ledger_dir = os.path.join(root, LEDGER_SUBDIR)
    if not os.path.isdir(ledger_dir):
        return []
    rows = []
    for name in sorted(os.listdir(ledger_dir)):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(ledger_dir, name), encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    row = json.loads(ln)
                except ValueError:
                    continue
                row.setdefault("device", name[: -len(".jsonl")])
                rows.append(row)
    return rows


def _rank(row):
    """Sort key for "which row of a (source_type, source_id) group is the latest" — highest wins.
    Prefers the document's own progress markers (how far distillation/extraction reached, or when
    the source itself last changed) over `seq`, since `seq` alone cannot order two *different*
    devices' rows against each other — only a device's own rows against themselves."""
    return (row.get("distilled_through_last_ts") or 0,
            row.get("source_updated_at") or "",
            row.get("seq", -1),
            row.get("device", ""))


def latest_rows(root=None):
    """The latest row of every `(source_type, source_id)`, across every device — read once."""
    best = {}
    for row in read_all(root):
        key = (row.get("source_type"), row.get("source_id"))
        if key not in best or _rank(row) > _rank(best[key]):
            best[key] = row
    return best


def pages_of(source_type, source_id, root=None):
    """The `pages` of the newest row for this source that lists any — for a newest row that lost them."""
    rows = [r for r in read_all(root) if (r.get("source_type"), r.get("source_id")) == (source_type, source_id)
            and isinstance(r.get("pages"), list) and r["pages"]]
    return max(rows, key=_rank)["pages"] if rows else []


def status_of(source_type, source_id, root=None):
    """The latest ledger row for `(source_type, source_id)`, across every device — or None if no
    row names this document yet.  The one function every reader (CLI `kal status`, `kal web`,
    `kal_extract_begin`) is meant to call instead of re-deriving "is this done" (§3.3)."""
    return latest_rows(root).get((source_type, source_id))


def _selftest():
    import tempfile, shutil
    d = tempfile.mkdtemp(prefix="kal-ledger-")
    try:
        # ── append: seq increments, device stamped ──────────────────────────────────────
        r0 = append({"source_type": "claude_session", "source_id": "s1"}, device="mac", root=d)
        r1 = append({"source_type": "claude_session", "source_id": "s1"}, device="mac", root=d)
        assert r0["seq"] == 0 and r1["seq"] == 1, (r0, r1)
        assert r0["device"] == r1["device"] == "mac", (r0, r1)

        # ── two devices' files don't collide, both readable ─────────────────────────────
        append({"source_type": "claude_session", "source_id": "s2"}, device="desktop2", root=d)
        rows = read_all(root=d)
        assert len(rows) == 3, rows
        assert {r["device"] for r in rows} == {"mac", "desktop2"}, rows

        # ── status_of: a later same-device row (continuation) outranks the first ────────
        append({"source_type": "claude_session", "source_id": "s1", "distilled_through_last_ts": 100},
                device="mac", root=d)
        append({"source_type": "claude_session", "source_id": "s1", "distilled_through_last_ts": 200},
                device="mac", root=d)
        st = status_of("claude_session", "s1", root=d)
        assert st["distilled_through_last_ts"] == 200, st
        assert st["seq"] == 3, "status_of did not pick the later of two same-device rows"

        # ── status_of: cross-device, the row with the later progress marker wins even when its
        #    file has a LOWER seq than a busier device's (seq only orders a device against
        #    itself — a raw max-seq comparison would wrongly favour the busier device) ────────
        for i in range(10):                   # pad desktop2 so its seq for s3 dwarfs mac's
            append({"source_type": "claude_session", "source_id": f"filler{i}"}, device="desktop2", root=d)
        append({"source_type": "claude_session", "source_id": "s3", "distilled_through_last_ts": 10},
                device="desktop2", root=d)    # desktop2's row for s3: high seq, OLD progress
        append({"source_type": "claude_session", "source_id": "s3", "distilled_through_last_ts": 500},
                device="mac", root=d)         # mac's row for s3: low seq, NEWER progress
        st3 = status_of("claude_session", "s3", root=d)
        assert st3["device"] == "mac" and st3["distilled_through_last_ts"] == 500, \
            f"a higher seq on a busier device's stale row outranked the actually-later progress: {st3}"

        # ── status_of: unknown document is None, not a guess ────────────────────────────
        assert status_of("claude_session", "does-not-exist", root=d) is None

        # ── unconfigured bundle: every function degrades to "nothing", never an exception ─
        assert append({"source_type": "x", "source_id": "y"}, root=None if bundle_root() else d) \
               is not None or bundle_root() is None
        old = os.environ.pop("KAL_VAULT", None)
        old2 = os.environ.pop("VAULT_DIR", None)
        try:
            assert bundle_root() is None
            assert append({"source_type": "x", "source_id": "y"}) is None
            assert read_all() == []
            assert status_of("x", "y") is None
        finally:
            if old is not None:
                os.environ["KAL_VAULT"] = old
            if old2 is not None:
                os.environ["VAULT_DIR"] = old2

        # ── device_id: sanitised, deterministic under KAL_DEVICE override ───────────────
        os.environ["KAL_DEVICE"] = "Taehyun's MacBook Pro.local"
        try:
            assert device_id() == "taehyun-s-macbook-pro-local", device_id()
        finally:
            del os.environ["KAL_DEVICE"]

        # ── device_id: a `kal login` credential outranks the hostname, KAL_DEVICE outranks it ──
        import device_auth
        old_dir, old_path = device_auth.CRED_DIR, device_auth.CRED_PATH
        cred_dir = os.path.join(d, "cred")
        device_auth.CRED_DIR = cred_dir
        device_auth.CRED_PATH = os.path.join(cred_dir, "device.json")
        try:
            device_auth.save_credential("https://app.kallimachos.dev", "Work Desktop", "kal_dev_x")
            assert device_id() == "work-desktop", device_id()
            os.environ["KAL_DEVICE"] = "override"
            try:
                assert device_id() == "override", "KAL_DEVICE must still outrank a login credential"
            finally:
                del os.environ["KAL_DEVICE"]
        finally:
            device_auth.CRED_DIR, device_auth.CRED_PATH = old_dir, old_path

        print("  ✅ ledger self-check —— append seq/device stamped · two devices merge in read_all "
              "· status_of picks the latest row across devices · unconfigured bundle degrades to "
              "no-op everywhere · device_id sanitises, and prefers a kal login credential over the "
              "hostname while KAL_DEVICE still outranks both")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print(__doc__)
