import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SRC = Path(__file__).resolve().parent


class DeviceEmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kal-device-emit-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / "bundle"
        self.source = self.root / "distilled"
        self.home = self.root / "state"
        self.bundle.mkdir()
        self.source.mkdir()
        self.home.mkdir()
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("KAL_", "ANTHROPIC_", "CLAUDE_", "GIT_"))
            and key not in ("PYTHONPATH", "PYTHONHOME")
        }
        self.env.update(HOME=str(self.root), KAL_HOME=str(self.home), KAL_VAULT=str(self.bundle),
                        KAL_DISTILLED=str(self.source), KAL_DEVICE="test-device", PYTHONPATH=str(SRC),
                        GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
                        HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        self.git("init", "-b", "main")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.invalid")
        self.protected = {
            "index.md": "Human root index\n",
            "personal/index.md": "Human personal index\n",
            "personal/sessions/index.md": "Human sessions index\n",
            "personal/sessions/other/page.md": "Other device's content\n",
            "personal/sessions/other/index.md": "Other device's index\n",
            ".page-manifest.json": '{"human":"manifest"}\n',
        }
        for relative, content in self.protected.items():
            target = self.bundle / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self.git("add", ".")
        self.git("commit", "-m", "seed")
        (self.source / "example.md").write_text(
            '---\ntitle: Example\ntype: conversation\nsession_id: fake-session\n'
            'session_agent: claude\n---\nA safe conclusion.\n'
        )

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.bundle), *args], env=self.env,
                              check=True, capture_output=True, text=True).stdout

    def emit(self, *args):
        command = [sys.executable, str(SRC / "openwiki_emit.py")]
        mutation = os.environ.get("KAL_TEST_EMIT_MUTATION")
        if mutation:
            code = 'import openwiki_emit as E; import sys\n'
            if mutation == "privacy-noop":
                code += 'E._privacy_metadata_only = lambda text: text\n'
            elif mutation == "rename-noop":
                code += 'original = E._device_plan\ndef broken(wiki, root, plan):\n    _, snapshots = original(wiki, root, plan)\n    return plan, snapshots\nE._device_plan = broken\n'
            else:
                raise AssertionError("unknown mutation")
            command = [sys.executable, "-c", code + 'sys.exit(E.main())']
        return subprocess.run([*command, "--wiki", str(self.bundle), "--from", str(self.source), *args], env=self.env,
                              capture_output=True, text=True)

    def test_device_mode_preserves_all_shared_and_other_device_files(self):
        result = self.emit("--device", "test-device")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.bundle / "personal/sessions/test-device/example.md").is_file())
        for relative, content in self.protected.items():
            self.assertEqual((self.bundle / relative).read_text(), content, relative)
        self.assertEqual(self.git("ls-files", "--others", "--exclude-standard").splitlines(),
                         ["personal/sessions/test-device/example.md"])
        self.assertEqual(self.git("diff", "--name-only"), "")

    def test_empty_settled_corpus_is_a_device_noop(self):
        (self.source / "example.md").unlink()
        result = self.emit("--device", "test-device")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git("status", "--porcelain"), "")
        for relative, content in self.protected.items():
            self.assertEqual((self.bundle / relative).read_text(), content, relative)
        self.assertNotEqual(self.emit().returncode, 0)

    def fill_source(self, n):
        for i in range(n):
            (self.source / f"extra{i}.md").write_text(
                f'---\ntitle: Extra {i}\ntype: conversation\nsession_id: fake-{i}\n'
                'session_agent: claude\n---\nA safe conclusion.\n')

    def test_shrink_guard_is_skipped_in_device_mode_and_refusals_reach_stderr(self):
        self.fill_source(5)
        self.assertEqual(self.emit("--device", "test-device").returncode, 0)
        self.git("add", ".")
        self.git("commit", "-m", "published")
        for path in self.source.glob("extra*.md"):
            path.unlink()      # e.g. a reinstalled device: far fewer pages than the bundle holds
        result = self.emit("--device", "test-device")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(list((self.bundle / "personal/sessions/test-device").glob("*.md"))), 6)
        # non-device mode keeps the guard, and says so on stderr
        self.assertEqual(self.emit().returncode, 0)
        self.git("add", ".")
        self.git("commit", "-m", "normal")
        self.fill_source(5)
        self.assertEqual(self.emit().returncode, 0)
        for path in self.source.glob("*.md"):
            path.unlink()
        (self.source / "one.md").write_text(
            '---\ntitle: One\ntype: conversation\n---\nbody\n')
        result = self.emit()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("would disappear", result.stderr)

    def test_normal_emission_still_builds_indexes_and_manifest(self):
        result = self.emit()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.bundle / "personal/sessions/claude/index.md").is_file())
        self.assertIn("pages", json.loads((self.bundle / ".page-manifest.json").read_text()))
        self.assertNotEqual((self.bundle / "index.md").read_text(), self.protected["index.md"])

    def test_device_cannot_choose_another_destination(self):
        for args in (("--device", "../other"), ("--device", "test-device", "--into", "personal/shared")):
            result = self.emit(*args)
            self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_device_directory_symlink_to_other_device_is_refused(self):
        (self.bundle / "personal/sessions/test-device").symlink_to("other", target_is_directory=True)
        result = self.emit("--device", "test-device")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.bundle / "personal/sessions/other/example.md").exists())
        self.assertEqual((self.bundle / "personal/sessions/other/page.md").read_text(), self.protected["personal/sessions/other/page.md"])

    def test_nested_device_symlink_is_refused_before_any_page_is_created(self):
        nested_source = self.source / "nested/deep"
        nested_source.mkdir(parents=True)
        (nested_source / "new.md").write_text((self.source / "example.md").read_text())
        device = self.bundle / "personal/sessions/test-device"
        device.mkdir()
        (device / "nested").symlink_to("../other", target_is_directory=True)
        self.git("add", ".")
        self.git("commit", "-m", "nested symlink")
        result = self.emit("--device", "test-device")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.bundle / "personal/sessions/other/deep").exists())
        self.assertFalse((device / "example.md").exists())
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_foreign_collision_is_not_overwritten(self):
        target = self.bundle / "personal/sessions/test-device/example.md"
        target.parent.mkdir(parents=True)
        target.write_text("Human note, not an emitted session.\n")
        self.git("add", ".")
        self.git("commit", "-m", "human note")
        result = self.emit("--device", "test-device")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(), "Human note, not an emitted session.\n")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_uncommitted_device_edit_is_not_overwritten(self):
        result = self.emit("--device", "test-device")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.git("add", ".")
        self.git("commit", "-m", "device page")
        target = self.bundle / "personal/sessions/test-device/example.md"
        target.write_text(target.read_text() + "\nHuman correction.\n")
        before = target.read_bytes()
        result = self.emit("--device", "test-device")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_bytes(), before)

    def test_committed_human_edit_of_generated_page_is_not_overwritten(self):
        result = self.emit("--device", "test-device")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        target = self.bundle / "personal/sessions/test-device/example.md"
        target.write_text(target.read_text() + "\nReviewed human correction.\n")
        self.git("add", ".")
        self.git("commit", "-m", "reviewed correction")
        before = target.read_bytes()
        result = self.emit("--device", "test-device")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_device_emission_does_not_delete_previously_published_pages(self):
        (self.source / "older.md").write_text((self.source / "example.md").read_text())
        result = self.emit("--device", "test-device")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.git("add", ".")
        self.git("commit", "-m", "published pages")
        target = self.bundle / "personal/sessions/test-device/older.md"
        before = target.read_bytes()
        (self.source / "older.md").unlink()
        result = self.emit("--device", "test-device", "--force")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def publish_example(self):
        result = self.emit("--device", "test-device")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.git("add", ".")
        self.git("commit", "-m", "published example")
        return self.bundle / "personal/sessions/test-device/example.md"

    def test_reviewed_rename_chain_preserves_body_title_bytes_and_mtime(self):
        target = self.publish_example()
        intermediate = target.with_name("first-rename.md")
        target.rename(intermediate)
        self.git("add", ".")
        self.git("commit", "-m", "first reviewed rename")
        renamed = intermediate.parent / "reviewed/final-name.md"
        renamed.parent.mkdir()
        intermediate.rename(renamed)
        renamed.write_bytes(renamed.read_bytes().replace(b'title: "Example"', b'title: "Reviewed conclusion"') + b"\nHuman correction.\n")
        self.git("add", ".")
        self.git("commit", "-m", "second reviewed rename and correction")
        before = renamed.read_bytes(), renamed.stat().st_mtime_ns
        for _ in range(3):
            result = self.emit("--device", "test-device")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((renamed.read_bytes(), renamed.stat().st_mtime_ns), before)
            self.assertFalse(target.exists())
            self.assertFalse(intermediate.exists())
            self.assertEqual(self.git("status", "--porcelain"), "")

    def test_duplicate_renamed_identity_is_refused_before_any_flag_or_creation(self):
        target = self.publish_example()
        renamed = target.with_name("renamed.md")
        target.rename(renamed)
        duplicate = target.with_name("duplicate.md")
        duplicate.write_bytes(renamed.read_bytes())
        self.git("add", ".")
        self.git("commit", "-m", "ambiguous copies")
        source = self.source / "example.md"
        source.write_text(source.read_text().replace("\ntitle:", "\nno_llm: true\ntitle:", 1))
        before = renamed.read_bytes(), duplicate.read_bytes()
        result = self.emit("--device", "test-device")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ambiguous", result.stderr)
        self.assertIn("duplicate.md", result.stderr)
        self.assertEqual((renamed.read_bytes(), duplicate.read_bytes()), before)
        self.assertFalse(target.exists())
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_preexisting_recreated_slug_reports_identity_collision_not_shrink(self):
        target = self.publish_example()
        content = target.read_bytes()
        renamed = target.with_name("reviewed.md")
        target.rename(renamed)
        self.git("add", ".")
        self.git("commit", "-m", "reviewed rename")
        target.write_bytes(content)
        self.git("add", ".")
        self.git("commit", "-m", "legacy duplicated slug")
        result = self.emit("--device", "test-device")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ambiguous published identity", result.stderr)
        self.assertIn("example.md", result.stderr)
        self.assertIn("reviewed.md", result.stderr)
        self.assertEqual(target.read_bytes(), content)
        self.assertEqual(renamed.read_bytes(), content)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_rename_to_other_device_is_not_adopted_or_recreated(self):
        target = self.publish_example()
        moved = self.bundle / "personal/sessions/other/reviewed.md"
        target.rename(moved)
        self.git("add", ".")
        self.git("commit", "-m", "move outside owned device")
        original = moved.read_bytes()
        result = self.emit("--device", "test-device")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manually flag", result.stderr)
        self.assertEqual(moved.read_bytes(), original)
        self.assertFalse(target.exists())
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_privacy_addition_preserves_crlf_arbitrary_body_and_other_metadata(self):
        target = self.publish_example()
        content = target.read_bytes().replace(b"\ntitle:", b"\nreview: |\n  Keep this wording and formatting.\ntitle:", 1)
        content = content.replace(b"\n", b"\r\n") + b"\r\nReviewed bytes: \x80\r\n"
        target.write_bytes(content)
        self.git("add", ".")
        self.git("commit", "-m", "reviewed byte-preserving page")
        source = self.source / "example.md"
        source.write_text(source.read_text().replace("\ntitle:", '\n"no_llm": true\ntitle:', 1))
        result = self.emit("--device", "test-device")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        expected = content.replace(b"---\r\n", b"---\r\nno_llm: true\r\n", 1)
        self.assertEqual(target.read_bytes(), expected)
        self.git("add", ".")
        self.git("commit", "-m", "monotonic privacy")
        source.write_text(source.read_text().replace('"no_llm": true', "no_llm: false"))
        for _ in range(3):
            result = self.emit("--device", "test-device")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(target.read_bytes(), expected)
            self.assertEqual(self.git("status", "--porcelain"), "")

    def test_quoted_published_privacy_is_an_unchanged_device_noop(self):
        target = self.publish_example()
        reviewed = target.read_bytes() + b"\nReviewed human body stays verbatim.\n"
        for quote in ('"', "'"):
            for value in ("true", "True", "TRUE"):
                with self.subTest(quote=quote, value=value):
                    flag = f"\n{quote}no_llm{quote}: {value}\ntitle:".encode()
                    target.write_bytes(reviewed.replace(b"\ntitle:", flag, 1))
                    self.git("add", ".")
                    self.git("commit", "-m", "quoted privacy review")
                    before = target.read_bytes(), target.stat().st_mtime_ns
                    for _ in range(3):
                        result = self.emit("--device", "test-device")
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual((target.read_bytes(), target.stat().st_mtime_ns), before)
                        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_mixed_quoted_duplicate_privacy_keys_are_refused_unchanged(self):
        target = self.publish_example()
        target.write_bytes(target.read_bytes().replace(b"\ntitle:", b'\n"no_llm": true\n\'no_llm\': TRUE\ntitle:', 1))
        self.git("add", ".")
        self.git("commit", "-m", "ambiguous quoted policy metadata")
        before = target.read_bytes(), target.stat().st_mtime_ns
        for _ in range(3):
            result = self.emit("--device", "test-device")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ambiguous frontmatter keys", result.stderr)
            self.assertEqual((target.read_bytes(), target.stat().st_mtime_ns), before)
            self.assertEqual(self.git("status", "--porcelain"), "")

    def test_ambiguous_privacy_keys_require_manual_review_without_writes(self):
        target = self.publish_example()
        target.write_text(target.read_text().replace("\ntitle:", "\nno_llm: false\nno_llm: true\ntitle:", 1))
        self.git("add", ".")
        self.git("commit", "-m", "ambiguous policy metadata")
        before = target.read_bytes()
        source = self.source / "example.md"
        source.write_text(source.read_text().replace("\ntitle:", "\nno_llm: true\ntitle:", 1))
        result = self.emit("--device", "test-device")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manually", result.stderr)
        self.assertIn("example.md", result.stderr)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_rename_with_lost_body_identity_requires_manual_privacy_review(self):
        target = self.publish_example()
        renamed = target.with_name("reviewed.md")
        target.rename(renamed)
        renamed.write_text(renamed.read_text().replace("A safe conclusion.", "Entirely rewritten human reasoning."))
        self.git("add", ".")
        self.git("commit", "-m", "rename with replaced body identity")
        source = self.source / "example.md"
        source.write_text(source.read_text().replace("\ntitle:", "\nno_llm: true\ntitle:", 1))
        original = renamed.read_bytes()
        result = self.emit("--device", "test-device")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manually flag", result.stderr)
        self.assertIn("reviewed.md", result.stderr)
        self.assertEqual(renamed.read_bytes(), original)
        self.assertFalse(target.exists())

    def test_privacy_refuses_a_page_mode_change_after_planning(self):
        target = self.publish_example()
        source = self.source / "example.md"
        source.write_text(source.read_text().replace("\ntitle:", "\nno_llm: true\ntitle:", 1))
        original = target.read_bytes()
        code = '''
import os
from pathlib import Path
import openwiki_emit as E
original = E._device_plan
def interfere(wiki, root, plan):
    result = original(wiki, root, plan)
    (Path(wiki) / "personal/sessions/test-device/example.md").chmod(0o600)
    return result
E._device_plan = interfere
raise SystemExit(E.main())
'''
        result = subprocess.run([sys.executable, "-c", code, "--wiki", str(self.bundle), "--from", str(self.source),
                                 "--device", "test-device"], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("changed during emission", result.stdout + result.stderr)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_real_distillation_emission_export_commit_and_local_push(self):
        binary = self.root / "bin"
        binary.mkdir()
        stub = binary / "claude"
        stub.write_text("#!/bin/sh\nexit 97\n")
        stub.chmod(0o700)
        self.env["PATH"] = str(binary) + os.pathsep + self.env.get("PATH", "")
        remote = self.root / "remote.git"
        self.git("remote", "add", "origin", "https://github.com/example/bundle.git")
        subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)],
                       check=True, capture_output=True, env=self.env)
        code = r'''
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
import distill_sessions as distill
import device_extract
import lr_extract
import github_sync
import openwiki_emit

bundle = os.environ["KAL_VAULT"]
source = Path(os.environ["KAL_DISTILLED"])
reply = "<<<DOC>>>\ntitle: Offline decision\ndoc_type: decision\nwhy_captured: Test conclusion\ntags: test\n---\nA verified local decision keeps entity extraction on the device and publishes only scoped incremental results.\n<<<END>>>"
record = {"text": "We decided to keep the offline test isolated.", "session_id": "fake-session",
          "agent": "claude", "project": "test", "n_msg": 2, "first_ts": "2020-01-01T00:00:00Z"}
with patch.object(distill, "claude_run", return_value=reply) as llm:
    docs, status = distill.distill(record)
    assert status == "ok"
    made = distill.write(record, docs, {})
    Path(distill.DONE).mkdir(parents=True)
    distill.mark_done(record, made)
    llm.assert_called_once()
sys.argv = ["openwiki_emit", "--wiki", bundle, "--from", str(source), "--device", "test-device"]
assert openwiki_emit.main() == 0
with patch.object(lr_extract, "claude_cli_run", return_value=json.dumps({"entities": [{"name": "Device"}], "relationships": []})) as llm:
    stats = device_extract.process(bundle, "test-device")
    assert stats["extracted"] == stats["exported"] == 1, stats
    assert device_extract.process(bundle, "test-device")["extracted"] == 0
    llm.assert_called_once()
assert github_sync.export_device_extract(bundle, "test-device") == 0
github_sync.check_ownership(bundle, "test-device")
assert github_sync.commit_own_changes(bundle, "test-device")
assert not github_sync.changed_paths(bundle)
assert openwiki_emit.main() == 0
assert not github_sync.changed_paths(bundle)
assert github_sync.push(bundle, os.environ["TEST_REMOTE"], token=None, device="test-device")
assert not github_sync.commit_own_changes(bundle, "test-device")
'''
        result = subprocess.run([sys.executable, "-c", code], env=dict(self.env, TEST_REMOTE=str(remote)),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.bundle / ".kal-sync/extract/test-device.lr_cache.jsonl").is_file())
        self.assertTrue((self.bundle / ".kal-sync/ledger/test-device.jsonl").is_file())
        changed = self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()
        self.assertTrue(changed)
        self.assertTrue(all(path.startswith(("personal/sessions/test-device/", ".kal-sync/ledger/test-device.jsonl",
                                             ".kal-sync/extract/test-device.lr_cache.jsonl")) for path in changed), changed)
        for relative, content in self.protected.items():
            self.assertEqual((self.bundle / relative).read_text(), content, relative)
        self.assertIn("Kal-Host: test-device", self.git("log", "-1", "--pretty=%B"))
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_repeated_emit_keeps_published_page_gated_by_any_key_spelling(self):
        base = self.git("rev-parse", "HEAD").strip()
        for spelling in ('"no_llm": true', "'no_llm': true", "no_llm: true", "no_llm: 'true'"):
            with self.subTest(spelling=spelling):
                self.git("reset", "-q", "--hard", base)
                self.git("clean", "-fdq")
                result = self.emit("--device", "test-device")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                target = self.bundle / "personal/sessions/test-device/example.md"
                target.write_text(target.read_text().replace("\ntitle:", f"\n{spelling}\ntitle:", 1))
                self.git("add", ".")
                self.git("commit", "-q", "-m", "gate page")
                before, mtime = target.read_bytes(), target.stat().st_mtime_ns
                result = self.emit("--device", "test-device")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(target.stat().st_mtime_ns, mtime)
                self.assertEqual(self.git("status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
