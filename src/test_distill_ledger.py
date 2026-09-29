"""The synced ledger answers "is this session done?" when the local .done marker is missing
(reinstall) or was written by another device (DESIGN-GITHUB-SYNC.md §3.3)."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import distill_sessions as D
import ingest_sessions as I

LAST = "2020-01-02T00:00:00Z"


class LedgerDoneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kal-distill-ledger-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out, self.bundle = self.root / "distilled", self.root / "bundle"
        self.done = self.out / ".done"
        self.done.mkdir(parents=True)
        (self.bundle / ".kal-sync/ledger").mkdir(parents=True)
        for target in (patch.dict(os.environ, {"HOME": str(self.root), "KAL_VAULT": str(self.bundle),
                                               "KAL_DEVICE": "device-b"}, clear=True),
                       patch.object(D, "OUT", str(self.out)), patch.object(D, "DONE", str(self.done)),
                       patch.object(D, "REVIEW", str(self.out / "review.tsv")),
                       patch.object(I, "EXCLUDE", str(self.root / "exclude.txt"))):
            target.start()
            self.addCleanup(target.stop)

    def rec(self, last=LAST):
        return {"agent": "claude", "session_id": "s1", "project": "fixture", "text": "transcript",
                "first_ts": "2020-01-01T00:00:00Z", "last_ts": last, "n_msg": 2}

    def ledger_row(self, device="device-a", last=LAST, **extra):
        with open(self.bundle / f".kal-sync/ledger/{device}.jsonl", "a") as fh:
            fh.write(json.dumps({"source_type": "claude_session", "source_id": "s1", "seq": 0,
                                 "source_updated_at": last, "distilled_through_last_ts": I.epoch(last),
                                 **extra}) + "\n")

    def own_rows(self):
        path = self.bundle / ".kal-sync/ledger/device-b.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def run_grown(self, **row):
        """A session done only via device-a's ledger row that has since grown; the model is mocked."""
        self.ledger_row(**row)
        grown = self.rec("2020-01-05T00:00:00Z")
        grown["abs_path"] = str(self.root / "raw.jsonl")
        reply = ("<<<DOC>>>\ntitle: New conclusion\ndoc_type: decision\nwhy_captured: Review\n"
                 "tags: fixture\n---\nA new reviewed decision.\n<<<END>>>")
        tail = {"text": "later turns", "n_msg": 2, "last_ts": grown["last_ts"]}
        with patch.object(D, "continuation_tail", return_value=tail), \
                patch.object(D, "corpus_records", return_value=([grown], [])), \
                patch.object(sys, "argv", ["distill_sessions.py", "--workers", "1"]), \
                patch.object(D, "call", return_value=reply) as model, \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                D._main_distill()
            except SystemExit:
                pass
            first = model.call_count
            try:
                D._main_distill()
            except SystemExit:
                pass
        return first, model.call_count - first

    def test_ledger_only_grown_session_is_continued_once_without_a_local_marker(self):
        first, second = self.run_grown(pages=["a-page"])
        self.assertEqual((first, second), (1, 0))
        info = json.loads((self.done / "claude-s1").read_text())
        self.assertEqual(info["pages"][0], "a-page")
        self.assertEqual(len(info["pages"]), 2)
        row = self.own_rows()[-1]
        self.assertEqual(row["continues"], "a-page")          # the link to device A's page
        self.assertEqual(row["pages"], info["pages"])

    def test_older_ledger_row_without_pages_continues_without_a_link(self):
        self.assertEqual(self.run_grown(), (1, 0))
        self.assertNotIn("continues", self.own_rows()[-1])

    def test_first_distillation_row_lists_its_pages(self):
        D.mark_done(self.rec(), [("a-page", "x")])
        self.assertEqual(self.own_rows()[-1]["pages"], ["a-page"])

    def test_marker_without_ledger_row_is_backfilled(self):
        (self.done / "claude-s1").write_text(json.dumps({"last_ts": LAST, "pages": ["a-page"]}))
        self.run_main(self.rec()).assert_not_called()
        row = self.own_rows()[-1]
        self.assertEqual((row["source_id"], row["pages"], row["distilled_through_last_ts"]),
                         ("s1", ["a-page"], I.epoch(LAST)))

    def test_unreadable_ledger_warns_once_and_fails_in_staged_mode(self):
        with patch.object(D.ledger, "status_of", side_effect=OSError("boom")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            D._LEDGER_WARNED = False
            self.assertIsNone(D._ledger_row(self.rec()))
            self.assertIsNone(D._ledger_row(self.rec()))
        self.assertEqual(err.getvalue().count("ledger unreadable"), 1)
        with patch.object(D.ledger, "status_of", side_effect=OSError("boom")), \
                patch.dict(os.environ, {"KAL_LEDGER_STAGE": "x"}), self.assertRaises(OSError):
            D._ledger_row(self.rec())

    def staged(self):
        """Sync's staged mode: rows go to a private pending file, not the ledger."""
        state = self.root / "state"
        state.mkdir(exist_ok=True)
        env = patch.dict(os.environ, {"KAL_HOME": str(state), "KAL_LEDGER_REPOSITORY": "github.com/x/y"})
        env.start()
        self.addCleanup(env.stop)
        path = D.ledger.prepare_pending(str(self.bundle), "device-b", "github.com/x/y")
        stage = patch.dict(os.environ, {"KAL_LEDGER_STAGE": path})
        stage.start()
        self.addCleanup(stage.stop)

    def test_session_only_in_the_pending_file_is_done(self):
        self.staged()
        self.assertFalse(D.is_done(self.rec()))
        D.ledger.append(D._ledger_row_of(self.rec(), [("a-page", "x")]))   # crash before the marker
        self.assertTrue(D.is_done(self.rec()))
        self.assertFalse((self.done / "claude-s1").exists())
        self.run_main(self.rec()).assert_not_called()

    def test_backfill_stages_all_rows_with_one_pending_write(self):
        self.staged()
        recs = []
        for n in range(3):
            rec = {**self.rec(), "session_id": f"s{n}"}
            (self.done / f"claude-s{n}").write_text(json.dumps({"last_ts": LAST, "pages": [f"p{n}"]}))
            recs.append(rec)
        with patch.object(D.ledger, "_write_pending", wraps=D.ledger._write_pending) as write:
            D._backfill_ledger(recs)
        self.assertEqual(write.call_count, 1)
        self.assertEqual(sorted(k[1] for k in D.ledger.pending_keys()), ["s0", "s1", "s2"])
        write.reset_mock()
        D._backfill_ledger(recs)                # already staged: nothing to write
        self.assertEqual(write.call_count, 0)

    def test_pages_from_another_device_are_validated(self):
        self.ledger_row(pages=["a-page", "evil\nno_llm: false", "../x", "a:b", "ok-v2", 7, ""])
        self.assertEqual(D._prior_pages(self.rec()), ["a-page", "ok-v2"])
        self.ledger_row(last="2020-01-03T00:00:00Z", pages=["bad\nsession_id: x"])
        self.assertEqual(D._prior_pages(self.rec()), [])
        self.run_grown(pages=["bad\nsession_id: x"])
        self.assertNotIn("continues", self.own_rows()[-1])
        for page in self.out.glob("*.md"):
            self.assertNotIn("session_id: x", page.read_text())

    def test_no_llm_ledger_row_stops_continuation_model_calls(self):
        self.assertEqual(self.run_grown(pages=["a-page"], no_llm=True), (0, 0))

    def test_earlier_row_pages_link_a_continuation_when_the_newest_row_has_none(self):
        self.ledger_row(last="2020-01-01T12:00:00Z", pages=["a-page"])
        self.run_grown()                        # newer row, no pages
        self.assertEqual(self.own_rows()[-1]["continues"], "a-page")

    def run_main(self, rec):
        with patch.object(D, "corpus_records", return_value=([rec], [])), \
                patch.object(sys, "argv", ["distill_sessions.py", "--workers", "1"]), \
                patch.object(D, "call", return_value="SKIP") as model, \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                D._main_distill()
            except SystemExit:
                pass
        return model

    def test_reinstall_with_ledger_done_makes_no_model_call(self):
        self.ledger_row()                       # no local marker at all
        self.assertTrue(D.is_done(self.rec()))
        self.assertFalse(D.grew(self.rec()))
        self.run_main(self.rec()).assert_not_called()

    def test_session_published_by_another_device_is_skipped_here(self):
        self.ledger_row("device-a")
        self.run_main(self.rec()).assert_not_called()
        self.assertFalse(list(self.done.iterdir()))

    def test_no_row_no_marker_is_pending_and_calls_the_model(self):
        self.assertFalse(D.is_done(self.rec()))
        self.assertTrue(self.run_main(self.rec()).called)

    def test_grown_session_resumes_as_continuation_candidate(self):
        self.ledger_row()
        grown = self.rec("2020-01-05T00:00:00Z")
        grown["abs_path"] = str(self.root / "raw.jsonl")
        self.assertTrue(D.is_done(grown) and D.grew(grown))
        self.assertEqual(D.continuation_candidates([grown]), [(grown, I.epoch(LAST))])

    def test_no_llm_dedupe_rows_only_count_when_present(self):
        (self.bundle / ".kal-sync/ledger/device-a.jsonl").write_text("not json\n")
        self.assertFalse(D.is_done(self.rec()))

    def test_no_corpus_in_device_context_is_a_noop_success(self):
        for env, code in (({"KAL_DEVICE": "device-b"}, 0), ({}, 1)):
            with patch.dict(os.environ, env, clear=False), \
                    patch.object(D, "corpus_records", return_value=([], [])), \
                    patch.object(sys, "argv", ["distill_sessions.py"]), \
                    contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
                if not env:
                    os.environ.pop("KAL_DEVICE", None)
                with self.assertRaises(SystemExit) as raised:
                    D._main_distill()
            self.assertEqual(raised.exception.code, code)
            self.assertIn("no session corpus", err.getvalue())
            self.assertNotIn("no session corpus", out.getvalue())


if __name__ == "__main__":
    unittest.main()
