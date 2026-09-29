import collections
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SRC = Path(__file__).resolve().parent
REPLY = "<<<DOC>>>\ntitle: Saved decision\ndoc_type: decision\nwhy_captured: Offline fixture\ntags: test\n---\nA verified offline decision preserves completed work and retries only unfinished sessions without overwriting reviewed pages.\n<<<END>>>"
LLM_RUNNER = '''
import json
import os
import runpy
import sys
from unittest.mock import patch
sys.path.insert(0, os.environ["FIXTURE_SRC"])
import claude_cli
responses = json.loads(os.environ["FIXTURE_RESPONSES"])
def answer(model, prompt, **kwargs):
    label = next((label for label in responses if label in prompt), "UNEXPECTED")
    with open(os.environ["FIXTURE_CALLS"], "a") as output:
        output.write(label + "\\n")
    if label not in responses or responses[label] == "RAISE":
        raise RuntimeError("fixture device backend unavailable")
    return responses[label]
sys.argv = [os.path.join(os.environ["FIXTURE_SRC"], "distill_sessions.py"), "--workers", "1"]
with patch.object(claude_cli, "run", side_effect=answer):
    runpy.run_path(sys.argv[0], run_name="__main__")
'''


class DistillationFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kal-distill-failure-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.state = self.home / "state"
        self.out = self.state / "distilled"
        self.bundle = self.root / "bundle"
        (self.state / "sessions").mkdir(parents=True)
        self.bundle.mkdir()
        self.corpus = self.state / "sessions/session_docs.json"
        self.calls = self.root / "calls.txt"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("KAL_", "GIT_", "CLAUDE_", "ANTHROPIC_"))
                    and key not in ("PYTHONPATH", "PYTHONHOME", "VAULT_DIR")}
        self.env.update(HOME=str(self.home), KAL_HOME=str(self.state), KAL_DISTILLED=str(self.out),
                        KAL_DEVICE="test-device", FIXTURE_SRC=str(SRC), FIXTURE_CALLS=str(self.calls),
                        GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
                        HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        self.git("init", "-b", "main")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.invalid")
        (self.bundle / "index.md").write_text("Human index\n")
        existing = self.bundle / "personal/sessions/test-device/human.md"
        existing.parent.mkdir(parents=True)
        existing.write_text("Existing reviewed human page\n")
        self.git("add", ".")
        self.git("commit", "-m", "fixture")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.bundle), *args], env=self.env,
                              check=True, capture_output=True, text=True).stdout

    def record(self, sid, label, last_ts="2020-01-01T00:00:00Z", **extra):
        return {"session_id": sid, "project": "offline-fixture", "agent": "claude", "n_msg": 2,
                "first_ts": "2020-01-01T00:00:00Z", "last_ts": last_ts,
                "text": f"**Me**: {label} We decided to preserve finished work and retry only failed sessions.", **extra}

    def run_distill(self, records, responses):
        self.corpus.write_text(json.dumps(records))
        return subprocess.run([sys.executable, "-c", LLM_RUNNER],
                              env=dict(self.env, FIXTURE_RESPONSES=json.dumps(responses)),
                              capture_output=True, text=True, timeout=60)

    def call_counts(self):
        return collections.Counter(self.calls.read_text().splitlines()) if self.calls.exists() else collections.Counter()

    def assert_run_status(self, status):
        runs = list((self.state / "runs").glob("*.json"))
        self.assertTrue(runs)
        latest = max(runs, key=lambda path: path.stat().st_mtime_ns)
        run = json.loads(latest.read_text())
        self.assertEqual(run["status"], status)
        self.assertEqual(run["exit_code"] == 0, status == "ok")

    def test_ordinary_failure_exits_nonzero_and_records_failed_without_marker(self):
        result = self.run_distill([self.record("bad", "FRESH_BAD")], {"FRESH_BAD": "invalid model output"})
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1 session(s) and 0 continuation(s) failed", result.stderr)
        self.assertFalse((self.out / ".done/claude-bad").exists())
        self.assertEqual(list(self.out.glob("*.md")), [])
        self.assert_run_status("failed")

    def test_backend_exception_also_records_failure_without_marker(self):
        result = self.run_distill([self.record("bad", "FRESH_BAD")], {"FRESH_BAD": "RAISE"})
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.out / ".done/claude-bad").exists())
        self.assert_run_status("failed")

    def test_partial_success_retries_only_failed_work_and_emits_without_overwriting(self):
        records = [self.record("good", "FRESH_GOOD"), self.record("bad", "FRESH_BAD")]
        result = self.run_distill(records, {"FRESH_GOOD": REPLY, "FRESH_BAD": "invalid model output"})
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_run_status("failed")
        good = self.out / "saved-decision.md"
        marker = self.out / ".done/claude-good"
        saved = good.read_bytes(), good.stat().st_mtime_ns, marker.read_bytes(), marker.stat().st_mtime_ns
        self.assertFalse((self.out / ".done/claude-bad").exists())
        self.assertEqual(self.git("status", "--porcelain"), "")
        result = self.run_distill(records, {"FRESH_BAD": REPLY})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_run_status("ok")
        self.assertEqual(saved, (good.read_bytes(), good.stat().st_mtime_ns, marker.read_bytes(), marker.stat().st_mtime_ns))
        self.assertEqual(self.call_counts(), {"FRESH_GOOD": 1, "FRESH_BAD": 2})
        self.assertTrue((self.out / ".done/claude-bad").exists())
        self.assertEqual(len(list(self.out.glob("*.md"))), 2)
        result = self.run_distill(records, {})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.call_counts(), {"FRESH_GOOD": 1, "FRESH_BAD": 2})
        result = subprocess.run([sys.executable, str(SRC / "openwiki_emit.py"), "--wiki", str(self.bundle),
                                 "--from", str(self.out), "--device", "test-device"],
                                env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.bundle / "personal/sessions/test-device/human.md").read_text(), "Existing reviewed human page\n")
        self.assertEqual((self.bundle / "index.md").read_text(), "Human index\n")
        changes = self.git("ls-files", "--others", "--exclude-standard").splitlines()
        self.assertEqual(len(changes), 2)
        self.assertTrue(all(path.startswith("personal/sessions/test-device/") for path in changes), changes)

    def test_continuation_failure_preserves_marker_and_retries_only_tail(self):
        original = self.record("grown", "ORIGINAL_TURN")
        result = self.run_distill([original], {"ORIGINAL_TURN": REPLY})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        page = self.out / "saved-decision.md"
        marker = self.out / ".done/claude-grown"
        before_page, before_marker = page.read_bytes(), marker.read_bytes()
        raw = self.root / "grown.jsonl"
        raw.write_text("\n".join(json.dumps({"type": "user", "timestamp": stamp,
                                           "message": {"content": text}}) for stamp, text in (
            ("2020-01-01T00:00:00Z", "ORIGINAL_TURN The original decision is already preserved."),
            ("2020-01-02T00:00:00Z", "TAIL_TURN A later decision preserves the original page and adds only new conclusions."),
        )) + "\n")
        grown = self.record("grown", "ORIGINAL_TURN TAIL_TURN", "2020-01-02T00:00:00Z", abs_path=str(raw))
        result = self.run_distill([grown], {"TAIL_TURN": "invalid model output"})
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("0 session(s) and 1 continuation(s) failed", result.stderr)
        self.assert_run_status("failed")
        self.assertEqual(marker.read_bytes(), before_marker)
        self.assertEqual(page.read_bytes(), before_page)
        self.assertEqual(len(list(self.out.glob("*.md"))), 1)
        result = self.run_distill([grown], {"TAIL_TURN": REPLY})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(page.read_bytes(), before_page)
        self.assertEqual(json.loads(marker.read_text())["last_ts"], grown["last_ts"])
        self.assertEqual(len(json.loads(marker.read_text())["pages"]), 2)
        result = self.run_distill([grown], {})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.call_counts(), {"ORIGINAL_TURN": 1, "TAIL_TURN": 2})

    def test_default_six_hour_settling_still_prevents_calls(self):
        recent = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=5)).isoformat()
        result = self.run_distill([self.record("active", "ACTIVE_TURN", recent)], {})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("6h", result.stdout)
        self.assertEqual(self.call_counts(), {})
        self.assertFalse((self.out / ".done/claude-active").exists())

    def test_actual_sync_stops_before_emission_extraction_commit_or_push(self):
        self.corpus.write_text(json.dumps([self.record("bad", "FRESH_BAD")]))
        self.git("remote", "add", "origin", "https://github.com/example/bundle.git")
        self.git("fetch", str(self.bundle), "main")
        code = '''
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
sys.path.insert(0, os.environ["FIXTURE_SRC"])
import github_sync
import device_auth
real_run = subprocess.run
def run(command, **kwargs):
    script = Path(command[1]).name if len(command) > 1 else ""
    if script in ("ingest_sessions.py", "ingest_codex_sessions.py", "ingest_hermes_sessions.py"):
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
    if script == "distill_sessions.py":
        return real_run([command[0], "-c", os.environ["FIXTURE_RUNNER"]], **kwargs)
    if script == "openwiki_emit.py":
        raise AssertionError("emission must not follow failed distillation")
    return real_run(command, **kwargs)
output = []
with patch.object(device_auth, "load_credential", return_value={"url": "https://example.invalid", "token": "fake-device"}), \\
     patch.object(github_sync, "pull"), \\
     patch.object(subprocess, "run", side_effect=run), \\
     patch.object(github_sync, "run_device_extract") as extract, \\
     patch.object(github_sync, "commit_own_changes") as commit, \\
     patch.object(github_sync, "push") as push:
    try:
        github_sync.sync(bundle=os.environ["KAL_VAULT"], device="test-device", print_fn=output.append,
                         get_token=lambda: {"repo": "https://github.com/example/bundle.git", "token": "fake-installation"})
    except github_sync.SyncError as error:
        assert "distill_sessions.py failed" in str(error), str(error)
        assert "1 session(s) and 0 continuation(s) failed" in str(error), str(error)
    else:
        raise AssertionError("failed distillation was reported as a completed sync")
    extract.assert_not_called()
    commit.assert_not_called()
    push.assert_not_called()
assert not any("sync complete" in line for line in output), output
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                                env=dict(self.env, KAL_VAULT=str(self.bundle), FIXTURE_RUNNER=LLM_RUNNER,
                                         FIXTURE_RESPONSES=json.dumps({"FRESH_BAD": "invalid model output"})))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_run_status("failed")
        self.assertEqual(self.git("status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
