import contextlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import device_auth
import github_sync


class ExtractionRetryTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="kal-sync-retry-")))
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("KAL_", "GIT_", "ANTHROPIC_", "CLAUDE_"))}
        env.update(HOME=str(self.root), KAL_HOME=str(self.root / "state"), KAL_DEVICE="test-device",
                   GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        self.stack.enter_context(patch.dict(os.environ, env, clear=True))
        self.git("init", "-b", "main")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.invalid")
        (self.bundle / "index.md").write_text("Human index\n")
        self.git("add", ".")
        self.git("commit", "-m", "seed")
        self.git("fetch", str(self.bundle), "main")
        self.url = "https://github.com/example/bundle.git"
        self.git("remote", "add", "origin", self.url)
        self.stack.enter_context(patch.object(device_auth, "load_credential", return_value={
            "url": "https://example.invalid", "token": "fake-device", "device_name": "test-device",
        }))
        self.stack.enter_context(patch.object(github_sync, "list_repositories", return_value=[
            {"id": "repo-id", "owner": "example", "name": "bundle", "enabled": True},
        ]))
        self.stack.enter_context(patch.object(github_sync, "_fetch_sync_token", return_value={
            "repo": self.url, "token": "fake-installation",
        }))
        self.real_pull = github_sync.pull
        self.real_fetch = github_sync._fetch
        self.fetch = self.stack.enter_context(patch.object(github_sync, "_fetch"))
        self.pull = self.stack.enter_context(patch.object(github_sync, "pull"))
        self.push = self.stack.enter_context(patch.object(github_sync, "push"))
        self.emitted = self.bundle / "personal/sessions/test-device/한글 note.md"

        def emit(*args, **kwargs):
            self.emitted.parent.mkdir(parents=True, exist_ok=True)
            self.emitted.write_text("New generated content\n")

        self.distill = self.stack.enter_context(patch.object(github_sync, "run_distill", side_effect=emit))
        self.extract = self.stack.enter_context(patch.object(github_sync, "run_device_extract", side_effect=[
            github_sync.SyncError("device extraction failed"), {"extracted": 1, "reused": 0, "exported": 0},
        ]))

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.bundle), *args], check=True,
                              capture_output=True, text=True).stdout

    def sync(self):
        return github_sync.sync(bundle=str(self.bundle), repository_id="repo-id", print_fn=lambda *_: None)

    def test_divergent_history_is_reconciled_before_reading_shared_cache(self):
        self.fetch.side_effect = self.real_fetch
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
        self.git("push", str(remote), "HEAD:main")
        # This device synced the seed before; without that baseline a diverged history is refused
        # as a possible rewrite (check_remote_not_rewritten).
        self.git("update-ref", "refs/kal/synced", "HEAD")
        other = self.root / "other"
        subprocess.run(["git", "clone", str(remote), str(other)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(other), "config", "user.name", "test"], check=True)
        subprocess.run(["git", "-C", str(other), "config", "user.email", "test@example.invalid"], check=True)
        self.emitted.parent.mkdir(parents=True)
        self.emitted.write_text("Previously unpushed local work\n")
        self.git("add", ".")
        self.git("commit", "-m", "local")
        relative = ".kal-sync/extract/other-device.lr_cache.jsonl"
        shared = other / relative
        shared.parent.mkdir(parents=True)
        shared.write_text("{}\n")
        for args in (("add", "."), ("commit", "-m", "other device cache"), ("push", "origin", "main")):
            subprocess.run(["git", "-C", str(other), *args], check=True, capture_output=True)
        self.real_pull(str(self.bundle), str(remote), None)
        github_sync._prepare_shared_cache(str(self.bundle))
        self.assertEqual((self.bundle / relative).read_text(), "{}\n")
        self.assertEqual(self.emitted.read_text(), "Previously unpushed local work\n")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_failed_extraction_resumes_exact_generated_changes_without_redistilling(self):
        with self.assertRaisesRegex(github_sync.SyncError, "extraction failed"):
            self.sync()
        self.assertTrue(self.emitted.exists())
        self.push.assert_not_called()
        self.sync()
        self.distill.assert_called_once()
        self.pull.assert_called_once()
        self.push.assert_called_once()
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertIn("Kal-Host: test-device", self.git("log", "-1", "--pretty=%B"))

    def test_user_edit_after_failure_is_not_adopted_or_overwritten(self):
        with self.assertRaises(github_sync.SyncError):
            self.sync()
        self.emitted.write_text("Human correction after failure\n")
        with self.assertRaisesRegex(github_sync.SyncError, "uncommitted changes"):
            self.sync()
        self.assertEqual(self.emitted.read_text(), "Human correction after failure\n")
        self.assertEqual(self.extract.call_count, 1)
        self.push.assert_not_called()

    def test_changed_index_after_failure_is_not_adopted(self):
        with self.assertRaises(github_sync.SyncError):
            self.sync()
        self.git("add", ".")
        with self.assertRaisesRegex(github_sync.SyncError, "uncommitted changes"):
            self.sync()
        self.assertEqual(self.extract.call_count, 1)
        self.push.assert_not_called()

    def test_shared_user_edit_after_failure_is_not_adopted(self):
        with self.assertRaises(github_sync.SyncError):
            self.sync()
        (self.bundle / "index.md").write_text("New human index\n")
        with self.assertRaisesRegex(github_sync.SyncError, "uncommitted changes"):
            self.sync()
        self.assertEqual(self.extract.call_count, 1)
        self.push.assert_not_called()


if __name__ == "__main__":
    unittest.main()
