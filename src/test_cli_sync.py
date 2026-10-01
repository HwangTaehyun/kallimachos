import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error

import device_auth
import github_sync
import kal_cli


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repos = [
            {"id": "repo-one", "owner": "example", "name": "first", "enabled": True},
            {"id": "repo-two", "owner": "example", "name": "second", "enabled": True},
        ]

    def test_single_repository_is_selected(self):
        self.assertEqual(github_sync.select_repository(self.repos[:1]), "repo-one")

    def test_multiple_repositories_require_explicit_choice(self):
        with self.assertRaisesRegex(github_sync.SyncError, "--repository"):
            github_sync.select_repository(self.repos)

    def test_explicit_repository_must_be_accessible(self):
        self.assertEqual(github_sync.select_repository(self.repos, "repo-two"), "repo-two")
        with self.assertRaises(github_sync.SyncError):
            github_sync.select_repository(self.repos, "repo-missing")

    def test_disabled_repository_cannot_be_selected(self):
        self.repos[1]["enabled"] = False
        with self.assertRaises(github_sync.SyncError):
            github_sync.select_repository(self.repos, "repo-two")
        with self.assertRaisesRegex(github_sync.SyncError, "--repository"):
            github_sync.select_repository(self.repos)

    def test_no_repositories_explains_connection(self):
        with self.assertRaisesRegex(github_sync.SyncError, "connect"):
            github_sync.select_repository([])

    def test_repository_get_and_token_post(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({"repositories": self.repos}).encode()
        with patch.object(device_auth.OPENER, "open", return_value=response) as request:
            self.assertEqual(github_sync.list_repositories("https://example.invalid", "fake"), self.repos)
            req = request.call_args.args[0]
            self.assertEqual(req.method, "GET")
            self.assertEqual(req.full_url, "https://example.invalid/api/sync/repositories")
            self.assertEqual(req.get_header("Authorization"), "Bearer fake")
            response.read.return_value = b'{"token":"fake-installation","repo":"https://github.com/example/second.git"}'
            github_sync._fetch_sync_token("https://example.invalid", "fake", repository_id="repo-two")
            self.assertEqual(json.loads(request.call_args.args[0].data), {"repository_id": "repo-two"})

    def test_build_endpoint_uses_device_bearer_and_empty_json(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"status":"queued"}'
        with patch.object(device_auth.OPENER, "open", return_value=response) as request:
            github_sync.request_cloud_build("https://example.invalid", "fake-device", "repo-two")
        req = request.call_args.args[0]
        self.assertEqual(req.full_url, "https://example.invalid/api/sync/repositories/repo-two/build")
        self.assertEqual(req.method, "POST")
        self.assertEqual(json.loads(req.data), {})
        self.assertEqual(req.get_header("Authorization"), "Bearer fake-device")

    def test_http_error_does_not_echo_secret_body(self):
        error = urllib.error.HTTPError("https://example.invalid", 401, "denied", {}, io.BytesIO(b"fake-secret"))
        with patch.object(device_auth.OPENER, "open", side_effect=error):
            with self.assertRaises(github_sync.SyncError) as caught:
                github_sync.list_repositories("https://example.invalid", "fake-secret")
        self.assertNotIn("fake-secret", str(caught.exception))

    def test_malformed_repository_response_is_clean_error(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"repositories": "invalid"}'
        with patch.object(device_auth.OPENER, "open", return_value=response):
            with self.assertRaises(github_sync.SyncError):
                github_sync.list_repositories("https://example.invalid", "fake")


class CLITests(unittest.TestCase):
    def test_sync_passes_repository(self):
        with patch.object(github_sync, "sync") as sync:
            self.assertEqual(kal_cli.main(["sync", "--repository", "repo-two"]), 0)
        sync.assert_called_once_with(repository_id="repo-two", notify_build=False)

    def test_session_end_runs_one_cycle(self):
        with patch.object(github_sync, "sync") as sync:
            self.assertEqual(kal_cli.main(["session-end", "--repository", "repo-two"]), 0)
        sync.assert_called_once_with(repository_id="repo-two", notify_build=False)

    def test_hook_printing_does_not_run_sync(self):
        output = io.StringIO()
        with patch.object(github_sync, "sync") as sync, contextlib.redirect_stdout(output):
            self.assertEqual(kal_cli.main(["session-end", "--repository", "repo-two", "--print-hook"]), 0)
        hook = json.loads(output.getvalue())["hooks"]["SessionEnd"][0]["hooks"][0]
        self.assertEqual(hook["type"], "command")
        self.assertIn("session-end --repository repo-two", hook["command"])
        sync.assert_not_called()

    def test_build_notification_failure_has_distinct_exit_status(self):
        error = github_sync.BuildNotificationError("Git push succeeded, but cloud build notification failed: offline")
        stderr = io.StringIO()
        with patch.object(github_sync, "sync", side_effect=error) as sync, contextlib.redirect_stderr(stderr):
            self.assertEqual(kal_cli.main(["sync", "--repository", "repo-two", "--notify-build"]), 2)
        sync.assert_called_once_with(repository_id="repo-two", notify_build=True)
        self.assertIn("push succeeded", stderr.getvalue())
        self.assertNotIn("push failed", stderr.getvalue())

    def test_hook_retains_opt_in_build_notification(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(kal_cli.main(["session-end", "--repository", "repo-two", "--notify-build", "--print-hook"]), 0)
        command = json.loads(output.getvalue())["hooks"]["SessionEnd"][0]["hooks"][0]["command"]
        self.assertTrue(command.endswith("--notify-build"))

    def test_autosync_default_interval(self):
        with patch.object(github_sync, "autosync") as autosync:
            self.assertEqual(kal_cli.main(["autosync", "--repository", "repo-two"]), 0)
        self.assertEqual(autosync.call_args.kwargs["interval"], 1800)
        self.assertEqual(autosync.call_args.kwargs["repository_id"], "repo-two")

    def test_autosync_rejects_nonpositive_interval(self):
        with patch.object(github_sync, "autosync") as autosync, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                kal_cli.main(["autosync", "--interval", "0"])
        autosync.assert_not_called()


class AutosyncTests(unittest.TestCase):
    def test_failure_is_reported_and_next_cycle_runs(self):
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.side_effect = [False, True]
        errors = []
        with patch.object(github_sync, "sync", side_effect=[github_sync.SyncError("offline"), None]) as sync:
            github_sync.autosync(repository_id="repo-two", interval=1800, stop=stop, print_fn=errors.append)
        self.assertEqual(sync.call_count, 2)
        self.assertEqual(stop.wait.call_args_list[0].args, (1800,))
        self.assertTrue(any("offline" in line for line in errors))

    def test_daemon_retries_notification_failure_without_misreporting_push(self):
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.side_effect = [False, True]
        printed = []
        error = github_sync.BuildNotificationError("Git push succeeded, but cloud build notification failed: offline")
        with patch.object(github_sync, "sync", side_effect=[error, None]) as sync:
            github_sync.autosync(repository_id="repo-two", notify_build=True, stop=stop, print_fn=printed.append)
        self.assertEqual(sync.call_count, 2)
        self.assertTrue(all(call.kwargs["notify_build"] for call in sync.call_args_list))
        self.assertIn("push succeeded", printed[0])
        self.assertIn("retrying", printed[0])
        self.assertNotIn("sync failed", printed[0])

    def test_already_stopped_daemon_does_no_work(self):
        stop = threading.Event()
        stop.set()
        with patch.object(github_sync, "sync") as sync:
            github_sync.autosync(stop=stop)
        sync.assert_not_called()

    def test_file_ownership_does_not_match_a_different_filename_prefix(self):
        self.assertFalse(github_sync._is_own_path(".kal-sync/extract/test-device.lr_cache.jsonl.backup", "test-device"))
        self.assertFalse(github_sync._is_own_path(".kal-sync/ledger/test-device.jsonl.other", "test-device"))

    def test_lock_rejects_overlap_then_releases(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"KAL_HOME": home}):
            with github_sync.sync_lock():
                with self.assertRaisesRegex(github_sync.SyncError, "already"):
                    with github_sync.sync_lock():
                        self.fail("overlapping sync acquired the lock")
            with github_sync.sync_lock():
                pass

    def test_distill_targets_this_devices_owned_directory(self):
        with patch.object(subprocess, "run", return_value=Mock(returncode=0)) as run:
            github_sync.run_distill("/unused", device="test-device")
        command = run.call_args.args[0]
        self.assertEqual(command[-2:], ["--device", "test-device"])
        self.assertEqual(run.call_args.kwargs["env"]["KAL_VAULT"], "/unused")
        self.assertEqual(run.call_args.kwargs["env"]["KAL_DEVICE"], "test-device")

    def test_emitter_uses_the_same_configured_distilled_folder(self):
        with patch.dict(os.environ, {"KAL_DISTILLED": "/configured/distilled"}), \
                patch.object(subprocess, "run", return_value=Mock(returncode=0)) as run:
            github_sync.run_distill("/unused", device="test-device")
        command = run.call_args.args[0]
        self.assertIn("--from", command)
        self.assertEqual(command[command.index("--from") + 1], "/configured/distilled")

    def test_git_askpass_runs_with_current_interpreter(self):
        helpers = []

        def verify(command, **kwargs):
            helper = Path(kwargs["env"]["GIT_ASKPASS"])
            helpers.append(helper)
            self.assertIn(sys.executable, helper.read_text())
            self.assertNotIn("fake-token", helper.read_text())
            self.assertEqual(helper.stat().st_mode & 0o777, 0o700)
            return Mock(returncode=0)

        with patch.object(subprocess, "run", side_effect=verify) as run:
            github_sync._git("/unused", ["status"], token="fake-token")
        self.assertNotIn("fake-token", " ".join(run.call_args.args[0]))
        self.assertFalse(helpers[0].exists())


class SyncCycleTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        (self.bundle / ".git").mkdir()
        self.stack.enter_context(patch.dict(os.environ, {"KAL_HOME": str(self.root / "home"), "KAL_DEVICE": "test-device"}))
        self.stack.enter_context(patch.object(device_auth, "load_credential", return_value={
            "url": "https://example.invalid", "token": "fake-device", "device_name": "Test Device",
        }))
        self.repositories = self.stack.enter_context(patch.object(github_sync, "list_repositories", return_value=[
            {"id": "repo-one", "owner": "example", "name": "first", "enabled": True},
            {"id": "repo-two", "owner": "example", "name": "second", "enabled": True},
        ]))
        self.token = self.stack.enter_context(patch.object(github_sync, "_fetch_sync_token", return_value={
            "repo": "https://github.com/example/second.git", "token": "fake-installation",
        }))
        self.git = self.stack.enter_context(patch.object(github_sync, "_git", return_value=Mock(
            returncode=0, stdout="git@github.com:example/second.git\n",
        )))
        def git_result(bundle, args, **kwargs):
            if args[0] == "check-ignore":
                return Mock(returncode=1, stdout="", stderr="")
            if args[0] in ("ls-files", "ls-tree"):
                paths = github_sync.publication_files(bundle, "test-device")
                if "-v" in args:
                    paths = ["H " + path for path in paths]
                return Mock(returncode=0, stdout="\0".join(paths), stderr="")
            if args[0] == "diff":
                return Mock(returncode=0, stdout="", stderr="")
            return self.git.return_value

        self.git.side_effect = git_result
        self.pull = self.stack.enter_context(patch.object(github_sync, "pull"))
        self.distill = self.stack.enter_context(patch.object(github_sync, "run_distill"))
        self.extract = self.stack.enter_context(patch.object(github_sync, "run_device_extract", return_value={
            "extracted": 0, "reused": 1, "exported": 0,
        }))
        self.commit = self.stack.enter_context(patch.object(github_sync, "commit_own_changes", return_value=True))
        self.push = self.stack.enter_context(patch.object(github_sync, "push"))
        self.changed = self.stack.enter_context(patch.object(github_sync, "changed_paths", return_value=[]))

    def test_ambiguity_stops_before_token_or_git(self):
        with self.assertRaisesRegex(github_sync.SyncError, "--repository"):
            github_sync.sync(bundle=str(self.bundle), print_fn=lambda *_: None)
        self.token.assert_not_called()
        self.pull.assert_not_called()
        self.distill.assert_not_called()

    def test_explicit_choice_reaches_token_endpoint(self):
        github_sync.sync(bundle=str(self.bundle), repository_id="repo-two", print_fn=lambda *_: None)
        self.token.assert_called_with("https://example.invalid", "fake-device", "repo-two")
        self.commit.assert_called_once_with(str(self.bundle), "test-device")

    def test_single_repository_works_without_argument(self):
        self.repositories.return_value = self.repositories.return_value[:1]
        github_sync.sync(bundle=str(self.bundle), print_fn=lambda *_: None)
        self.token.assert_called_with("https://example.invalid", "fake-device", "repo-one")

    def test_retry_pushes_existing_commit_without_new_changes(self):
        self.commit.return_value = False
        github_sync.sync(bundle=str(self.bundle), repository_id="repo-two", print_fn=lambda *_: None)
        self.push.assert_called_once()

    def test_gitfile_checkout_is_accepted(self):
        (self.bundle / ".git").rmdir()
        (self.bundle / ".git").write_text("gitdir: ../repo.git\n")
        github_sync.sync(bundle=str(self.bundle), repository_id="repo-two", print_fn=lambda *_: None)
        self.pull.assert_called_once()

    def test_selected_repository_must_match_checkout(self):
        self.git.return_value.stdout = "git@github.com:example/different.git\n"
        with self.assertRaisesRegex(github_sync.SyncError, "does not match"):
            github_sync.sync(bundle=str(self.bundle), repository_id="repo-two", print_fn=lambda *_: None)
        self.pull.assert_not_called()
        self.distill.assert_not_called()

    def test_missing_origin_is_not_silently_bound(self):
        self.git.return_value.returncode = 2
        with self.assertRaisesRegex(github_sync.SyncError, "origin"):
            github_sync.sync(bundle=str(self.bundle), repository_id="repo-two", print_fn=lambda *_: None)
        self.pull.assert_not_called()

    def test_entity_extraction_runs_between_emission_and_commit(self):
        steps = []
        self.distill.side_effect = lambda *a, **k: steps.append("emit")
        def extract(*args, **kwargs):
            steps.append("extract")
            artifact = self.bundle / ".kal-sync/extract/test-device.lr_cache.jsonl"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("{}\n")
            return {"extracted": 1, "reused": 0, "exported": 1}

        self.extract.side_effect = extract
        self.commit.side_effect = lambda *a, **k: (steps.append("commit") or True)
        self.push.side_effect = lambda *a, **k: steps.append("push")
        github_sync.sync(bundle=str(self.bundle), repository_id="repo-two", print_fn=lambda *_: None)
        self.assertEqual(steps, ["emit", "extract", "commit", "push"])

    def test_entity_extraction_failure_never_commits_or_pushes(self):
        self.extract.side_effect = github_sync.SyncError("device entity extraction failed")
        output = []
        with self.assertRaisesRegex(github_sync.SyncError, "extraction failed"):
            github_sync.sync(bundle=str(self.bundle), repository_id="repo-two", print_fn=output.append)
        self.commit.assert_not_called()
        self.push.assert_not_called()
        self.assertFalse(any("sync complete" in text or "extracted 1" in text for text in output))

    def test_dirty_checkout_is_refused_before_pull_or_emission(self):
        self.changed.return_value = ["index.md", "personal/sessions/test-device/manual.md"]
        with self.assertRaisesRegex(github_sync.SyncError, "uncommitted changes"):
            github_sync.sync(bundle=str(self.bundle), repository_id="repo-two", print_fn=lambda *_: None)
        self.pull.assert_not_called()
        self.distill.assert_not_called()
        self.commit.assert_not_called()

    def test_token_is_reissued_right_before_push(self):
        self.token.side_effect = [
            {"repo": "https://github.com/example/second.git", "token": "old"},
            {"repo": "https://github.com/example/second.git", "token": "fresh"},
        ]
        github_sync.sync(bundle=str(self.bundle), repository_id="repo-two", print_fn=lambda *_: None)
        self.assertEqual(self.push.call_args.args[2], "fresh")

    def test_push_auth_failure_requests_one_more_token(self):
        self.token.side_effect = [{"repo": "https://github.com/example/second.git", "token": t}
                                  for t in ("t1", "t2", "t3")]
        self.push.side_effect = [github_sync.SyncError("push failed: fatal: Authentication failed"), True]
        github_sync.sync(bundle=str(self.bundle), repository_id="repo-two", print_fn=lambda *_: None)
        self.assertEqual([c.args[2] for c in self.push.call_args_list], ["t2", "t3"])

    def test_non_auth_push_failure_is_not_retried(self):
        self.push.side_effect = github_sync.SyncError("push failed: remote rejected")
        with self.assertRaises(github_sync.SyncError):
            github_sync.sync(bundle=str(self.bundle), repository_id="repo-two", print_fn=lambda *_: None)
        self.assertEqual(self.push.call_count, 1)

    def test_last_result_is_recorded_and_shown(self):
        github_sync.sync(bundle=str(self.bundle), repository_id="repo-two", print_fn=lambda *_: None)
        path = Path(github_sync.last_result_path())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(github_sync.read_last_result()["ok"])
        self.push.side_effect = github_sync.SyncError("push failed: boom")
        with self.assertRaises(github_sync.SyncError):
            github_sync.sync(bundle=str(self.bundle), repository_id="repo-two", print_fn=lambda *_: None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = kal_cli.main(["sync", "--status"])
        self.assertEqual(code, 1)
        self.assertIn("FAILED", out.getvalue())
        self.assertIn("boom", out.getvalue())
        self.assertNotIn("fake-installation", path.read_text())

    def test_unexpected_exception_is_recorded_by_type_name_only(self):
        self.push.side_effect = KeyError("secret-detail")
        with self.assertRaises(KeyError):
            github_sync.sync(bundle=str(self.bundle), repository_id="repo-two", print_fn=lambda *_: None)
        last = github_sync.read_last_result()
        self.assertFalse(last["ok"])
        self.assertIn("KeyError", last["message"])
        self.assertNotIn("secret-detail", last["message"])

    def test_interrupted_emit_refusal_prints_recovery_steps(self):
        self.changed.return_value = ["personal/sessions/test-device/a.md"]
        with self.assertRaisesRegex(github_sync.SyncError, "clean -fd"):
            github_sync.sync(bundle=str(self.bundle), repository_id="repo-two", print_fn=lambda *_: None)

    def test_opt_in_cloud_build_runs_after_successful_push(self):
        events = []
        self.push.side_effect = lambda *a, **kw: events.append("push")
        with patch.object(github_sync, "request_cloud_build", side_effect=lambda *a: events.append("build")) as build:
            github_sync.sync(bundle=str(self.bundle), repository_id="repo-two", notify_build=True, print_fn=lambda *_: None)
        self.assertEqual(events, ["push", "build"])
        build.assert_called_once_with("https://example.invalid", "fake-device", "repo-two")

    def test_build_notification_is_off_by_default(self):
        with patch.object(github_sync, "request_cloud_build") as build:
            github_sync.sync(bundle=str(self.bundle), repository_id="repo-two", print_fn=lambda *_: None)
        build.assert_not_called()

    def test_build_failure_reports_successful_push_and_retries_without_new_commit(self):
        self.commit.return_value = False
        with patch.object(github_sync, "request_cloud_build", side_effect=[github_sync.SyncError("offline"), {}]) as build:
            with self.assertRaisesRegex(github_sync.BuildNotificationError, "push succeeded"):
                github_sync.sync(bundle=str(self.bundle), repository_id="repo-two", notify_build=True, print_fn=lambda *_: None)
            github_sync.sync(bundle=str(self.bundle), repository_id="repo-two", notify_build=True, print_fn=lambda *_: None)
        self.assertEqual(build.call_count, 2)
        self.assertEqual(self.push.call_count, 2)

    def test_failed_push_never_requests_build(self):
        self.push.side_effect = github_sync.SyncError("push rejected")
        with patch.object(github_sync, "request_cloud_build") as build:
            with self.assertRaisesRegex(github_sync.SyncError, "push rejected"):
                github_sync.sync(bundle=str(self.bundle), repository_id="repo-two", notify_build=True, print_fn=lambda *_: None)
        build.assert_not_called()

    def test_invalid_token_response_never_reaches_git(self):
        self.token.return_value = {"token": "fake"}
        with self.assertRaisesRegex(github_sync.SyncError, "invalid sync token"):
            github_sync.sync(bundle=str(self.bundle), repository_id="repo-two", print_fn=lambda *_: None)
        self.pull.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class BundleResolutionTests(unittest.TestCase):
    """`just vault <path>` / the web UI write ~/.kal/config.json; sync read only KAL_VAULT and said
    "no bundle configured" on a device that was configured (2026-09-30)."""

    def test_config_vault_is_used_when_the_environment_has_none(self):
        import kal_config
        import ledger
        with tempfile.TemporaryDirectory() as t, \
                patch.object(device_auth, "load_credential", return_value={
                    "url": "https://example.invalid", "token": "t", "device_name": "d"}), \
                patch.object(ledger, "bundle_root", return_value=None), \
                patch.object(kal_config, "path_override", return_value=t) as override:
            with self.assertRaisesRegex(github_sync.SyncError, "not a git checkout") as caught:
                github_sync._sync()
            override.assert_called_with("vault")
            self.assertIn(t, str(caught.exception))

    def test_nothing_configured_names_both_ways_to_set_it(self):
        import kal_config
        import ledger
        with patch.object(device_auth, "load_credential", return_value={
                    "url": "https://example.invalid", "token": "t", "device_name": "d"}), \
                patch.object(ledger, "bundle_root", return_value=None), \
                patch.object(kal_config, "path_override", return_value=None):
            with self.assertRaisesRegex(github_sync.SyncError, "KAL_VAULT or `just vault"):
                github_sync._sync()
