"""Real-git regression tests for `kal sync`: force-push resurrection, empty first sync, HTTP hardening."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import device_auth
import github_sync


class GitCase(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="kal-sync-hard-")))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("KAL_", "GIT_"))}
        env.update(HOME=str(self.root), KAL_HOME=str(self.root / "state"),
                   GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        self.stack.enter_context(patch.dict(os.environ, env, clear=True))
        self.remote = self.root / "remote.git"
        self.git(None, "init", "--bare", "-b", "main", str(self.remote))

    def git(self, repo, *args):
        cmd = ["git"] + (["-C", str(repo)] if repo else []) + list(args)
        return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout

    def clone(self, name):
        path = self.root / name
        self.git(None, "clone", str(self.remote), str(path))
        self.git(path, "config", "user.name", name)
        self.git(path, "config", "user.email", f"{name}@example.invalid")
        return path

    def commit_file(self, repo, rel, text="x\n"):
        target = Path(repo) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        self.git(repo, "add", "--", rel)
        self.git(repo, "commit", "-m", f"add {rel}")


class ForcePushTests(GitCase):
    def test_force_pushed_away_commit_is_not_resurrected(self):
        other = self.clone("other")
        self.commit_file(other, "personal/sessions/other/seed.md")
        self.git(other, "push", "origin", "main")

        mine = self.clone("mine")
        self.commit_file(mine, "personal/sessions/mine/leak.md", "secret\n")
        self.assertTrue(github_sync.push(str(mine), str(self.remote), None, "mine"))

        # someone force-pushes the leak away
        self.git(other, "pull", "origin", "main")
        self.git(other, "reset", "--hard", "HEAD~1")
        self.git(other, "push", "--force", "origin", "main")

        # my device has an unpushed commit, then syncs
        self.commit_file(mine, "personal/sessions/mine/new.md")
        with self.assertRaisesRegex(github_sync.SyncError, "rewritten"):
            github_sync.pull(str(mine), str(self.remote), None)
        with self.assertRaisesRegex(github_sync.SyncError, "rewritten"):
            github_sync.push(str(mine), str(self.remote), None, "mine")
        tree = self.git(None, "--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", "main")
        self.assertNotIn("leak.md", tree)
        self.assertNotIn("new.md", tree)

    def test_normal_divergence_still_rebases(self):
        other = self.clone("other")
        self.commit_file(other, "personal/sessions/other/seed.md")
        self.git(other, "push", "origin", "main")
        mine = self.clone("mine")
        github_sync.pull(str(mine), str(self.remote), None)
        self.commit_file(other, "personal/sessions/other/two.md")
        self.git(other, "push", "origin", "main")
        self.commit_file(mine, "personal/sessions/mine/a.md")
        self.assertTrue(github_sync.push(str(mine), str(self.remote), None, "mine"))
        tree = self.git(None, "--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", "main")
        self.assertIn("two.md", tree)
        self.assertIn("a.md", tree)


class DivergedPullTests(GitCase):
    def test_diverged_local_commits_are_rebased_before_distil_sees_the_ledger(self):
        other = self.clone("other")
        self.commit_file(other, "personal/sessions/other/seed.md")
        self.git(other, "push", "origin", "main")
        mine = self.clone("mine")
        github_sync.pull(str(mine), str(self.remote), None)
        self.commit_file(other, ".kal-sync/ledger/other.jsonl", '{"source_id": "s1"}\n')
        self.git(other, "push", "origin", "main")
        self.commit_file(mine, "personal/sessions/mine/unpushed.md")   # an earlier failed push
        self.assertTrue(github_sync.pull(str(mine), str(self.remote), None))
        self.assertTrue((mine / ".kal-sync/ledger/other.jsonl").exists())
        self.assertTrue((mine / "personal/sessions/mine/unpushed.md").exists())

    def test_conflicting_rebase_aborts_the_sync_before_distil(self):
        other = self.clone("other")
        self.commit_file(other, "f.md", "base\n")
        self.git(other, "push", "origin", "main")
        mine = self.clone("mine")
        self.commit_file(other, "f.md", "theirs\n")
        self.git(other, "push", "origin", "main")
        self.commit_file(mine, "f.md", "mine\n")
        head = self.git(mine, "rev-parse", "HEAD")
        with self.assertRaisesRegex(github_sync.SyncError, "conflict"):
            github_sync.pull(str(mine), str(self.remote), None)
        self.assertEqual(self.git(mine, "rev-parse", "HEAD"), head)
        self.assertNotIn("rebase", self.git(mine, "status"))


class SharedIndexConflictTests(GitCase):
    """Two devices adding pages at once both rewrite the shared indexes; that must not wedge sync."""

    PAGE = "---\ntitle: %s\ndescription: d\n---\nbody\n"

    def seeded(self):
        seed = self.clone("seed")
        self.commit_file(seed, "personal/sessions/index.md",
                         "# sessions\n\n* [a](/personal/sessions/a/index.md) - 0 page(s)\n"
                         "* [b](/personal/sessions/b/index.md) - 0 page(s)\n")
        self.git(seed, "push", "origin", "main")
        return self.clone("dev-a"), self.clone("dev-b")

    def add_page(self, repo, device, line):
        self.commit_file(repo, f"personal/sessions/{device}/p.md", self.PAGE % device)
        index = repo / "personal/sessions/index.md"
        lines = index.read_text().splitlines()
        lines[line] = lines[line].replace("0 page(s)", "1 page(s)")
        self.commit_file(repo, "personal/sessions/index.md", "\n".join(lines) + "\n")

    def test_concurrent_pushes_both_land_with_a_consistent_index(self):
        a, b = self.seeded()
        self.add_page(a, "a", 2)
        self.add_page(b, "b", 3)
        self.assertTrue(github_sync.push(str(a), str(self.remote), None, "a"))
        self.assertTrue(github_sync.push(str(b), str(self.remote), None, "b"))
        tree = self.git(None, "--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", "main")
        self.assertIn("personal/sessions/a/p.md", tree)
        self.assertIn("personal/sessions/b/p.md", tree)
        index = self.git(None, "--git-dir", str(self.remote), "show", "main:personal/sessions/index.md")
        self.assertIn("a/index.md) - 1 page(s)", index)
        self.assertIn("b/index.md) - 1 page(s)", index)
        self.assertEqual(self.git(b, "status", "--porcelain"), "")
        self.assertNotIn("rebase", self.git(b, "status"))

    def test_pull_resolves_the_same_conflict(self):
        a, b = self.seeded()
        self.add_page(a, "a", 2)
        self.add_page(b, "b", 3)
        self.assertTrue(github_sync.push(str(a), str(self.remote), None, "a"))
        self.assertTrue(github_sync.pull(str(b), str(self.remote), None, device="dev-b"))
        self.assertTrue((b / "personal/sessions/a/p.md").exists())
        self.assertTrue((b / "personal/sessions/b/p.md").exists())
        self.assertEqual(self.git(b, "status", "--porcelain"), "")
        self.assertIn("a/index.md) - 1 page(s)", (b / "personal/sessions/index.md").read_text())

    def test_upstream_deleted_index_modify_delete_does_not_wedge(self):
        a, b = self.seeded()
        self.git(a, "rm", "-q", "personal/sessions/index.md")
        self.git(a, "commit", "-q", "-m", "drop index")
        self.git(a, "push", "origin", "main")
        self.add_page(b, "b", 3)
        self.assertTrue(github_sync.pull(str(b), str(self.remote), None, device="dev-b"))
        self.assertEqual(self.git(b, "status", "--porcelain"), "")
        self.assertNotIn("rebase", self.git(b, "status"))

    def test_conflict_outside_the_shared_indexes_still_aborts(self):
        a, b = self.seeded()
        self.commit_file(a, "personal/sessions/f.md", "theirs\n")
        self.commit_file(b, "personal/sessions/f.md", "mine\n")
        self.git(a, "push", "origin", "main")
        head = self.git(b, "rev-parse", "HEAD")
        with self.assertRaisesRegex(github_sync.SyncError, "conflict"):
            github_sync.pull(str(b), str(self.remote), None, device="dev-b")
        self.assertEqual(self.git(b, "rev-parse", "HEAD"), head)
        self.assertNotIn("rebase", self.git(b, "status"))


class RecoveryTextTests(unittest.TestCase):
    def test_docs_and_error_share_the_recovery_steps(self):
        docs = (Path(__file__).resolve().parent.parent / "docs" / "CONNECTING-AGENTS.md").read_text()
        for step in github_sync.recovery_steps("main"):
            self.assertIn(step, docs)
        joined = "".join(github_sync.recovery_steps("main"))
        self.assertIn("refs/kal/synced..", joined)
        self.assertIn("FETCH_HEAD", joined)
        self.assertIn("stash -u", joined)
        self.assertNotIn("origin/main && ", joined)


class BaselineTests(GitCase):
    def rewritten_remote(self):
        other = self.clone("other")
        self.commit_file(other, "personal/sessions/other/seed.md")
        self.git(other, "push", "origin", "main")
        mine = self.clone("mine")                       # origin/main == seed; no refs/kal/synced yet
        self.commit_file(mine, "personal/sessions/mine/new.md")
        self.git(other, "commit", "--amend", "-m", "rewritten seed")    # the seed mine is based on is gone
        self.git(other, "push", "--force", "origin", "main")
        return mine


    def test_error_text_names_the_synced_ref_and_matches_the_docs(self):
        mine = self.rewritten_remote()
        with self.assertRaises(github_sync.SyncError) as caught:
            github_sync.pull(str(mine), str(self.remote), None)
        message = str(caught.exception)
        self.assertIn("refs/kal/synced..", message)
        for step in github_sync.recovery_steps("main"):
            self.assertIn(step, message)
    def test_upgraded_device_uses_origin_tracking_ref_as_baseline(self):
        mine = self.rewritten_remote()
        with self.assertRaisesRegex(github_sync.SyncError, "update-ref -d refs/kal/synced"):
            github_sync.pull(str(mine), str(self.remote), None)

    def test_tracking_ref_catches_a_rewrite_that_head_alone_would_accept(self):
        other = self.clone("other")
        self.commit_file(other, "a.md")
        self.commit_file(other, "b.md")
        self.git(other, "push", "origin", "main")
        mine = self.clone("mine")                       # origin/main == b
        self.git(mine, "reset", "--hard", "HEAD~1")     # HEAD == a: an ancestor of whatever replaces b
        self.git(other, "reset", "--hard", "HEAD~1")
        self.commit_file(other, "c.md")
        self.git(other, "push", "--force", "origin", "main")
        with self.assertRaisesRegex(github_sync.SyncError, "rewritten"):
            github_sync.pull(str(mine), str(self.remote), None)

    def test_no_baseline_and_diverged_is_refused_but_a_fresh_clone_proceeds(self):
        mine = self.rewritten_remote()
        self.git(mine, "update-ref", "-d", "refs/remotes/origin/main")
        with self.assertRaisesRegex(github_sync.SyncError, "rewritten"):
            github_sync.pull(str(mine), str(self.remote), None)
        fresh = self.clone("fresh")
        self.assertTrue(github_sync.pull(str(fresh), str(self.remote), None))


class GitEnvTests(GitCase):
    def test_every_git_call_runs_under_the_c_locale(self):
        seen = []
        real = subprocess.run

        def spy(cmd, *a, **kw):
            seen.append(kw["env"])
            return real(cmd, *a, **kw)

        with patch.dict(os.environ, {"LC_ALL": "ko_KR.UTF-8", "LANGUAGE": "ko"}), \
                patch.object(github_sync.subprocess, "run", spy):
            github_sync._git(str(self.root), ["--version"], check=False)
        self.assertEqual(seen[0]["LC_ALL"], "C")
        self.assertEqual(seen[0]["LANG"], "C")
        self.assertNotIn("LANGUAGE", seen[0])


class EmptyRepositoryTests(GitCase):
    def test_first_sync_into_an_empty_repository(self):
        mine = self.clone("mine")
        self.assertEqual(github_sync._current_head(str(mine)), "")
        self.assertIs(github_sync.pull(str(mine), str(self.remote), None), False)
        github_sync._prepare_shared_cache(str(mine), remote_exists=False)
        target = mine / "personal/sessions/dev/a.md"
        target.parent.mkdir(parents=True)
        target.write_text("# a\n")
        self.assertTrue(github_sync.commit_own_changes(str(mine), "dev"))
        self.assertTrue(github_sync.push(str(mine), str(self.remote), None, "dev"))
        self.assertIn("a.md", self.git(None, "--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", "main"))


class HttpHardeningTests(unittest.TestCase):
    def test_only_https_or_loopback_http(self):
        for ok in ("https://app.example.com", "http://localhost:8080", "http://127.0.0.1:1", "http://[::1]:9"):
            device_auth.check_url(ok)
        for bad in ("http://example.com", "ftp://x", "file:///etc/passwd", "https://", "example.com"):
            with self.assertRaises(ValueError, msg=bad):
                device_auth.check_url(bad)

    def test_plain_http_is_refused_before_any_request(self):
        with patch.object(device_auth.OPENER, "open") as opened:
            with self.assertRaises(github_sync.SyncError):
                github_sync.list_repositories("http://example.com", "tok")
            with self.assertRaises(device_auth.LoginError):
                device_auth.login(url="http://example.com", print_fn=lambda *_: None)
        opened.assert_not_called()

    def test_redirects_are_refused(self):
        handler = device_auth._NoRedirect()
        req = urllib.request.Request("https://a.example/x", headers={"Authorization": "Bearer t"})
        self.assertIsNone(handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example/"))

    def test_login_accepts_rfc_and_legacy_field_names(self):
        for keys in (("verification_uri", "verification_uri_complete"), ("verify_uri", "verify_uri_complete")):
            body = {"device_code": "d", "user_code": "U-1", "expires_in": 5, "interval": 0,
                    keys[0]: "https://x/device", keys[1]: "https://x/device?c=U-1"}
            replies = [(200, body), (200, {"access_token": "kal_dev_t"})]
            out = []
            with patch.object(device_auth, "_post", side_effect=replies), \
                 patch.object(device_auth, "save_credential"):
                device_auth.login(url="https://x", device_name="n", sleep=lambda s: None, print_fn=out.append)
            self.assertIn("https://x/device", out[0])


class ErrorSurfacingTests(unittest.TestCase):
    def test_stdout_tail_is_included(self):
        proc = subprocess.CompletedProcess([], 1, stdout="refused: reason X", stderr="")
        self.assertIn("reason X", github_sync._output_tail(proc))
        proc = subprocess.CompletedProcess([], 1, stdout="out", stderr="err")
        text = github_sync._output_tail(proc)
        self.assertIn("err", text)
        self.assertIn("out", text)


if __name__ == "__main__":
    unittest.main()
