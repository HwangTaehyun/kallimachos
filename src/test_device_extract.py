import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SRC = Path(__file__).resolve().parent


class DeviceExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kal-device-extraction-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.bundle = self.root / "bundle"
        self.source = self.root / "distilled"
        for directory in (self.home, self.bundle, self.source, self.root / "bin"):
            directory.mkdir()
        stub = self.root / "bin/claude"
        stub.write_text("#!/bin/sh\nexit 97\n")
        stub.chmod(0o700)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("KAL_", "GIT_", "ANTHROPIC_", "CLAUDE_"))
                    and k not in ("PYTHONPATH", "PYTHONHOME")}
        self.env.update(HOME=str(self.home), KAL_HOME=str(self.home / "state"), KAL_VAULT=str(self.bundle),
                        KAL_DEVICE="test-device", PYTHONPATH=str(SRC), KAL_DISTILLED=str(self.source),
                        GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
                        HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                        PATH=str(self.root / "bin") + os.pathsep + os.environ.get("PATH", ""))
        if os.environ.get("KAL_TEST_MUTATION"):
            self.env["KAL_TEST_MUTATION"] = os.environ["KAL_TEST_MUTATION"]
        subprocess.run(["git", "init", "-b", "main", str(self.bundle)], env=self.env,
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.bundle), "remote", "add", "origin", "https://github.com/example/bundle.git"],
                       env=self.env, check=True, capture_output=True)
        (self.source / "decision.md").write_text(
            "---\ntitle: Offline decision\ntype: conversation\nsession_id: fixture-session\n"
            "session_agent: claude\n---\n"
            "The local device extracts only new chunks and publishes the verified results. " * 2
        )

    def run_code(self, code):
        preamble = '''
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
import device_extract as E
import lr_extract as L
import openwiki_emit
bundle = os.environ["KAL_VAULT"]
repository = E.repository_identity(bundle)
cache = E.scoped_cache_home(repository) / "lr_cache.jsonl"
sys.argv = ["openwiki_emit", "--wiki", bundle, "--from", os.environ["KAL_DISTILLED"], "--device", "test-device"]
assert openwiki_emit.main() == 0
response = json.dumps({"entities": [{"name": "Device", "type": "concept", "description": "A local device"}], "relationships": []})
mutation = os.environ.get("KAL_TEST_MUTATION")
if mutation == "storage-identity":
    E.result_identity = lambda row: E.hashlib.sha256(json.dumps([row.get(k) for k in ("repository", "doc", "idx", "h", "pv", "model")]).encode()).hexdigest()
elif mutation == "model-insertion-order":
    def storage_order(rows):
        by_storage = {(*E._identity(row), row["model"]): row for row in rows}
        return {E._identity(row): row for row in by_storage.values()}
    E.current_rows = storage_order
elif mutation == "collapsed-repository":
    E.repository_identity = lambda *args, **kwargs: "github.com/example/bundle"
'''
        result = subprocess.run([sys.executable, "-c", preamble + code], env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_new_emitted_chunk_is_extracted_once_then_reused(self):
        self.run_code('''
with patch.object(L, "claude_cli_run", return_value=response) as llm:
    first = E.process(bundle, "test-device")
    assert first["extracted"] == 1, first
    assert first["exported"] == 1, first
    llm.assert_called_once()
with patch.object(L, "claude_cli_run", side_effect=AssertionError("unchanged chunk was charged again")) as llm:
    second = E.process(bundle, "test-device")
    assert second["extracted"] == second["exported"] == 0, second
    llm.assert_not_called()
assert len(E.current_rows(E._rows(cache))) == 1
assert (Path(bundle) / ".kal-sync/extract/test-device.lr_cache.jsonl").is_file()
''')

    def test_only_changed_chunk_is_extracted_after_an_edit(self):
        self.run_code('''
page = Path(bundle) / "personal/sessions/test-device/decision.md"
page.write_text(("An isolated incremental extraction fixture. " * L.CHUNK_CHARS)[:L.CHUNK_CHARS + 100])
with patch.object(L, "claude_cli_run", return_value=response) as llm:
    first = E.process(bundle, "test-device")
    assert first["extracted"] == 2, first
    assert llm.call_count == 2
page.write_text(page.read_text()[:-1] + "Z")
with patch.object(L, "claude_cli_run", return_value=response) as llm:
    second = E.process(bundle, "test-device")
    assert second["extracted"] == second["exported"] == 1, second
    llm.assert_called_once()
''')

    def test_partial_failure_retains_successful_chunks_for_retry(self):
        self.run_code('''
page = Path(bundle) / "personal/sessions/test-device/decision.md"
page.write_text(("An isolated incremental extraction fixture. " * L.CHUNK_CHARS)[:L.CHUNK_CHARS + 100])
with patch.object(L, "claude_cli_run", side_effect=[response, "{}", "{}", "{}"]):
    try:
        E.process(bundle, "test-device")
    except E.ExtractionError:
        pass
    else:
        raise AssertionError("partial extraction failure was hidden")
assert len(cache.read_text().splitlines()) == 1
assert not (Path(bundle) / ".kal-sync/extract/test-device.lr_cache.jsonl").exists()
with patch.object(L, "claude_cli_run", return_value=response) as llm:
    result = E.process(bundle, "test-device")
    assert result["extracted"] == 1 and result["exported"] == 2, result
    llm.assert_called_once()
''')

    def test_pulled_agent_cache_prevents_duplicate_charge_and_remains_usable(self):
        self.run_code('''
chunks = E.collect_bundle(bundle)
assert len(chunks) == 1
row = {k: chunks[0][k] for k in ("doc", "idx", "h")}
row.update(pv=L.PROMPT_VERSION, model="agent:sonnet", at="2026-01-01", entities=[], relationships=[], repository=repository)
shared = Path(bundle) / ".kal-sync/extract/other-device.lr_cache.jsonl"
shared.parent.mkdir(parents=True)
shared.write_text(json.dumps(row) + "\\n")
with patch.object(L, "claude_cli_run", side_effect=AssertionError("shared work must be reused")) as llm:
    result = E.process(bundle, "test-device")
    assert result["extracted"] == result["exported"] == 0, result
    llm.assert_not_called()
assert not (shared.parent / "test-device.lr_cache.jsonl").exists()
import subprocess
subprocess.run([sys.executable, "-c", "import lr_extract as L; assert L.pending_extraction()['pending_count'] == 0"],
               env=dict(os.environ, KAL_HOME=str(cache.parent)), check=True)
assert json.loads(cache.read_text())["model"] == "agent:sonnet"
assert not Path(L.CACHE).exists()
''')

    def test_retry_reuses_fetched_peer_cache_without_overwriting_pending_pages(self):
        self.run_code('''
import subprocess
def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
                          check=True, capture_output=True, text=True).stdout
chunk = E.collect_bundle(bundle)[0]
row = {k: chunk[k] for k in ("doc", "idx", "h")}
row.update(pv=L.PROMPT_VERSION, model="agent:sonnet", entities=[], relationships=[], repository=repository)
git(bundle, "add", ".")
git(bundle, "commit", "-m", "emitted fixture")
peer = Path(os.environ["HOME"]) / "peer"
subprocess.run(["git", "clone", bundle, str(peer)], check=True, capture_output=True)
shared = peer / ".kal-sync/extract/peer.lr_cache.jsonl"
shared.parent.mkdir(parents=True)
shared.write_text(json.dumps(row) + "\\n")
git(peer, "add", ".")
git(peer, "commit", "-m", "peer extraction")
git(bundle, "fetch", str(peer), "main")
assert not (Path(bundle) / ".kal-sync/extract/peer.lr_cache.jsonl").exists()
pending = Path(bundle) / "personal/sessions/test-device/pending.md"
pending.write_text("Pending short note, not an eligible chunk.\\n")
with patch.object(L, "claude_cli_run", side_effect=AssertionError("peer completed the work before this retry")) as llm:
    result = E.process(bundle, "test-device", shared_ref="FETCH_HEAD")
    assert result["extracted"] == result["exported"] == 0, result
    llm.assert_not_called()
assert pending.read_text() == "Pending short note, not an eligible chunk.\\n"
assert not (Path(bundle) / ".kal-sync/extract/peer.lr_cache.jsonl").exists()
''')

    def test_export_omits_unrelated_local_cache_and_old_nonmatching_content(self):
        self.run_code('''
chunk = E.collect_bundle(bundle)[0]
row = {k: chunk[k] for k in ("doc", "idx", "h")}
row.update(pv=L.PROMPT_VERSION, model="agent:opus", entities=[], relationships=[])
private = dict(row, doc="private_other_vault", entities=[{"name":"PRIVATE-CANARY"}])
stale = dict(row, h="000000000000", entities=[{"name":"OLD-PRIVATE-CANARY"}])
cache.write_text("\\n".join(json.dumps(r) for r in (private, stale, row)) + "\\n")
with patch.object(L, "claude_cli_run", side_effect=AssertionError("already cached")):
    result = E.process(bundle, "test-device")
assert result["exported"] == 1, result
text = (Path(bundle) / ".kal-sync/extract/test-device.lr_cache.jsonl").read_text()
assert "PRIVATE-CANARY" not in text
assert json.loads(text)["doc"] == chunk["doc"]
''')

    def test_latest_same_key_corrections_publish_initial_and_incremental_revisions(self):
        self.run_code('''
chunk = E.collect_bundle(bundle)[0]
def row(name, model="agent:sonnet"):
    return {**{k: chunk[k] for k in ("doc", "idx", "h")}, "pv": L.PROMPT_VERSION, "model": model,
            "entities": [{"name": name, "type": "concept", "description": name}], "relationships": []}
E._append(cache, [row("Old"), row("Corrected")])
with patch.object(L, "claude_cli_run", side_effect=AssertionError("cached correction charged again")):
    first = E.process(bundle, "test-device")
    exported = Path(bundle) / ".kal-sync/extract/test-device.lr_cache.jsonl"
    assert first["exported"] == 1, first
    assert [r["entities"][0]["name"] for r in E._rows(exported)] == ["Corrected"]
    original = exported.read_bytes()
    corrected = dict(E._rows(cache)[-1], entities=row("FurtherCorrected")["entities"])
    E._append(cache, [corrected])
    second = E.process(bundle, "test-device")
    assert second["exported"] == 1, second
    assert exported.read_bytes().startswith(original)
    assert [r["entities"][0]["name"] for r in E._rows(exported)] == ["Corrected", "FurtherCorrected"]
    assert E.process(bundle, "test-device")["exported"] == 0
''')

    def test_cross_model_a_b_a_uses_physical_order_and_republishes_a(self):
        self.run_code('''
chunk = E.collect_bundle(bundle)[0]
base = {**{k: chunk[k] for k in ("doc", "idx", "h")}, "pv": L.PROMPT_VERSION, "entities": [], "relationships": []}
a, b = dict(base, model="agent:a"), dict(base, model="agent:b")
E._append(cache, [a, b, a])
with patch.object(L, "claude_cli_run", side_effect=AssertionError("already extracted")):
    assert E.process(bundle, "test-device")["exported"] == 1
    exported = Path(bundle) / ".kal-sync/extract/test-device.lr_cache.jsonl"
    assert [r["model"] for r in E._rows(exported)] == ["agent:a"]
    for next_row in (b, a):
        E._append(cache, [next_row])
        assert E.process(bundle, "test-device")["exported"] == 1
    assert [r["model"] for r in E._rows(exported)] == ["agent:a", "agent:b", "agent:a"]
    merged = E.current_rows(E.read_shared_rows(bundle, repository))
    assert next(iter(merged.values()))["model"] == "agent:a"
''')

    def test_shared_corrections_import_over_same_storage_key_without_reexport(self):
        self.run_code('''
chunk = E.collect_bundle(bundle)[0]
base = {**{k: chunk[k] for k in ("doc", "idx", "h")}, "pv": L.PROMPT_VERSION,
        "model": "agent:sonnet", "repository": repository, "relationships": []}
def row(name):
    return dict(base, entities=[{"name": name, "type": "concept", "description": name}])
E._append(cache, [row("LocalOld")])
shared = Path(bundle) / ".kal-sync/extract/peer.lr_cache.jsonl"
E._append(shared, [row("SharedCorrection")])
with patch.object(L, "claude_cli_run", side_effect=AssertionError("shared correction was not reused")):
    result = E.process(bundle, "test-device")
    assert result["exported"] == result["extracted"] == 0, result
    assert E._rows(cache)[-1]["entities"][0]["name"] == "SharedCorrection"
    E._append(shared, [row("LaterCorrection")])
    result = E.process(bundle, "test-device")
    assert result["imported"] == 1 and result["exported"] == 0, result
    assert E._rows(cache)[-1]["entities"][0]["name"] == "LaterCorrection"
    E._append(cache, [row("LocalNewest")])
    result = E.process(bundle, "test-device")
    assert result["exported"] == 1, result
    assert E._rows(cache)[-1]["entities"][0]["name"] == "LocalNewest"
    assert E.process(bundle, "test-device")["exported"] == 0
''')

    def test_repository_a_shared_canary_never_reused_or_exported_by_b(self):
        self.run_code('''
import shutil
import subprocess
chunk = E.collect_bundle(bundle)[0]
canary = {**{k: chunk[k] for k in ("doc", "idx", "h")}, "pv": L.PROMPT_VERSION,
          "model": "agent:sonnet", "repository": repository,
          "entities": [{"name": "REPO-A-CANARY", "type": "concept", "description": "Private to A"}], "relationships": []}
shared = Path(bundle) / ".kal-sync/extract/peer.lr_cache.jsonl"
E._append(shared, [canary])
with patch.object(L, "claude_cli_run", side_effect=AssertionError("A shared cache should be reused")):
    assert E.process(bundle, "test-device")["extracted"] == 0
assert "REPO-A-CANARY" in cache.read_text()
assert not Path(L.CACHE).exists()
bundle_b = Path(bundle).parent / "bundle-b"
subprocess.run(["git", "init", str(bundle_b)], check=True, capture_output=True)
subprocess.run(["git", "-C", str(bundle_b), "remote", "add", "origin", "git@github.com:example/bundle-b.git"], check=True)
shutil.copytree(Path(bundle) / "personal", bundle_b / "personal")
assert E.collect_bundle(str(bundle_b))[0]["h"] == chunk["h"]
with patch.object(L, "claude_cli_run", return_value=response) as llm:
    result = E.process(str(bundle_b), "test-device", repository="github.com/example/bundle-b")
    llm.assert_called_once()
    assert result["extracted"] == result["exported"] == 1, result
assert Path(result["cache_home"]) != cache.parent
assert "REPO-A-CANARY" not in (Path(result["cache_home"]) / "lr_cache.jsonl").read_text()
assert "REPO-A-CANARY" not in (bundle_b / ".kal-sync/extract/test-device.lr_cache.jsonl").read_text()
with patch.object(L, "claude_cli_run", side_effect=AssertionError("returning to A must reuse A")):
    assert E.process(bundle, "test-device")["extracted"] == 0
''')

    def test_same_repository_new_checkout_reuses_and_publishes_scoped_local_work(self):
        self.run_code('''
import shutil
import subprocess
with patch.object(L, "claude_cli_run", return_value=response) as llm:
    assert E.process(bundle, "test-device")["extracted"] == 1
    llm.assert_called_once()
other = Path(bundle).parent / "other-checkout"
subprocess.run(["git", "init", str(other)], check=True, capture_output=True)
subprocess.run(["git", "-C", str(other), "remote", "add", "origin", "git@github.com:EXAMPLE/BUNDLE.git"], check=True)
shutil.copytree(Path(bundle) / "personal", other / "personal")
with patch.object(L, "claude_cli_run", side_effect=AssertionError("same repository local work must be reused")):
    result = E.process(str(other), "test-device")
    assert result["extracted"] == 0 and result["exported"] == 1, result
    original = cache.read_bytes()
    for target in (bundle, str(other), bundle):
        assert E.process(target, "test-device")["exported"] == 0
    assert cache.read_bytes() == original
assert (other / ".kal-sync/extract/test-device.lr_cache.jsonl").is_file()
''')

    def test_canonical_identity_is_idempotent_and_does_not_collapse_git_suffix_names(self):
        self.run_code('''
import github_sync as G
for url in ("https://github.com/EXAMPLE/Bundle.git", "git@github.com:example/bundle.git"):
    assert G.canonical_repository(url) == repository
canonical = G.canonical_repository("https://github.com/example/bundle.git.git")
assert canonical == "github.com/example/bundle.git"
assert G.canonical_repository(canonical) == canonical
assert canonical != repository
''')

    def test_legacy_global_cache_is_ignored_and_explicit_unbound_cache_refused(self):
        self.run_code('''
chunk = E.collect_bundle(bundle)[0]
legacy = Path(L.CACHE)
row = {**{k: chunk[k] for k in ("doc", "idx", "h")}, "pv": L.PROMPT_VERSION, "model": "agent:sonnet",
       "entities": [{"name": "UNSCOPED-CANARY", "type": "concept", "description": "Unknown origin"}], "relationships": []}
E._append(legacy, [row])
before = legacy.read_bytes()
with patch.object(L, "claude_cli_run", return_value=response) as llm:
    result = E.process(bundle, "test-device")
    llm.assert_called_once()
assert result["ignored_unscoped_cache"] == 1, result
assert legacy.read_bytes() == before
assert "UNSCOPED-CANARY" not in (Path(bundle) / ".kal-sync/extract/test-device.lr_cache.jsonl").read_text()
with patch.object(L, "claude_cli_run", side_effect=AssertionError("must refuse before paid work")):
    try:
        E.process(bundle, "test-device", local_cache_path=str(legacy))
    except E.ExtractionError as error:
        assert "unscoped" in str(error), error
    else:
        raise AssertionError("unscoped explicit cache was blessed")
''')

    def test_foreign_or_unscoped_shared_rows_are_not_relabelled_for_publication(self):
        self.run_code('''
chunk = E.collect_bundle(bundle)[0]
base = {**{k: chunk[k] for k in ("doc", "idx", "h")}, "pv": L.PROMPT_VERSION, "model": "agent:sonnet",
        "entities": [{"name": "FOREIGN-CANARY", "type": "concept", "description": "Not this repository"}], "relationships": []}
shared = Path(bundle) / ".kal-sync/extract/peer.lr_cache.jsonl"
E._append(shared, [base, dict(base, repository="github.com/example/foreign")])
original = shared.read_bytes()
with patch.object(L, "claude_cli_run", return_value=response) as llm:
    result = E.process(bundle, "test-device")
    llm.assert_called_once()
assert shared.read_bytes() == original
assert "FOREIGN-CANARY" not in cache.read_text()
assert "FOREIGN-CANARY" not in (shared.parent / "test-device.lr_cache.jsonl").read_text()
''')

    def test_scoped_agent_write_lock_blocks_cli_cache_writer(self):
        self.run_code('''
import subprocess
agent = subprocess.Popen([sys.executable, "-c", "from kal_lock import db_lock; cm=db_lock('scoped-agent'); cm.__enter__(); print('ready', flush=True); input(); cm.__exit__(None,None,None)"],
                         env=dict(os.environ, KAL_HOME=str(cache.parent)), stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
try:
    assert agent.stdout.readline().strip() == "ready"
    with patch.object(L, "claude_cli_run", side_effect=AssertionError("agent writer already owns the scoped cache")):
        try:
            E.process(bundle, "test-device")
        except E.ExtractionError as error:
            assert "busy" in str(error), error
        else:
            raise AssertionError("scoped agent lock was bypassed")
finally:
    agent.communicate("\\n", timeout=10)
    assert agent.returncode == 0
''')

    def test_selected_repository_and_bound_home_identity_must_match(self):
        self.run_code('''
with patch.object(L, "claude_cli_run", side_effect=AssertionError("identity mismatch must precede paid work")):
    for kwargs in ({"repository": "github.com/example/other"}, {"local_cache_path": str(cache)}):
        if "local_cache_path" in kwargs:
            binding = cache.parent / ".repository.json"
            binding.write_text(json.dumps({"version": 1, "repository": "github.com/example/other"}))
        try:
            E.process(bundle, "test-device", **kwargs)
        except E.ExtractionError as error:
            assert "repository" in str(error), error
        else:
            raise AssertionError("foreign identity accepted")
''')

    def test_llm_failure_is_not_cached_or_exported(self):
        self.run_code('''
with patch.object(L, "claude_cli_run", return_value="{}"):
    try:
        E.process(bundle, "test-device")
    except E.ExtractionError as error:
        assert "failed" in str(error)
    else:
        raise AssertionError("failed extraction was reported as success")
assert not cache.exists() or not cache.read_text()
assert not (Path(bundle) / ".kal-sync/extract/test-device.lr_cache.jsonl").exists()
''')

    def test_missing_device_cli_reports_unavailable_instead_of_success(self):
        self.run_code('''
with patch.object(E.shutil, "which", return_value=None), patch.object(L, "claude_cli_run", side_effect=AssertionError("missing backend must fail before a call")) as llm:
    try:
        E.process(bundle, "test-device")
    except E.ExtractionError as error:
        assert "unavailable" in str(error)
    else:
        raise AssertionError("missing backend was reported as success")
    llm.assert_not_called()
''')

    def test_active_agent_job_is_not_duplicated_by_the_cli(self):
        self.run_code('''
chunk = E.collect_bundle(bundle)[0]
jobs = Path(L.KAL_HOME) / "extract_jobs"
jobs.mkdir()
(jobs / "fixture.json").write_text(json.dumps({"finished_at": None,
    "all_keys": [f"{chunk['doc']}:{chunk['idx']}:{chunk['h']}"]}))
with patch.object(L, "claude_cli_run", side_effect=AssertionError("agent already owns the work")) as llm:
    try:
        E.process(bundle, "test-device")
    except E.ExtractionError as error:
        assert "agent extraction job" in str(error)
    else:
        raise AssertionError("active agent work was duplicated")
    llm.assert_not_called()
''')

    def test_no_llm_page_is_not_extracted_or_exported(self):
        self.run_code('''
page = Path(bundle) / "personal/sessions/test-device/decision.md"
page.write_text(page.read_text().replace("---\\n", "---\\nno_llm: true\\n", 1))
with patch.object(L, "claude_cli_run", side_effect=AssertionError("no_llm page must not leave the device")) as llm:
    result = E.process(bundle, "test-device")
    assert result["extracted"] == result["exported"] == 0, result
    llm.assert_not_called()
''')

    def test_symlink_outside_bundle_is_refused_before_collection(self):
        self.run_code('''
private = Path(os.environ["HOME"]) / "private.md"
private.write_text("PRIVATE-CANARY " * 40)
(Path(bundle) / "leak.md").symlink_to(private)
with patch.object(L, "claude_cli_run", side_effect=AssertionError("symlink target must never reach an LLM")) as llm:
    try:
        E.process(bundle, "test-device")
    except E.ExtractionError:
        pass
    else:
        raise AssertionError("bundle symlink was accepted")
    llm.assert_not_called()
''')


if __name__ == "__main__":
    unittest.main()


class ResultMaskingTests(unittest.TestCase):
    def test_credential_shaped_model_output_is_masked_not_fatal(self):
        import tempfile
        from pathlib import Path
        import device_extract as D
        row = {"doc": "d", "idx": 0, "h": "h", "pv": "p", "model": "m",
               "entities": [{"name": "db", "type": "tool",
                             "description": "connects with postgresql://user:hunter22@db:5432/app"}],
               "relationships": []}
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / "x.jsonl"
            D._append(out, [row])
            text = out.read_text()
        self.assertIn("REDACTED", text)
        self.assertNotIn("hunter22", text)
