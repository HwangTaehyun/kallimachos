import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


class ExtractionRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.TemporaryDirectory()
        cls.environment = patch.dict(os.environ, {
            "HOME": cls.root.name, "KAL_HOME": cls.root.name, "KAL_VAULT": cls.root.name,
            "KAL_PATH": str(Path(cls.root.name, "db")), "KAL_NO_LLM": "", "KAL_SKIP": "",
            "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "KAL_MCP_WRITE": "0",
        })
        cls.environment.start()
        cls.lr = importlib.import_module("lr_extract")

    @classmethod
    def tearDownClass(cls):
        cls.environment.stop()
        cls.root.cleanup()

    def setUp(self):
        self.chunk = {"doc": "note.md", "idx": 0, "h": "synthetic-hash", "text": "Synthetic note."}
        self.good = {**self.chunk, "pv": self.lr.PROMPT_VERSION, "model": "haiku",
                     "entities": [{"name": "old"}], "relationships": []}

    def test_obsolete_missing_version_failed_and_bad_rows_are_pending(self):
        bad_rows = [
            {**self.good, "pv": "obsolete"},
            {k: v for k, v in self.good.items() if k != "pv"},
            {**self.good, "failed": True},
            {k: v for k, v in self.good.items() if k != "relationships"},
            {**self.good, "entities": [1]},
            {**self.good, "relationships": [{"source": "x"}]},
            {**self.good, "idx": False},
            {**self.good, "pv": []},
            {"h": "synthetic-hash"},
        ]
        for row in bad_rows:
            for model in (None, "haiku"):
                with self.subTest(row=row, model=model), \
                     patch.object(self.lr, "collect", return_value=[self.chunk]), \
                     patch.object(self.lr, "_load_cache_lines", return_value=[(0, row)]):
                    result = self.lr.pending_extraction(reextract_model=model)
                    self.assertEqual(result["pending_chunks"], [self.chunk])
                    self.assertNotIn(self.lr._done_key(self.chunk), self.lr.pick_current([(0, row)]))

    def test_latest_valid_result_and_pending_use_same_stored_version(self):
        rows = [(0, self.good),
                (1, {**self.good, "model": "agent:sonnet", "entities": [{"name": "current"}]}),
                (2, {**self.good, "failed": True}),
                (3, {**self.good, "pv": "obsolete", "entities": [{"name": "obsolete"}]}),
                (4, {**self.good, "entities": "bad"})]
        current = self.lr.pick_current(rows)
        self.assertEqual(current[self.lr._done_key(self.chunk)]["entities"], [{"name": "current"}])
        with patch.object(self.lr, "collect", return_value=[self.chunk]), \
             patch.object(self.lr, "_load_cache_lines", return_value=rows):
            self.assertEqual(self.lr.pending_extraction()["pending_count"], 0)
            self.assertEqual(self.lr.pending_extraction("agent:sonnet")["pending_count"], 0)
            self.assertEqual(self.lr.pending_extraction("sonnet")["pending_count"], 1)

    def _run_cli(self, argv, cached, chunks=None, reply=None):
        with tempfile.TemporaryDirectory() as root, contextlib.ExitStack() as stack:
            cache = Path(root, "lr_cache.jsonl")
            cache.write_text("".join(json.dumps(row) + "\n" for row in cached))
            for name, value in (("KAL_HOME", root), ("VAULT", root), ("CACHE", str(cache)),
                                ("OUT", str(Path(root, "lr_kg.json"))), ("MODEL", "haiku")):
                stack.enter_context(patch.object(self.lr, name, value))
            stack.enter_context(patch.dict(os.environ, {"KAL_HOME": root, "KAL_VAULT": root}))
            stack.enter_context(patch.object(sys, "argv", ["lr_extract.py", *argv]))
            stack.enter_context(patch.object(self.lr, "collect", return_value=chunks or [self.chunk]))
            stack.enter_context(patch.object(self.lr, "effective_workers", return_value=1))
            boundary = stack.enter_context(patch.object(
                self.lr, "claude_cli_run", return_value=reply if reply is not None else
                json.dumps({"entities": [{"name": "new"}], "relationships": []})))
            stack.enter_context(patch.object(self.lr.time, "sleep"))
            group = stack.enter_context(patch.object(self.lr, "group_nodes", return_value=([], [])))
            summaries = stack.enter_context(patch.object(self.lr, "summarize_all"))
            summaries.models = []

            def summarize(*args):
                summaries.models.append(self.lr.MODEL)
                return 0, 0

            summaries.side_effect = summarize
            writer = stack.enter_context(patch.object(self.lr, "write_kg"))
            stack.enter_context(patch("ledger.append"))
            stack.enter_context(patch("run_log.count"))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            failure = None
            try:
                self.lr._main_extract()
            except SystemExit as error:
                failure = error
            self.assertEqual(self.lr.MODEL, "haiku")
            rows = [json.loads(line) for line in cache.read_text().splitlines()]
            return boundary, group, summaries, writer, rows, failure

    def test_reextract_model_calls_selected_model_and_replaces_current_result_once(self):
        for args in (["--reextract-model", "sonnet", "--no-summary"],
                     ["1", "--no-summary", "--reextract-model", "sonnet"],
                     ["--reextract-model=sonnet", "1", "--no-summary"]):
            with self.subTest(args=args):
                boundary, group, summaries, writer, rows, failure = self._run_cli(args, [self.good])
                self.assertIsNone(failure)
                self.assertEqual(boundary.call_count, 1)
                self.assertEqual(boundary.call_args.args[0], "sonnet")
                self.assertEqual(len(group.call_args.args[0]), 1)
                current = group.call_args.args[0][0]
                self.assertEqual((current["model"], current["pv"], current["entities"]),
                                 ("sonnet", self.lr.PROMPT_VERSION, [{"name": "new"}]))
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[-1]["model"], "sonnet")
                self.assertEqual(writer.call_args.kwargs["extra"], {"chunks": 1, "failed": 0})
                summaries.assert_not_called()

    def test_target_model_reuses_its_valid_row_not_another_models_later_row(self):
        sonnet = {**self.good, "model": "sonnet", "entities": [{"name": "selected"}]}
        boundary, group, _, _, rows, failure = self._run_cli(
            ["--reextract-model", "sonnet", "--no-summary"], [sonnet, self.good])
        self.assertIsNone(failure)
        boundary.assert_not_called()
        self.assertEqual(group.call_args.args[0], [sonnet])
        self.assertEqual(len(rows), 2)

    def test_legacy_model_missing_does_not_become_selected_model(self):
        legacy = {k: v for k, v in self.good.items() if k != "model"}
        boundary, group, _, _, _, failure = self._run_cli(
            ["--reextract-model", "sonnet", "--no-summary"], [legacy])
        self.assertIsNone(failure)
        self.assertEqual(boundary.call_count, 1)
        self.assertEqual(group.call_args.args[0][0]["model"], "sonnet")

    def test_cli_old_prompt_and_failed_cache_rows_are_retried(self):
        for row in ({**self.good, "pv": "obsolete"}, {**self.good, "failed": True},
                    {**self.good, "relationships": "bad"}):
            with self.subTest(row=row):
                boundary, group, _, _, _, failure = self._run_cli(["--no-summary"], [row])
                self.assertIsNone(failure)
                self.assertEqual(boundary.call_count, 1)
                self.assertEqual(len(group.call_args.args[0]), 1)
                self.assertEqual(group.call_args.args[0][0]["entities"], [{"name": "new"}])

    def test_failed_reextraction_does_not_double_count_old_current_row(self):
        boundary, group, _, writer, rows, failure = self._run_cli(
            ["--reextract-model", "sonnet", "--no-summary"], [self.good], reply="")
        self.assertIsInstance(failure, SystemExit)
        self.assertEqual(boundary.call_count, 3)
        self.assertEqual(len(group.call_args.args[0]), 1)
        self.assertTrue(group.call_args.args[0][0]["failed"])
        self.assertEqual(writer.call_args.kwargs["extra"], {"chunks": 1, "failed": 1})
        self.assertEqual(rows, [self.good])

    def test_bad_cli_arguments_fail_before_collect_or_llm(self):
        for args in (["--reextract-model"], ["--unknown"], ["not-a-limit"], ["-1"]):
            with self.subTest(args=args), patch.object(sys, "argv", ["lr_extract.py", *args]), \
                 patch.object(self.lr, "collect") as collect, \
                 patch.object(self.lr, "claude_cli_run") as boundary, \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.lr._main_extract()
                collect.assert_not_called()
                boundary.assert_not_called()

    def test_selected_model_also_reaches_summary_and_zero_limit_is_empty(self):
        boundary, _, summaries, _, _, failure = self._run_cli(["--reextract-model", "sonnet"], [])
        self.assertIsNone(failure)
        self.assertEqual(boundary.call_args.args[0], "sonnet")
        self.assertEqual(summaries.models, ["sonnet"])
        boundary, group, _, writer, _, failure = self._run_cli(["0", "--no-summary"], [])
        self.assertIsNone(failure)
        boundary.assert_not_called()
        self.assertEqual(group.call_args.args[0], [])
        self.assertEqual(writer.call_args.kwargs["extra"], {"chunks": 0, "failed": 0})

    def test_scope_check_remains_read_only(self):
        with patch.object(sys, "argv", ["lr_extract.py", "--check-scope"]), \
             patch.object(self.lr, "check_scope", return_value=0) as check, \
             patch.object(self.lr, "collect") as collect, \
             patch.object(self.lr, "claude_cli_run") as boundary:
            with self.assertRaises(SystemExit) as error:
                self.lr._main_extract()
            self.assertEqual(error.exception.code, 0)
            check.assert_called_once()
            collect.assert_not_called()
            boundary.assert_not_called()

    def test_positional_limit_restart_and_summary_contract(self):
        chunks = [self.chunk, {**self.chunk, "idx": 1}]
        boundary, group, summaries, writer, rows, failure = self._run_cli(
            ["1", "--restart", "--allow-shrink"], [self.good], chunks=chunks)
        self.assertIsNone(failure)
        self.assertEqual(boundary.call_count, 1)
        self.assertEqual(len(group.call_args.args[0]), 1)
        self.assertEqual(len(rows), 1)
        summaries.assert_called_once()
        self.assertTrue(writer.call_args.kwargs["allow_shrink"])


if __name__ == "__main__":
    unittest.main()
