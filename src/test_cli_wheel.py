import argparse
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile


def smoke(wheel, sdist):
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        modules = {
            name for name in names
            if "/" not in name and name.endswith(".py") and not name.startswith("test_")
        }
        required = {
            "kal_cli.py", "device_auth.py", "github_sync.py", "git_askpass.py", "ledger.py",
            "kal_lock.py", "ingest_sessions.py", "ingest_codex_sessions.py",
            "ingest_hermes_sessions.py", "distill_sessions.py", "openwiki_emit.py",
            "schema_v3.py", "kal_config.py", "claude_cli.py", "okf_convert.py", "source_links.py", "device_extract.py",
        }
        assert required <= modules, required - modules
        assert all(name in modules or ".dist-info/" in name for name in names), names
        entrypoint = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        assert "kal = kal_cli:main" in archive.read(entrypoint).decode()
    with tarfile.open(sdist) as archive:
        names = ["/".join(Path(member.name).parts[1:]) for member in archive.getmembers() if member.isfile()]
        assert all(
            name in {"pyproject.toml", "uv.lock", "LICENSE", "PKG-INFO", ".gitignore"}
            or (name.startswith("src/") and name.endswith(".py") and not Path(name).name.startswith("test_"))
            for name in names
        ), names

    with tempfile.TemporaryDirectory(prefix="kal-cli-wheel-") as directory:
        root = Path(directory)
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("KAL_", "GIT_", "ANTHROPIC_", "CLAUDE_"))
            and key not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")
        }
        env.update(HOME=directory, KAL_HOME=str(root / "state"), XDG_CONFIG_HOME=str(root / "config"),
                   GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        venv = root / "venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, env=env)
        python = venv / "bin" / "python"
        cli = venv / "bin" / "kal"
        subprocess.run(["uv", "pip", "install", "--python", str(python), "--no-deps", "--no-index", str(wheel)],
                       cwd=root, env=env, check=True, capture_output=True, text=True)
        for args in (["--help"], ["sync", "--help"], ["autosync", "--help"], ["whoami"],
                     ["session-end", "--repository", "22", "--print-hook"]):
            result = subprocess.run([str(cli), *args], cwd=root, env=env, capture_output=True, text=True)
            assert result.returncode == 0, (args, result.stderr)
        subprocess.run([str(python), "-I", "-m", "device_extract", "--help"], cwd=root, env=env,
                       check=True, capture_output=True, text=True)
        result = subprocess.run([str(cli), "repositories"], cwd=root, env=env, capture_output=True, text=True)
        assert result.returncode == 1 and "not logged in" in result.stderr, result
        result = subprocess.run([str(cli), "sync"], cwd=root, env=env, capture_output=True, text=True)
        assert result.returncode == 1 and "not logged in" in result.stderr, result
        daemon_home = root / "daemon-state"
        daemon = subprocess.Popen([str(cli), "autosync"], cwd=root,
                                  env=dict(env, KAL_HOME=str(daemon_home)),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not (daemon_home / ".sync.lock").exists() and time.monotonic() < deadline:
                assert daemon.poll() is None, "autosync exited before its first cycle"
                time.sleep(0.02)
            assert (daemon_home / ".sync.lock").exists(), "autosync did not start a cycle"
            daemon.terminate()
            stdout, stderr = daemon.communicate(timeout=5)
            assert daemon.returncode == 0, (stdout, stderr)
            assert "not logged in" in stdout, stdout
        finally:
            if daemon.poll() is None:
                daemon.kill()
                daemon.communicate()
        code = '''
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock, patch
import github_sync
import kal_cli
import device_auth
assert "site-packages" in kal_cli.__file__, kal_cli.__file__
assert str(Path.home()) == os.environ["HOME"]
with patch.object(subprocess, "run", return_value=Mock(returncode=0, stdout='{"extracted":0,"reused":0,"exported":0}')) as run:
    github_sync.run_distill(os.environ["KAL_HOME"], device="test-device")
    assert github_sync.run_device_extract(os.environ["KAL_HOME"], "test-device")["extracted"] == 0
for call in run.call_args_list:
    assert Path(call.args[0][1]).is_file(), call
real_run = subprocess.run
def verify(command, **kwargs):
    result = real_run(
        ["git", *github_sync.HARDENED_GIT_CONFIG, "credential", "fill"],
        input="protocol=https\\nhost=github.com\\n\\n", env=kwargs["env"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "password=fake-smoke-token" in result.stdout
    assert "username=x-access-token" in result.stdout
    return result
with patch.object(subprocess, "run", side_effect=verify):
    github_sync._git(os.environ["HOME"], ["status"], token="fake-smoke-token")
assert not Path(device_auth.CRED_PATH).exists()
'''
        subprocess.run([str(python), "-I", "-c", code], cwd=root, env=env, check=True)
    print("wheel/sdist allowlists, isolated no-dependency install, CLI commands, packaged pipeline paths and real Git askpass: passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    parser.add_argument("sdist", type=Path)
    args = parser.parse_args()
    smoke(args.wheel.resolve(), args.sdist.resolve())
