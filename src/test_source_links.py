import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pyarrow as pa
import lancedb


class SourceLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "HOME": cls.root.name, "KAL_HOME": cls.root.name, "KAL_VAULT": cls.root.name,
            "KAL_PATH": str(Path(cls.root.name) / "db"), "KAL_NO_LLM": "",
            "KAL_SKIP": "", "KAL_MCP_WRITE": "0",
            "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        })
        cls.env.start()
        cls.schema = importlib.import_module("schema_v3")
        cls.search = importlib.import_module("kal_search")
        cls.mcp = importlib.import_module("kal_mcp")
        cls.sync = importlib.import_module("sync_v3")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.root.cleanup()

    def test_provenance_shapes_and_first_valid_original(self):
        from source_links import source_urls
        for fm in (
            'sources: [{resource: "https://example.org/original?a=1&b=2", type: web}]',
            'sources:\n  - resource: "https://example.org/original?a=1&b=2"\n    type: web',
            'source: "https://example.org/original?a=1&b=2"',
            'resource: "https://example.org/original?a=1&b=2"',
            'source_url: "https://example.org/original?a=1&b=2"',
            'original_url: "https://example.org/original?a=1&b=2"',
        ):
            with self.subTest(fm=fm):
                self.assertEqual(source_urls(f"---\n{fm}\n---\nbody"),
                                 ["https://example.org/original?a=1&b=2"])
        self.assertEqual(source_urls('---\nsources: [{resource: /Users/local/a}, '
                                    '{resource: "https://example.org/a"}, '
                                    '{resource: "https://example.org/b"}, '
                                    '{resource: "https://example.org/a"}]\n---\n'),
                         ["https://example.org/a", "https://example.org/b"])

    def test_urls_never_inferred_from_body_or_unrelated_yaml(self):
        from source_links import source_urls
        for raw in (
            'https://example.org/not-provenance',
            '---\ntitle: note\n---\nsource: https://example.org/body',
            '---\nsummary: |\n  source: https://example.org/summary\n---\n',
            '---\ngenerated: {resource: "https://example.org/generator"}\n---\n',
            '---\nsources: [broken\n---\n',
            '---\nsources: &loop [*loop]\n---\n',
        ):
            with self.subTest(raw=raw):
                self.assertEqual(source_urls(raw), [])

    def test_yaml_aliases_and_merges_never_reach_construction(self):
        import yaml
        from source_links import source_urls
        for fm in (
            'base: &base {x: y}\ncopy: *base',
            'base: &base {x: y}\ncopy: {<<: [*base, *base]}',
            'copy: {<<: {x: y}}',
            "copy: {!!merge '<<': {x: y}}",
            'loop: &loop [*loop]',
        ):
            raw = f'---\n{fm}\nsource: https://example.org/source\n---\n'
            with self.subTest(fm=fm), patch.object(
                    yaml.SafeLoader, "construct_document",
                    side_effect=AssertionError("unsafe YAML reached construction")) as construct:
                self.assertEqual(source_urls(raw), [])
                construct.assert_not_called()

    def test_yaml_depth_and_node_budgets_precede_construction(self):
        import yaml
        from source_links import source_urls
        for fm in ('nested: ' + '[' * 40 + 'x' + ']' * 40,
                   'wide: [' + ','.join(['x'] * 4100) + ']'):
            with self.subTest(kind=fm[:10]), patch.object(
                    yaml.SafeLoader, "construct_document",
                    side_effect=AssertionError("over-budget YAML reached construction")) as construct:
                self.assertEqual(source_urls(f'---\n{fm}\nsource: https://example.org/source\n---\n'), [])
                construct.assert_not_called()

    def test_yaml_bounded_valid_lists_dates_and_multiline_are_unchanged(self):
        from source_links import source_urls, source_resources
        raw = ('---\ncreated: 2026-01-01\nsummary: |\n  aliases *literal and << are prose\n'
               '  not YAML references\nmetadata: {nested: [one, {two: three}]}\n'
               'sources:\n  - resource: https://example.org/source\n    last_modified: 2026-01-02\n'
               '    title: >\n      An ordinary\n      multiline title\n---\nbody')
        self.assertEqual(source_urls(raw), ['https://example.org/source'])
        self.assertEqual(source_resources(raw), ['https://example.org/source'])

    def test_strict_frontmatter_refuses_unsafe_input_while_optional_links_stay_empty(self):
        import yaml
        import source_links as L

        doubling = "base: &b0 {value: canary}\n" + "\n".join(
            f"b{i}: &b{i} {{<<: [*b{i - 1}, *b{i - 1}]}}" for i in range(1, 6))
        for raw in (
            "---\n" + doubling + "\n---\n",
            "---\nsource: [broken\n---\n",
            "---\nsource: unfinished\n",
            "---\nsource: " + "x" * L.MAX_FRONTMATTER_CHARS + "\n---\n",
        ):
            with self.subTest(raw=raw[:50]), patch.object(
                    yaml.SafeLoader, "construct_document",
                    side_effect=AssertionError("unsafe YAML reached construction")) as construct:
                with self.assertRaisesRegex(ValueError, "frontmatter"):
                    L.load_frontmatter(raw)
                self.assertEqual(L.source_urls(raw), [])
                self.assertEqual(L.source_resources(raw), [])
                construct.assert_not_called()
        with self.assertRaisesRegex(ValueError, "mapping"):
            L.load_frontmatter("---\n[not, a, mapping]\n---\n")
        self.assertEqual(L.source_urls("---\n[not, a, mapping]\n---\n"), [])

    def test_strict_frontmatter_returns_valid_mapping_and_limits_only_frontmatter(self):
        import source_links as L

        raw = ('---\n"no_llm": true\nsources: [{resource: "https://example.org/original"}]\n'
               '---\n' + "body " * L.MAX_FRONTMATTER_CHARS)
        self.assertEqual(L.load_frontmatter(raw), {
            "no_llm": True, "sources": [{"resource": "https://example.org/original"}]})
        self.assertEqual(L.source_urls(raw), ["https://example.org/original"])
        self.assertEqual(L.load_frontmatter("plain body"), {})
        self.assertEqual(L.load_frontmatter("---\n\n---\nbody"), {})

    def test_rejects_nonweb_paths_credentials_controls_and_malformed_urls(self):
        from source_links import external_url
        for value in (None, 12, [], "/Users/name/vault/note.md", "C:\\Users\\name\\note.md",
                      "file:///Users/name/a", "obsidian://open?vault=secret", "claude-session://abc",
                      "javascript:alert(1)", "data:text/plain,secret", "//example.org/a",
                      "https:///no-host", "https://", "https://user:secret@example.org/a",
                      "https://example.org:bad/a", "https://[bad/a", "https://example.org/has space",
                      "https://example.org/\nsecret", "https://example.org/\\secret",
                      "https://example.org/" + "x" * 2048):
            with self.subTest(value=value):
                self.assertEqual(external_url(value), "")
        self.assertEqual(external_url("https://example.org/a#part"), "https://example.org/a#part")

    def test_scan_rows_match_schema_and_preserve_privacy_gate(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "source.md").write_text(
                '---\ntitle: Source\nsources: [{resource: "https://example.org/original", type: web}]\n'
                'no_llm: true\n---\n' + 'Synthetic content. ' * 10)
            Path(root, "local.md").write_text(
                '---\nsource: /Users/private/original.md\n---\n' + 'Synthetic content. ' * 10)
            with patch.object(self.schema, "VAULT", root):
                rows = list(self.schema.scan_vault().values())
            data = [{k: v for k, v in row.items() if k != "_body"} for row in rows]
            table = pa.Table.from_pylist(data, schema=self.schema.schemas(2)["documents"])
            by_path = {r["path"]: r for r in table.to_pylist()}
            self.assertEqual(by_path["source.md"]["source_url"], "https://example.org/original")
            self.assertTrue(by_path["source.md"]["no_llm"])
            self.assertEqual(by_path["local.md"]["source_url"], "")
            self.assertEqual(set(data[0]), set(table.schema.names))

    def test_search_exposes_only_valid_original_and_tolerates_old_rows(self):
        kal = object.__new__(self.search.KAL)
        kal.D = {
            1: {"path": "a.md", "source_url": "https://example.org/a"},
            2: {"path": "b.md", "source_url": "/Users/private/a"},
            3: {"path": "c.md"},
        }
        kal.bm25 = lambda *a, **kw: {1: 3, 2: 2, 3: 1}
        kal.chunk_vec = kal.entity_vec = kal.relation_vec = lambda *a, **kw: {}
        with patch.object(self.search, "encode_query", return_value=np.zeros(2)):
            rows, _, _ = kal.search("synthetic", mode="keyword")
        self.assertEqual([r["source_url"] for r in rows], ["https://example.org/a", "", ""])

    def test_mcp_search_metadata_gate_and_no_absolute_source(self):
        rows = [
            {"doc_id": 1, "path": "a.md", "source_url": "https://example.org/a"},
            {"doc_id": 2, "path": "b.md", "source_url": "/Users/private/a"},
            {"doc_id": 3, "path": "secret.md", "source_url": "https://example.org/secret", "no_llm": True},
            {"doc_id": 4, "path": "blocked.md", "source_url": "https://example.org/blocked"},
            {"doc_id": 5, "path": "old.md"},
        ]
        table = Mock()
        table.to_arrow.return_value.to_pylist.return_value = rows
        kal = Mock()
        kal.search.return_value = (rows, "keyword", [])
        kal.snippets.return_value = {}
        with patch.object(self.mcp, "tbl", return_value=table), \
             patch.object(self.mcp, "_gate_by_path", side_effect=lambda p: p == "blocked.md"), \
             patch.object(self.mcp, "fresh"), patch.object(self.mcp, "_DOCS", None), \
             patch.object(self.mcp, "db", return_value=kal):
            hits = self.mcp.kal_search("synthetic", mode="keyword")["hits"]
        self.assertEqual([r["doc_id"] for r in hits], [1, 2, 5])
        self.assertEqual([r["source_url"] for r in hits], ["https://example.org/a", "", ""])
        self.assertNotIn("/Users/", json.dumps(hits))

    def test_refs_resolve_persisted_url_without_vault(self):
        docs = {1: {"doc_id": 1, "path": "gone.md", "date": "", "title": "Source",
                    "source_url": "https://example.org/original"}}
        with patch.object(self.mcp, "_DOCS", docs), patch.object(self.mcp, "_read_in_vault", return_value=None):
            result = self.mcp.refs_of([1])
        self.assertEqual(result["refs_status"], "ok")
        self.assertEqual([r["url"] for r in result["refs"]], ["https://example.org/original"])

    def test_refs_direct_flow_block_and_unresolved(self):
        docs = {1: {"doc_id": 1, "path": "source.md", "date": "", "title": "Source"}}
        for fm in ('sources: [{resource: "https://example.org/a"}]',
                   'sources:\n  - resource: "https://example.org/a"',
                   'source: "https://example.org/a"'):
            with self.subTest(fm=fm), tempfile.TemporaryDirectory() as root:
                Path(root, "source.md").write_text(f"---\n{fm}\n---\nbody")
                with patch.object(self.mcp, "VAULT", root), patch.object(self.mcp, "_VROOT", os.path.realpath(root)), \
                     patch.object(self.mcp, "SRC_DIR", str(Path(root, "wiki", "sources"))), \
                     patch.object(self.mcp, "_DOCS", docs):
                    result = self.mcp.refs_of([1])
                self.assertEqual(result["refs_status"], "ok")
                self.assertEqual([r["url"] for r in result["refs"]], ["https://example.org/a"])
        with tempfile.TemporaryDirectory() as root:
            Path(root, "source.md").write_text('---\nsources:\n  - resource: /missing/ref.md\n---\nbody')
            Path(root, "wiki", "sources").mkdir(parents=True)
            Path(root, "wiki", "sources", "ref.md").write_text('https://example.org/unrelated')
            with patch.object(self.mcp, "VAULT", root), patch.object(self.mcp, "_VROOT", os.path.realpath(root)), \
                 patch.object(self.mcp, "SRC_DIR", str(Path(root, "wiki", "sources"))), \
                 patch.object(self.mcp, "_DOCS", docs):
                result = self.mcp.refs_of([1])
        self.assertEqual(result["refs_status"], "unresolved")
        self.assertEqual(result["refs"], [])

    def test_legacy_refs_still_resolve_and_prefer_explicit_original(self):
        with tempfile.TemporaryDirectory() as root:
            sources = Path(root, "personal", "wiki", "sources")
            sources.mkdir(parents=True)
            (sources / "reference.md").write_text(
                '---\ntitle: Reference\nsummary: https://example.org/unrelated\n'
                'source: https://example.org/original\n---\nhttps://example.org/body')
            docs = {1: {"doc_id": 1, "path": "note.md", "date": "", "title": "Note"}}
            for fm in ('sources: ["s1:reference"]',
                       'sources:\n  - resource: /wiki/sources/reference.md',
                       'sources: [{resource: /personal/wiki/sources/reference.md}]'):
                Path(root, "note.md").write_text(f"---\n{fm}\n---\nbody")
                with self.subTest(fm=fm), patch.object(self.mcp, "VAULT", root), \
                     patch.object(self.mcp, "_VROOT", os.path.realpath(root)), \
                     patch.object(self.mcp, "SRC_DIR", str(sources)), patch.object(self.mcp, "_DOCS", docs):
                    result = self.mcp.refs_of([1])
                self.assertEqual(result["refs_status"], "ok")
                self.assertEqual([r["url"] for r in result["refs"]], ["https://example.org/original"])

    def test_cloud_export_scrubs_source_url_on_gated_rows(self):
        from export_cloud import _scrub_documents
        table = pa.table({"doc_id": [1, 2], "path": ["open.md", "secret.md"],
                          "source_url": ["https://example.org/open", "https://example.org/secret"],
                          "abs_path": ["/Users/local/open.md", "/Users/local/secret.md"],
                          "no_llm": [False, True]})
        result = _scrub_documents(table, pa.array([False, True])).to_pylist()
        self.assertEqual([r["source_url"] for r in result], ["https://example.org/open", ""])
        self.assertNotIn("/Users/", json.dumps(result))
        self.assertNotIn("https://example.org/secret", json.dumps(result))

    def test_refs_rechecks_live_no_llm_and_source_path_gate(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "source.md").write_text(
                '---\nno_llm: true\nsources: [{resource: "https://example.org/private"}]\n---\n')
            docs = {1: {"doc_id": 1, "path": "source.md", "date": "", "title": "Source",
                        "source_url": "https://example.org/private"}}
            with patch.object(self.mcp, "VAULT", root), patch.object(self.mcp, "_VROOT", os.path.realpath(root)), \
                 patch.object(self.mcp, "_DOCS", docs):
                result = self.mcp.refs_of([1])
            self.assertEqual(result["refs"], [])
            self.assertEqual(result["refs_status"], "unresolved")
            Path(root, "source.md").write_text('---\nsources:\n  - resource: secret.md\n---\n')
            Path(root, "secret.md").write_text('---\ntitle: Secret\n---\nhttps://example.org/private')
            docs[1].pop("source_url")
            with patch.object(self.mcp, "VAULT", root), patch.object(self.mcp, "_VROOT", os.path.realpath(root)), \
                 patch.object(self.mcp, "_DOCS", docs), \
                 patch.object(self.mcp, "_gate_by_path", side_effect=lambda p: p == "secret.md"):
                result = self.mcp.refs_of([1])
            self.assertEqual(result["refs"], [])
            self.assertEqual(result["refs_status"], "unresolved")

    def test_sync_old_schema_keeps_documents_and_explains_rebuild(self):
        with tempfile.TemporaryDirectory() as root:
            db = lancedb.connect(str(Path(root, "db")))
            schema = self.schema.schemas(2)["documents"]
            schema = pa.schema([f for f in schema if f.name != "source_url"])
            row = {f.name: (False if pa.types.is_boolean(f.type) else
                           1 if pa.types.is_integer(f.type) else "") for f in schema}
            row.update(doc_id=1, path="source.md", content_hash="old")
            db.create_table("documents", schema=schema, data=[row])
            docs = {1: {**row, "content_hash": "new", "source_url": "https://example.org/a", "_body": "body"}}
            db.create_table("chunks", schema=self.schema.schemas(2)["chunks"], data=[])
            fake_model = Mock()
            fake_model.get_embedding_dimension.return_value = 2
            output = io.StringIO()
            with patch.object(self.sync, "DB", str(Path(root, "db"))), \
                 patch.object(self.sync, "SentenceTransformer", return_value=fake_model), \
                 patch.object(self.sync, "scan_vault", return_value=(docs, [])), \
                 patch.object(self.sync, "reindex_chunks", return_value=(0, 0)), \
                 patch.object(self.sync, "rebuild_inverted", return_value=(0, 0)), \
                 patch.object(self.sync, "prune_kg", return_value=0), \
                 patch.object(self.sync, "mark_stale", return_value=0), contextlib.redirect_stdout(output):
                self.sync.sync()
            current = db.open_table("documents").search().to_list()
            self.assertEqual([(r["doc_id"], r["content_hash"]) for r in current], [(1, "new")])
            self.assertIn("documents.source_url", output.getvalue())
            self.assertIn("just index", output.getvalue())
            output = io.StringIO()
            with patch.object(self.sync, "DB", str(Path(root, "db"))), \
                 patch.object(self.sync, "SentenceTransformer", return_value=fake_model), \
                 patch.object(self.sync, "scan_vault", return_value=(docs, [])), \
                 contextlib.redirect_stdout(output):
                self.sync.sync()
            self.assertIn("no changes", output.getvalue())
            self.assertIn("documents.source_url", output.getvalue())
            self.assertIn("just index", output.getvalue())


if __name__ == "__main__":
    unittest.main()
