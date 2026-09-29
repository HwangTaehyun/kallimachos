import collections
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SRC = Path(__file__).resolve().parent
REPOSITORY = "https://github.com/example/bundle.git"


class StagedLedgerSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kal-staged-ledger-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.state = self.home / "state"
        self.bundle = self.root / "bundle"
        self.remote = self.root / "remote.git"
        self.binary = self.root / "bin"
        for directory in (self.home, self.state, self.bundle, self.binary):
            directory.mkdir()
        self.real_git = shutil.which("git")
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("KAL_", "GIT_", "ANTHROPIC_", "CLAUDE_"))
                    and key not in ("PYTHONPATH", "PYTHONHOME", "VAULT_DIR")}
        self.env.update(HOME=str(self.home), KAL_HOME=str(self.state), KAL_VAULT=str(self.bundle),
                        KAL_DEVICE="test-device", PYTHONPATH=str(SRC),
                        GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
                        HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                        PATH=str(self.binary) + os.pathsep + self.env.get("PATH", ""))
        if os.environ.get("KAL_TEST_MUTATION"):
            self.env["KAL_TEST_MUTATION"] = os.environ["KAL_TEST_MUTATION"]
        self.git(self.bundle, "init", "-b", "main")
        self.git(self.bundle, "config", "user.name", "fixture")
        self.git(self.bundle, "config", "user.email", "fixture@example.invalid")
        self.own = self.bundle / ".kal-sync/ledger/test-device.jsonl"
        self.other = self.bundle / ".kal-sync/ledger/other-device.jsonl"
        self.own.parent.mkdir(parents=True)
        self.seed = json.dumps({"source_type": "obsidian", "source_id": "human-reviewed", "seq": 0,
                                "device": "test-device", "no_llm": True}) + "\n"
        self.own.write_text(self.seed)
        self.other.write_text('{"device":"other-device","source_id":"preserve-me"}\n')
        (self.bundle / "index.md").write_text("Human index\n")
        self.git(self.bundle, "add", ".")
        self.git(self.bundle, "commit", "-m", "seed")
        self.git(self.root, "init", "--bare", "-b", "main", str(self.remote))
        self.git(self.bundle, "push", str(self.remote), "HEAD:main")
        self.git(self.bundle, "remote", "add", "origin", REPOSITORY)
        self.head = self.git(self.bundle, "rev-parse", "HEAD").strip()
        credentials = self.home / ".config/kal/device.json"
        credentials.parent.mkdir(parents=True, mode=0o700)
        credentials.write_text(json.dumps({"url": "https://example.invalid", "device_name": "test-device", "token": "fake-device"}))
        credentials.chmod(0o600)
        raw = self.home / ".claude/projects/fixture-project"
        raw.mkdir(parents=True)
        for name in ("GOOD", "BAD"):
            (raw / (name.lower() + ".jsonl")).write_text(json.dumps({
                "type": "user", "timestamp": "2020-01-01T00:00:00Z",
                "message": {"content": f"SESSION_{name} " + "We settled an offline design decision and preserved the reasoning for future review. " * 30},
            }) + "\n")
        self.settings = self.home / "model-fixture.json"
        self.settings.write_text(json.dumps({"bad_fails": True, "extract_fails": False}))
        fake_cli = self.binary / "claude"
        fake_cli.write_text(f"#!{sys.executable}\n" + '''
import json
import os
from pathlib import Path
import sys
home = Path(os.environ["HOME"])
settings = json.loads((home / "model-fixture.json").read_text())
prompt = sys.stdin.read()
extract = "You extract a knowledge graph from text." in prompt
label = "extract" if extract else ("GOOD_TAIL" if "TAIL_GREW" in prompt else ("GOOD" if "SESSION_GOOD" in prompt else "BAD"))
fd = os.open(home / "model-calls.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
os.write(fd, (json.dumps(label) + "\\n").encode())
os.close(fd)
if label == "GOOD" and settings.get("tamper_ledger"):
    target = Path(settings["ledger_path"])
    if settings["tamper_ledger"] == "mode":
        target.chmod(0o600)
    else:
        replacement = target.with_suffix(".temporary")
        replacement.write_bytes(target.read_bytes())
        os.replace(replacement, target)
if extract:
    print("{}" if settings["extract_fails"] else json.dumps({"entities":[{"name":"Offline decision", "type":"decision", "description":"Preserve successful work"}],"relationships":[]}))
elif (label == "BAD" and settings["bad_fails"]) or (label == "GOOD_TAIL" and settings.get("tail_fails")):
    print("invalid fixture response")
else:
    print(f"<<<DOC>>>\\ntitle: {label.title()} note\\ndoc_type: decision\\nwhy_captured: Settled offline conclusion\\ntags: test\\n---\\nThis {label.lower()} decision preserves completed work and only retries unfinished sessions without overwriting reviewed notes.\\n<<<END>>>")
''')
        fake_cli.chmod(0o700)
        git_transport = self.binary / "git"
        git_transport.write_text(f"#!{sys.executable}\n" + f'''
import os
import sys
import json
from pathlib import Path
args = sys.argv[1:]
if os.environ.get("KAL_TEST_MUTATION") == "ignore-preflight" and "check-ignore" in args:
    raise SystemExit(1)
settings = json.loads((Path(os.environ["HOME"]) / "model-fixture.json").read_text())
if settings.get("drop_cache_stage") and "add" in args:
    args = [arg for arg in args if arg != ".kal-sync/extract/test-device.lr_cache.jsonl"]
if "fetch" in args or "push" in args:
    args = [{str(self.remote)!r} if arg == {REPOSITORY!r} else arg for arg in args]
    if any(arg.startswith(("https://", "http://", "ssh://", "git@")) for arg in args):
        raise SystemExit("network transport prohibited in this fixture")
os.execv({self.real_git!r}, [{self.real_git!r}, *args])
''')
        git_transport.chmod(0o700)

    def git(self, root, *args):
        return subprocess.run([self.real_git, "-C", str(root), *args], env=self.env, check=True,
                              capture_output=True, text=True).stdout

    def sync(self):
        code = f'''
import github_sync
try:
    github_sync.sync(get_token=lambda: {{"repo": {REPOSITORY!r}, "token": "fake-installation"}})
except github_sync.SyncError as error:
    print(str(error))
    raise SystemExit(1)
'''
        return subprocess.run([sys.executable, "-c", code], env=self.env,
                              capture_output=True, text=True, timeout=90)

    def counts(self):
        path = self.home / "model-calls.jsonl"
        return collections.Counter(json.loads(line) for line in path.read_text().splitlines()) if path.exists() else collections.Counter()

    def fail_first(self):
        result = self.sync()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("distillation incomplete", result.stdout + result.stderr)
        self.assertEqual(self.git(self.bundle, "status", "--porcelain"), "")
        self.assertEqual(self.git(self.bundle, "rev-parse", "HEAD").strip(), self.head)
        self.assertEqual(self.git(self.remote, "rev-parse", "main").strip(), self.head)
        self.assertEqual(self.own.read_text(), self.seed)
        self.assertTrue((self.state / "distilled/.done/claude-good").exists())
        self.assertFalse((self.state / "distilled/.done/claude-bad").exists())
        stages = list((self.state / "sync_ledger").glob("*.json"))
        self.assertEqual(len(stages), 1)
        self.assertEqual(stages[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(stages[0].parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual([row["source_id"] for row in json.loads(stages[0].read_text())["rows"]], ["good"])
        self.assertFalse(list((self.state / "sync_pending").glob("*.json")))
        return stages[0]

    def test_partial_distillation_retries_cleanly_and_preserves_remote_rows(self):
        stage = self.fail_first()
        pending_before = stage.read_bytes()
        good = self.state / "distilled/good-note.md"
        marker = self.state / "distilled/.done/claude-good"
        before = good.read_bytes(), good.stat().st_mtime_ns, marker.read_bytes(), marker.stat().st_mtime_ns
        other_before = self.other.read_bytes()
        peer = self.root / "peer"
        self.git(self.root, "clone", str(self.remote), str(peer))
        self.git(peer, "config", "user.name", "fixture")
        self.git(peer, "config", "user.email", "fixture@example.invalid")
        additional = '{"source_type":"obsidian","source_id":"remote-reviewed","seq":1,"device":"test-device","no_llm":true}\n'
        (peer / ".kal-sync/ledger/test-device.jsonl").write_text(self.seed + additional)
        self.git(peer, "add", ".")
        self.git(peer, "commit", "-m", "reviewed remote progress")
        self.git(peer, "push", "origin", "main")
        self.settings.write_text(json.dumps({"bad_fails": False, "extract_fails": False}))
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(before, (good.read_bytes(), good.stat().st_mtime_ns, marker.read_bytes(), marker.stat().st_mtime_ns))
        self.assertEqual(self.counts(), {"GOOD": 1, "BAD": 2, "extract": 2})
        self.assertTrue(self.own.read_text().startswith(self.seed + additional))
        rows = [json.loads(line) for line in self.own.read_text().splitlines()]
        self.assertEqual([row["seq"] for row in rows], list(range(4)))
        self.assertEqual({row["source_id"] for row in rows[2:]}, {"good", "bad"})
        self.assertEqual(self.other.read_bytes(), other_before)
        self.assertEqual(self.git(self.bundle, "status", "--porcelain"), "")
        self.assertEqual(self.git(self.bundle, "rev-parse", "HEAD").strip(), self.git(self.remote, "rev-parse", "main").strip())
        self.assertEqual(json.loads(stage.read_text())["rows"], [])
        self.assertTrue((self.bundle / "personal/sessions/test-device/good-note.md").is_file())
        self.assertTrue((self.bundle / "personal/sessions/test-device/bad-note.md").is_file())
        snapshot = self.own.read_bytes()
        stage.write_bytes(pending_before)
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.counts(), {"GOOD": 1, "BAD": 2, "extract": 2})
        self.assertEqual(self.own.read_bytes(), snapshot)

    def test_continuation_failure_keeps_both_completed_sessions_and_retries_only_tail(self):
        self.fail_first()
        good = self.state / "distilled/good-note.md"
        marker = self.state / "distilled/.done/claude-good"
        before = good.read_bytes(), good.stat().st_mtime_ns, marker.read_bytes()
        raw = self.home / ".claude/projects/fixture-project/good.jsonl"
        with raw.open("a") as output:
            output.write(json.dumps({"type": "user", "timestamp": "2020-01-02T00:00:00Z", "message": {
                "content": "TAIL_GREW " + "We settled a further decision after the original session ended. " * 40}}) + "\n")
        self.settings.write_text(json.dumps({"bad_fails": False, "extract_fails": False, "tail_fails": True}))
        result = self.sync()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("0 session(s) and 1 continuation(s) failed", result.stdout + result.stderr)
        self.assertEqual(before, (good.read_bytes(), good.stat().st_mtime_ns, marker.read_bytes()))
        self.assertTrue((self.state / "distilled/.done/claude-bad").exists())
        self.assertEqual(self.git(self.bundle, "status", "--porcelain"), "")
        self.assertEqual(self.git(self.remote, "rev-parse", "main").strip(), self.head)
        self.assertEqual(self.counts(), {"GOOD": 1, "BAD": 2, "GOOD_TAIL": 1})
        self.settings.write_text(json.dumps({"bad_fails": False, "extract_fails": False, "tail_fails": False}))
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(before[:2], (good.read_bytes(), good.stat().st_mtime_ns))
        self.assertEqual(self.counts(), {"GOOD": 1, "BAD": 2, "GOOD_TAIL": 2, "extract": 3})
        rows = [json.loads(line) for line in self.own.read_text().splitlines()]
        progress = [row for row in rows if row.get("source_id") == "good"]
        self.assertEqual(len(progress), 2)
        self.assertGreater(progress[-1]["distilled_through_last_ts"], progress[0]["distilled_through_last_ts"])
        self.assertEqual(progress[-1]["continues"], "good-note")
        self.assertEqual(self.git(self.bundle, "status", "--porcelain"), "")

    def test_git_ignored_ledger_is_not_reported_as_published(self):
        self.git(self.bundle, "rm", "--cached", ".kal-sync/ledger/test-device.jsonl")
        (self.bundle / ".git/info/exclude").write_text(".kal-sync/ledger/test-device.jsonl\n")
        self.git(self.bundle, "commit", "-m", "keep ledger local")
        self.git(self.bundle, "push", str(self.remote), "HEAD:main")
        self.settings.write_text(json.dumps({"bad_fails": False, "extract_fails": False}))
        result = self.sync()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("device ledger is ignored by Git", result.stdout + result.stderr)
        self.assertEqual(self.counts(), {})
        self.assertEqual(self.own.read_text(), self.seed)
        self.assertEqual(self.git(self.bundle, "status", "--porcelain"), "")

    def test_all_ignored_publication_targets_stop_before_any_paid_calls(self):
        self.settings.write_text(json.dumps({"bad_fails": False, "extract_fails": False}))
        for pattern in ("personal/sessions/", "personal/sessions/test-device/", ".kal-sync/extract/",
                        ".kal-sync/extract/test-device.lr_cache.jsonl"):
            with self.subTest(pattern=pattern):
                (self.bundle / ".git/info/exclude").write_text(pattern + "\n")
                result = self.sync()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.counts(), {})
                self.assertIn("ignored by Git", result.stdout + result.stderr)
                self.assertEqual(self.git(self.bundle, "status", "--porcelain"), "")
                self.assertEqual(self.git(self.bundle, "rev-parse", "HEAD").strip(), self.head)
                self.assertEqual(self.git(self.remote, "rev-parse", "main").strip(), self.head)

    def test_index_flags_cannot_hide_unpublished_generated_changes(self):
        relative = ".kal-sync/ledger/test-device.jsonl"
        for flag in ("assume-unchanged", "skip-worktree"):
            with self.subTest(flag=flag):
                self.git(self.bundle, "update-index", "--" + flag, relative)
                result = self.sync()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("index before paid work", result.stdout + result.stderr)
                self.assertEqual(self.counts(), {})
                self.assertEqual(self.git(self.bundle, "rev-parse", "HEAD").strip(), self.head)
                self.git(self.bundle, "update-index", "--no-" + flag, relative)

    def test_missing_staged_cache_stops_commit_and_preserves_pending_state(self):
        self.settings.write_text(json.dumps({"bad_fails": False, "extract_fails": False, "drop_cache_stage": True}))
        result = self.sync()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("not staged or tracked", result.stdout + result.stderr)
        self.assertEqual(self.git(self.bundle, "rev-parse", "HEAD").strip(), self.head)
        self.assertEqual(self.git(self.remote, "rev-parse", "main").strip(), self.head)
        self.assertEqual(len(list((self.state / "sync_pending").glob("*.json"))), 1)
        stage = next((self.state / "sync_ledger").glob("*.json"))
        self.assertEqual(len(json.loads(stage.read_text())["rows"]), 2)
        self.assertTrue((self.bundle / ".kal-sync/extract/test-device.lr_cache.jsonl").exists())

    def test_post_commit_verification_requires_artifacts_in_head_not_only_index(self):
        artifact = self.bundle / ".kal-sync/extract/test-device.lr_cache.jsonl"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("{}\n")
        self.git(self.bundle, "add", ".kal-sync/extract/test-device.lr_cache.jsonl")
        code = '''
import os
import github_sync as G
bundle = os.environ["KAL_VAULT"]
G.verify_publication(bundle, "test-device")
try:
    G.verify_publication(bundle, "test-device", committed=True)
except G.SyncError as error:
    assert "not staged or tracked" in str(error), error
else:
    raise AssertionError("an uncommitted artifact was accepted as published")
'''
        result = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_distilled_output_inside_checkout_is_refused_before_llm_calls(self):
        self.env["KAL_DISTILLED"] = str(self.bundle / "unsafe-output")
        result = self.sync()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("distilled output must be outside", result.stdout + result.stderr)
        self.assertEqual(self.counts(), {})
        self.assertEqual(self.git(self.bundle, "status", "--porcelain"), "")

    def test_state_inside_checkout_is_refused_before_llm_calls(self):
        self.env["KAL_HOME"] = str(self.bundle / ".git/unsafe-state")
        result = self.sync()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("pending ledger must be outside", result.stdout + result.stderr)
        self.assertEqual(self.counts(), {})
        self.assertFalse((self.bundle / ".git/unsafe-state/sync_ledger").exists())
        self.assertEqual(self.git(self.bundle, "status", "--porcelain"), "")

    def test_staging_error_does_not_create_a_successful_marker(self):
        stage = self.fail_first()
        stage.chmod(0o644)
        code = f'''
import os
import distill_sessions as D
import ledger
os.environ.update(KAL_LEDGER_STAGE={str(stage)!r}, KAL_LEDGER_REPOSITORY="example/bundle")
try:
    D.mark_done({{"agent":"claude", "session_id":"unmarked", "text":"fixture", "last_ts":"2020-01-01T00:00:00Z"}})
except ledger.LedgerStageError:
    assert not os.path.exists(os.path.join(D.DONE, "claude-unmarked"))
else:
    raise AssertionError("staging failure was swallowed")
'''
        result = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.own.read_text(), self.seed)

    def test_pending_mode_and_identity_tampering_stops_before_any_more_calls(self):
        stage = self.fail_first()
        original = stage.read_bytes()
        baseline = self.counts()
        for field in ("device", "repository", "bundle"):
            with self.subTest(field=field):
                data = json.loads(original)
                data["identity"][field] = "unrelated"
                stage.write_text(json.dumps(data))
                result = self.sync()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("pending ledger", result.stdout + result.stderr)
                self.assertEqual(self.counts(), baseline)
                self.assertEqual(self.git(self.bundle, "status", "--porcelain"), "")
                stage.write_bytes(original)
        stage.chmod(0o644)
        result = self.sync()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("pending ledger", result.stdout + result.stderr)
        self.assertEqual(stage.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.counts(), baseline)
        self.assertEqual(self.own.read_text(), self.seed)

    def test_edited_pending_rows_are_not_adopted(self):
        stage = self.fail_first()
        data = json.loads(stage.read_text())
        data["rows"][0]["source_id"] = "unreviewed-user-entry"
        stage.write_text(json.dumps(data))
        result = self.sync()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("pending ledger", result.stdout + result.stderr)
        self.assertEqual(self.counts(), {"GOOD": 1, "BAD": 1})
        self.assertEqual(self.own.read_text(), self.seed)

    def test_foreign_pending_row_is_refused_even_with_a_matching_digest(self):
        stage = self.fail_first()
        data = json.loads(stage.read_text())
        data["rows"][0]["device"] = "other-device"
        data["digest"] = hashlib.sha256(json.dumps({"identity": data["identity"], "rows": data["rows"]},
                                                  sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        stage.write_text(json.dumps(data))
        result = self.sync()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("foreign row", result.stdout + result.stderr)
        self.assertEqual(self.counts(), {"GOOD": 1, "BAD": 1})
        self.assertEqual(self.own.read_text(), self.seed)

    def test_dirty_user_ledger_is_not_blessed_on_retry(self):
        self.fail_first()
        edited = self.seed + '{"user":"reviewed change"}\n'
        self.own.write_text(edited)
        result = self.sync()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("uncommitted changes", result.stdout + result.stderr)
        self.assertEqual(self.own.read_text(), edited)
        self.assertEqual(self.counts(), {"GOOD": 1, "BAD": 1})
        self.assertEqual(self.git(self.bundle, "rev-parse", "HEAD").strip(), self.head)

    def assert_inflight_ledger_tamper_refused(self, kind):
        self.own.chmod(0o644)
        before = self.own.stat().st_ino
        self.settings.write_text(json.dumps({"bad_fails": False, "extract_fails": False,
                                           "tamper_ledger": kind, "ledger_path": str(self.own)}))
        result = self.sync()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("mode or file identity changed", result.stdout + result.stderr)
        self.assertEqual(self.git(self.bundle, "status", "--porcelain"), "")
        self.assertEqual(self.own.read_text(), self.seed)
        self.assertFalse((self.bundle / "personal").exists())
        self.assertEqual(self.counts(), {"GOOD": 1, "BAD": 1})
        self.assertEqual(self.git(self.bundle, "rev-parse", "HEAD").strip(), self.head)
        self.assertEqual(self.git(self.remote, "rev-parse", "main").strip(), self.head)
        if kind == "mode":
            self.assertEqual(self.own.stat().st_mode & 0o777, 0o600)
        else:
            self.assertNotEqual(self.own.stat().st_ino, before)

    def test_inflight_mode_change_invisible_to_git_is_refused_before_emission(self):
        self.assert_inflight_ledger_tamper_refused("mode")

    def test_inflight_identical_byte_file_replacement_is_refused_before_emission(self):
        self.assert_inflight_ledger_tamper_refused("identity")

    def test_pending_symlink_and_hardlink_are_refused(self):
        stage = self.fail_first()
        saved = stage.with_suffix(".saved")
        stage.rename(saved)
        stage.symlink_to(saved)
        result = self.sync()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("pending ledger", result.stdout + result.stderr)
        stage.unlink()
        saved.rename(stage)
        os.link(stage, saved)
        result = self.sync()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("pending ledger", result.stdout + result.stderr)
        self.assertEqual(self.counts(), {"GOOD": 1, "BAD": 1})
        self.assertEqual(self.git(self.bundle, "status", "--porcelain"), "")

    def test_extraction_failure_keeps_ledger_pending_without_a_commit_or_push(self):
        self.settings.write_text(json.dumps({"bad_fails": False, "extract_fails": True}))
        result = self.sync()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("entity extraction failed", result.stdout + result.stderr)
        self.assertEqual(self.git(self.bundle, "rev-parse", "HEAD").strip(), self.head)
        self.assertEqual(self.git(self.remote, "rev-parse", "main").strip(), self.head)
        staged = list((self.state / "sync_ledger").glob("*.json"))
        self.assertEqual(len(staged), 1)
        self.assertEqual(len(json.loads(staged[0].read_text())["rows"]), 2)
        self.settings.write_text(json.dumps({"bad_fails": False, "extract_fails": False}))
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.counts()["GOOD"], 1)
        self.assertEqual(self.counts()["BAD"], 1)
        self.assertEqual(len(self.own.read_text().splitlines()), 3)
        self.assertEqual(self.git(self.bundle, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
