import json
import unittest

import test_sync_ledger_stage as fixture


class ContinuationSyncTests(unittest.TestCase):
    setUp = fixture.StagedLedgerSyncTests.setUp
    git = fixture.StagedLedgerSyncTests.git
    sync = fixture.StagedLedgerSyncTests.sync
    counts = fixture.StagedLedgerSyncTests.counts

    def initial_sync(self):
        self.settings.write_text(json.dumps({"bad_fails": False, "extract_fails": False}))
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.counts(), {"GOOD": 1, "BAD": 1, "extract": 2})

    def append_tail(self):
        raw = self.home / ".claude/projects/fixture-project/good.jsonl"
        with raw.open("a") as output:
            output.write(json.dumps({"type": "user", "timestamp": "2020-01-02T00:00:00Z", "message": {
                "content": "TAIL_GREW PRIVATE_TAIL_CANARY " + "A private conclusion must never reach the model or a published page. " * 40}}) + "\n")

    def block_published(self, rename=False):
        page = self.bundle / "personal/sessions/test-device/good-note.md"
        content = page.read_text().replace("\ntitle:", "\nno_llm: true\ntitle:", 1)
        if rename:
            content = content.replace('title: "Good note"', 'title: "Human renamed conclusion"')
            destination = page.with_name("human-renamed.md")
            page.rename(destination)
            page = destination
        page.write_text(content)
        self.git(self.bundle, "add", ".")
        self.git(self.bundle, "commit", "-m", "privacy review")
        self.git(self.bundle, "push", str(self.remote), "HEAD:main")
        return page

    def assert_private_sync(self, rename=False):
        self.initial_sync()
        page = self.block_published(rename)
        before = page.read_bytes()
        marker = self.state / "distilled/.done/claude-good"
        progress = marker.read_bytes()
        self.append_tail()
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.counts(), {"GOOD": 1, "BAD": 1, "extract": 2})
        self.assertEqual(page.read_bytes(), before)
        self.assertEqual(marker.read_bytes(), progress)
        self.assertIn("no_llm: true", (self.state / "distilled/good-note.md").read_text())
        for root in (self.bundle, self.state / "distilled"):
            for candidate in root.rglob("*.md"):
                self.assertNotIn("PRIVATE_TAIL_CANARY", candidate.read_text())
                self.assertNotIn("good_tail", candidate.read_text())
        for _ in range(3):
            result = self.sync()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(self.counts(), {"GOOD": 1, "BAD": 1, "extract": 2})
            self.assertEqual(page.read_bytes(), before)
            if rename:
                self.assertFalse(page.with_name("good-note.md").exists())
        self.assertEqual(self.git(self.bundle, "status", "--porcelain"), "")

    def test_committed_published_flag_blocks_tail_before_distillation_and_extraction(self):
        self.assert_private_sync()

    def test_renamed_published_page_and_title_cannot_bypass_session_privacy(self):
        self.assert_private_sync(rename=True)

    def test_quoted_published_privacy_blocks_tail_without_changing_page(self):
        self.initial_sync()
        page = self.bundle / "personal/sessions/test-device/good-note.md"
        reviewed = page.read_bytes() + b"\nReviewed human conclusion stays verbatim.\n"
        marker = self.state / "distilled/.done/claude-good"
        progress = marker.read_bytes()
        cache = self.bundle / ".kal-sync/extract/test-device.lr_cache.jsonl"
        cache_bytes = cache.read_bytes()
        calls = self.counts()
        self.append_tail()
        for quote in ('"', "'"):
            for value in ("true", "True", "TRUE"):
                with self.subTest(quote=quote, value=value):
                    flag = f"\n{quote}no_llm{quote}: {value}\ntitle:".encode()
                    page.write_bytes(reviewed.replace(b"\ntitle:", flag, 1))
                    self.git(self.bundle, "add", ".")
                    self.git(self.bundle, "commit", "-m", "quoted published privacy")
                    self.git(self.bundle, "push", str(self.remote), "HEAD:main")
                    before = page.read_bytes(), page.stat().st_mtime_ns
                    for _ in range(3):
                        result = self.sync()
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual((page.read_bytes(), page.stat().st_mtime_ns), before)
                        self.assertEqual(self.counts(), calls)
                        self.assertEqual(marker.read_bytes(), progress)
                        self.assertEqual(cache.read_bytes(), cache_bytes)
                        self.assertEqual(self.git(self.bundle, "status", "--porcelain"), "")
        for root in (self.bundle / "personal/sessions", self.state / "distilled"):
            for candidate in root.rglob("*.md"):
                self.assertNotIn("PRIVATE_TAIL_CANARY", candidate.read_text())
                self.assertNotIn("good_tail", candidate.read_text())

    def test_existing_continuation_gets_only_monotonic_privacy_metadata(self):
        self.assert_existing_continuation_privacy(False)

    def test_renamed_existing_continuation_gets_only_monotonic_privacy_metadata(self):
        self.assert_existing_continuation_privacy(True)

    def assert_existing_continuation_privacy(self, rename):
        self.initial_sync()
        self.append_tail()
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        marker = json.loads((self.state / "distilled/.done/claude-good").read_text())
        continuation = self.bundle / "personal/sessions/test-device" / (marker["pages"][-1] + ".md")
        self.assertNotIn(b"no_llm: true", continuation.read_bytes())
        content = continuation.read_bytes().replace(b"\ntitle:", b"\nreviewed_by: human\nno_llm: false # reviewed policy\ntitle:", 1)
        content += b"\nPRIVATE_EXISTING_FACT: a human correction must remain verbatim.\n"
        if rename:
            moved = continuation.with_name("reviewed-continuation.md")
            continuation.rename(moved)
            continuation = moved
        continuation.write_bytes(content)
        original = self.block_published(rename=rename)
        original_bytes = original.read_bytes()
        cache = self.bundle / ".kal-sync/extract/test-device.lr_cache.jsonl"
        cache_bytes = cache.read_bytes()
        calls = self.counts()
        expected = content.replace(b"no_llm: false # reviewed policy", b"no_llm: true # reviewed policy")
        for _ in range(3):
            result = self.sync()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(continuation.read_bytes(), expected)
            self.assertEqual(original.read_bytes(), original_bytes)
            self.assertEqual(self.counts(), calls)
            self.assertEqual(cache.read_bytes(), cache_bytes)
            self.assertEqual(self.git(self.bundle, "status", "--porcelain"), "")
        self.assertNotIn(b"PRIVATE_EXISTING_FACT", cache.read_bytes())
        if rename:
            self.assertFalse(continuation.with_name(marker["pages"][-1] + ".md").exists())
            self.assertFalse(original.with_name("good-note.md").exists())

    def test_skip_continuation_advances_staged_ledger_without_replacing_pages(self):
        self.initial_sync()
        fake = self.binary / "claude"
        fake.write_text(fake.read_text().replace('if extract:\n', 'if label == "GOOD_TAIL":\n    print("SKIP")\nelif extract:\n'))
        page = self.bundle / "personal/sessions/test-device/good-note.md"
        before = page.read_bytes()
        marker = self.state / "distilled/.done/claude-good"
        pages = json.loads(marker.read_text())["pages"]
        self.append_tail()
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        info = json.loads(marker.read_text())
        self.assertEqual(info, {"last_ts": "2020-01-02T00:00:00Z", "pages": pages})
        self.assertEqual(page.read_bytes(), before)
        rows = [json.loads(line) for line in self.own.read_text().splitlines()]
        good = [row for row in rows if row.get("source_id") == "good"]
        self.assertEqual(len(good), 2)
        self.assertGreater(good[-1]["distilled_through_last_ts"], good[0]["distilled_through_last_ts"])
        self.assertEqual(good[-1]["continues"], "good-note")
        self.assertEqual(self.counts(), {"GOOD": 1, "BAD": 1, "GOOD_TAIL": 1, "extract": 2})
        baseline = self.own.read_bytes()
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.counts(), {"GOOD": 1, "BAD": 1, "GOOD_TAIL": 1, "extract": 2})
        self.assertEqual(self.own.read_bytes(), baseline)
        self.assertEqual(self.git(self.bundle, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
