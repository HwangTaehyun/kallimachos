#!/usr/bin/env python3
"""Does the plugin **really start** —— the container's MCP is knocked on over real stdio.

Why it is needed
  `just selftest` runs `kal_mcp.py --selftest` in the host venv.  That checks whether the
  Python functions behave, and never once whether **the plugin is installed and starts**.
  What has to be verified sits between the two: is the image built, does the stdio handshake
  work, does a tool list come back, is stdout clean, does it state a version.
  (deep review 2026-08-23 —— the completeness lens caught it as "there are no acceptance criteria")

The probe's trap —— **stdin has to stay open**
  Pipe the requests from a file and EOF follows the last line immediately, so the server
  answers `initialize` and exits.  Measured (2026-08-23), an unanswered `tools/list` was
  nearly misdiagnosed as "the container's MCP cannot serve tools".  So this writes into the
  pipe directly and **does not close it** until the response arrives.

Usage:
    python src/plugin_probe.py                      # kal:local against the real DB
    python src/plugin_probe.py --image kal:local --kal ~/.kal --vault ~/notes
    python src/plugin_probe.py --selftest           # the logic alone, no docker
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from frontmatter import FM_RE

#  The 5 MCP tools.  Missing one breaks the selection rules of the skill (`skills/kal-recall`).
EXPECT_TOOLS = {"kal_search", "kal_entity", "kal_timeline", "kal_neighbors", "kal_doc"}
PROTOCOL = "2025-06-18"


def _requests(call_tool=None):
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                    "clientInfo": {"name": "plugin_probe", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    if call_tool:
        reqs.append({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": call_tool, "arguments": {"query": "probe"}}})
    return reqs


def docker_args(image, kal_dir, vault_dir, hardened=True):
    """Build **the same arguments** `.mcp.json` uses.

    Why they must match —— if the probe succeeds with looser flags, the real install can break
    on the hardened ones while this test stays green.  That is not a test but a comfort.
    """
    a = ["docker", "run", "--rm", "-i"]
    if hardened:
        a += ["--network", "none", "--read-only",
              "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
              "--tmpfs", "/tmp"]
    a += ["-v", f"{kal_dir}/db:/data/db:ro",
          "-v", f"{vault_dir}:/vault:ro",
          image]
    return a


def probe(image, kal_dir, vault_dir, hardened=True, timeout=120, call_tool=None):
    """(elapsed, responses, stderr, dirty_stdout_lines)."""
    args = docker_args(image, kal_dir, vault_dir, hardened)
    t0 = time.time()
    p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, bufsize=1)
    seen, dirty = {}, []
    try:
        for r in _requests(call_tool):
            p.stdin.write(json.dumps(r) + "\n")   # type: ignore[union-attr]
            p.stdin.flush()                       # type: ignore[union-attr]
        #  ⚠ stdin is not closed here (see the file header).
        while time.time() - t0 < timeout:
            line = p.stdout.readline()             # type: ignore[union-attr]
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                dirty.append(line[:200])       # stdout pollution —— the protocol breaks
                continue
            if "id" in m:
                seen[m["id"]] = m
            want = {1, 2} | ({3} if call_tool else set())
            if want <= set(seen):
                break
    finally:
        #  ⚠ `communicate()` tries to flush a closed stdin again and dies
        #    (`ValueError: I/O operation on closed file`).  stderr is read **directly**.
        for h in (p.stdin, p.stdout):
            try:
                h.close()          # type: ignore[union-attr]
            except Exception:
                pass
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=5)
        try:
            err = p.stderr.read()   # type: ignore[union-attr]
        except Exception:
            err = ""
        try:
            p.stderr.close()        # type: ignore[union-attr]
        except Exception:
            pass
    return time.time() - t0, seen, err or "", dirty


def manifest_drift(repo=None):
    """Does the image tag `.mcp.json` pins match `plugin.json`'s version.

    Why it is checked here —— `kal_mcp.py`'s self-check, run **inside the container**, reads
    `/app/.claude-plugin/plugin.json` (the copy baked in at build time).  All three asserts
    then look at the same file, making it **a tautology** that cannot catch "the host
    manifest says 0.2.0 while the pinned image is :0.1.0".  This check reads **the host repository**.
    (deep review 2026-08-23, consistency lens)
    """
    repo = repo or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bad = []
    try:
        with open(os.path.join(repo, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            ver = json.load(f).get("version")
        with open(os.path.join(repo, ".mcp.json"), encoding="utf-8") as f:
            args = json.load(f)["mcpServers"]["kal"]["args"]
    except Exception as e:
        return [f"the manifest could not be read: {type(e).__name__}: {e}"]

    refs = [a for a in args if "/kal@" in a or "/kal:" in a]
    if not refs:
        return ["`.mcp.json` holds no reference to the kal image"]
    ref = refs[-1]
    if "@sha256:" in ref:
        return bad                      # a digest pin —— comparing tags is meaningless and weaker
    tag = ref.rsplit(":", 1)[-1]
    if tag != ver:
        bad.append(f"the tag `.mcp.json` pins ({tag}) differs from plugin.json's version ({ver})")

    #  Do `plugin/package.json` and `package-lock.json` agree on **name and version**.
    #  `npm ci` refuses when they differ —— it dies at CI's first step.  It really happened
    #  when the plugin id changed and only package.json was updated
    #  (2026-08-25).  The Python side has `uv lock --check` (justfile:222) doing the same job;
    #  the node side had nothing.
    try:
        pj = json.load(open(os.path.join(repo, "plugin", "package.json"), encoding="utf-8"))
        pl = json.load(open(os.path.join(repo, "plugin", "package-lock.json"), encoding="utf-8"))
    except Exception as e:
        bad.append(f"the plugin's package(.lock).json could not be read: {type(e).__name__}: {e}")
        return bad
    for key, got, want in (("name", pl.get("name"), pj.get("name")),
                           ("name(packages.'')", pl.get("packages", {}).get("", {}).get("name"), pj.get("name")),
                           ("version", pl.get("version"), pj.get("version"))):
        if got != want:
            bad.append(f"package-lock.json has {key}={got!r} while package.json says {want!r} "
                       f"—— `npm ci` refuses this")
    return bad


def userconfig_safety(repo=None):
    """Can a `${user_config.*}` reference **silently become an empty string**.

    The measured case matrix (2026-08-23, Claude Code 2.1.241, reproduced with an isolated config):

        declared + default          → substituted (the default)
        declared + a user value     → substituted (the user's value)
        required:true · unset       → **the server does not start at all**       ← safe
        required:false · no default · unset
                                    → **an empty string**   `/db:/data/db:ro`     ← a silent malfunction
        not declared (a typo)       → the server does not start
        no userConfig block at all
                                    → the literal `${user_config.x}` passes through  ← the worst

    The dangerous row is the fourth.  Dropping `required` without giving a `default` makes the
    mount source an empty string and docker creates some unrelated path —— with no error.  So
    every referenced key is required here to **be required or have a default**.
    """
    repo = repo or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bad = []
    try:
        with open(os.path.join(repo, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            uc = json.load(f).get("userConfig") or {}
        with open(os.path.join(repo, ".mcp.json"), encoding="utf-8") as f:
            mcp = json.load(f)
    except Exception as e:
        return [f"the manifest could not be read: {type(e).__name__}: {e}"]

    refs = set(re.findall(r"\$\{user_config\.([A-Za-z0-9_]+)\}", json.dumps(mcp)))
    if refs and not uc:
        return ["`.mcp.json` uses ${user_config.*} while plugin.json has no userConfig "
                "—— the literal is handed straight to docker"]
    for k in sorted(refs):
        spec = uc.get(k)
        if spec is None:
            bad.append(f"`${{user_config.{k}}}` is not declared in plugin.json —— the server will not start")
        elif not spec.get("required") and "default" not in spec:
            bad.append(f"`{k}` is neither required nor has a default —— unset, it becomes "
                       f"**an empty string** and diverges silently")
    return bad


def skills_frontmatter(repo=None):
    """Does every `skills/*/SKILL.md` have the frontmatter `name` and `description`.

    Why it is checked directly —— `claude plugin validate skills/` **does not catch this**
    (measured 2026-08-23: deleting the `name:` line still gave "Validation passed").  Yet with
    no `name` the invocation name falls back to **the install directory name**, and for a
    marketplace install that is a version string, so **the skill's name changes on every update.**

    Codex requires the same two fields (`~/.agents/skills/<n>/SKILL.md`) —— so this one guard
    protects both hosts.
    """
    repo = repo or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sk = os.path.join(repo, "skills")
    if not os.path.isdir(sk):
        return ["there is no skills/ directory"]
    bad = []
    found = 0
    for name in sorted(os.listdir(sk)):
        f = os.path.join(sk, name, "SKILL.md")
        if not os.path.isfile(f):
            continue
        found += 1
        head = open(f, encoding="utf-8").read(4000)
        m = FM_RE.match(head)
        if not m:
            bad.append(f"{name}: no frontmatter block")
            continue
        fm = m.group(1)
        for key in ("name", "description"):
            if not re.search(rf"^{key}:", fm, re.M):
                bad.append(f"{name}: frontmatter has no `{key}` —— "
                           f"{'the invocation name falls back to the install directory name (a version string)'
                              if key == 'name' else 'the host cannot judge when to use it'}")
        dn = re.search(r"^name:\s*(\S+)", fm, re.M)
        if dn and dn.group(1) != name:
            bad.append(f"{name}: the frontmatter name ({dn.group(1)}) differs from the directory name")
    if not found:
        bad.append("skills/ holds no SKILL.md at all")
    return bad


#  The skills quote messages that live in other files —— failure sentences with a remedy
#  attached (`kal-maintain`), and sample responses an agent is told to read (`kal-recall`).
#  A quoted string is a contract across two files, and both of these broke: until 2026-09-01
#  they showed the **Korean** text those files used to emit, the code had gone English, and
#  an agent was matching on and expecting text nothing sends.  Nothing fails when that
#  happens —— the guidance simply stops arriving, which is the worst shape a defect can take.
#      message fragment  ->  (the file that must still emit it, the skill that quotes it)
QUOTED_MESSAGES = {
    "has no index yet":                           ("src/kal_mcp.py",  "skills/kal-maintain/SKILL.md"),
    "the DB is already in use — ":                ("src/kal_lock.py", "skills/kal-maintain/SKILL.md"),
    "Editing the same DB concurrently corrupts it silently.": ("src/kal_lock.py", "docs/PIPELINE.md"),
    "something else is writing the DB — ":        ("api/main.go",     "skills/kal-maintain/SKILL.md"),
    "needs the claude CLI and it is unavailable": ("api/main.go",     "skills/kal-maintain/SKILL.md"),
    "pass one of the candidates back **verbatim**.": ("src/kal_mcp.py", "skills/kal-recall/SKILL.md"),
    "changes have been recorded since this point": ("src/kal_mcp.py", "skills/kal-recall/SKILL.md"),
    "so 0 means either":                          ("src/kal_mcp.py",  "skills/kal-recall/SKILL.md"),
}


def skills_quoted_messages(repo=None):
    """Do the messages the skills quote still exist in the files that emit them.

    Checked in **both** directions —— reword the source and it fires, drop the line from the
    skill and it fires.  A one-sided check would let the skill rot silently, which is the
    failure this exists for.
    """
    repo = repo or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bad = []
    cache = {}

    def _read(rel):
        if rel not in cache:
            p = os.path.join(repo, rel)
            cache[rel] = open(p, encoding="utf-8").read() if os.path.isfile(p) else None
        return cache[rel]

    for msg, (src, doc) in QUOTED_MESSAGES.items():
        stext, dtext = _read(src), _read(doc)
        if dtext is None:
            bad.append(f"{doc} is missing —— it is what quotes {msg!r}")
            continue
        if stext is None:
            bad.append(f"{src} is missing —— {doc} quotes a message from it")
            continue
        if msg not in stext:
            bad.append(f"{src} no longer emits {msg!r} —— {doc} still tells an agent to expect it")
        if msg not in dtext:
            bad.append(f"{doc} no longer shows {msg!r} —— {src} still emits it, so an agent "
                       f"that meets it gets no guidance")
    return bad


def check(seen, dirty):
    """Returns a list of findings.  Empty means it passes."""
    bad = []
    if 1 not in seen:
        bad.append("no initialize response —— the server failed to start")
        return bad                              # nothing else is worth looking at
    info = seen[1].get("result", {}).get("serverInfo", {}) or {}
    if not info.get("version"):
        bad.append(f"serverInfo.version is empty ({info!r}) —— the host cannot read a version")
    if 2 not in seen:
        bad.append("no tools/list response")
    else:
        got = {t["name"] for t in seen[2].get("result", {}).get("tools", [])}
        if got != EXPECT_TOOLS:
            bad.append(f"the tool list differs —— missing {sorted(EXPECT_TOOLS - got)} · "
                       f"extra {sorted(got - EXPECT_TOOLS)}")
    if dirty:
        bad.append(f"{len(dirty)} non-JSON-RPC line(s) on stdout —— the protocol breaks: {dirty[0]!r}")
    return bad


def _selftest():
    """The judgement logic alone, no docker.  A test that cannot pass is not a test."""
    ok = {1: {"result": {"serverInfo": {"name": "kal", "version": "0.1.0"}}},
          2: {"result": {"tools": [{"name": n} for n in EXPECT_TOOLS]}}}
    assert check(ok, []) == [], f"a healthy case was flagged: {check(ok, [])}"

    #  ① an empty version must be caught —— it really was empty once
    bad = dict(ok); bad[1] = {"result": {"serverInfo": {"name": "kal", "version": ""}}}
    assert any("version" in b for b in check(bad, [])), "an empty version is not caught"

    #  ② a missing tool must be caught
    bad = dict(ok); bad[2] = {"result": {"tools": [{"name": "kal_search"}]}}
    assert any("tool list" in b for b in check(bad, [])), "a missing tool is not caught"

    #  ③ stdout pollution must be caught
    assert any("stdout" in b for b in check(ok, ["Loading weights: 0%"])), "stdout pollution is not caught"

    #  ④ when the server does not start at all, only that is reported
    assert check({}, []) == ["no initialize response —— the server failed to start"]

    #  ⑤ do the probe's arguments use **the same hardened flags** as `.mcp.json`.
    #     Passing with looser flags leaves it green while the real install breaks.
    a = " ".join(docker_args("img", "/k", "/v"))
    for need in ("--network none", "--read-only", "--cap-drop ALL",
                 "no-new-privileges", "/k/db:/data/db:ro", "/v:/vault:ro"):
        assert need in a, f"the probe does not use `{need}` —— it has diverged from .mcp.json"

    #  ⑥ does the manifest-drift judgement itself run (against the repository's current state)
    assert manifest_drift() == [], f"the repository is already divergent: {manifest_drift()}"

    import tempfile as _tf, shutil as _sh
    with _tf.TemporaryDirectory() as _d:
        _repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        os.makedirs(os.path.join(_d, ".claude-plugin"))
        _sh.copy(os.path.join(_repo, ".claude-plugin", "plugin.json"),
                 os.path.join(_d, ".claude-plugin", "plugin.json"))
        _m = json.load(open(os.path.join(_repo, ".mcp.json"), encoding="utf-8"))
        _a = _m["mcpServers"]["kal"]["args"]
        _a[-1] = "ghcr.io/hwangtaehyun/kal:9.9.9"          # only the tag is skewed
        json.dump(_m, open(os.path.join(_d, ".mcp.json"), "w", encoding="utf-8"))
        assert manifest_drift(_d), "tag drift is not caught"
        _a[-1] = "ghcr.io/hwangtaehyun/kal@sha256:" + "a" * 64   # a digest must pass
        json.dump(_m, open(os.path.join(_d, ".mcp.json"), "w", encoding="utf-8"))
        assert manifest_drift(_d) == [], "a digest pin was flagged"

    #  ⑦ userConfig safety —— the guard that came out of the measured case matrix
    assert userconfig_safety() == [], f"the repository is already unsafe: {userconfig_safety()}"

    with _tf.TemporaryDirectory() as _d:
        os.makedirs(os.path.join(_d, ".claude-plugin"))
        _pj = json.load(open(os.path.join(_repo, ".claude-plugin", "plugin.json"), encoding="utf-8"))
        _sh.copy(os.path.join(_repo, ".mcp.json"), os.path.join(_d, ".mcp.json"))

        #  Dropping required without giving a default → an empty-string path.  It must be caught.
        _p2 = json.loads(json.dumps(_pj))
        _p2["userConfig"]["vault_dir"].pop("required", None)
        json.dump(_p2, open(os.path.join(_d, ".claude-plugin", "plugin.json"), "w", encoding="utf-8"))
        assert any("an empty string" in b for b in userconfig_safety(_d)), \
            "dropping required is not caught"

        #  Deleting the key itself means the server will not start —— that is caught too
        _p3 = json.loads(json.dumps(_pj))
        del _p3["userConfig"]["vault_dir"]
        json.dump(_p3, open(os.path.join(_d, ".claude-plugin", "plugin.json"), "w", encoding="utf-8"))
        assert any("is not declared" in b for b in userconfig_safety(_d)), "an undeclared key is not caught"

        #  Deleting the whole userConfig block sends the literal to docker —— the worst
        _p4 = json.loads(json.dumps(_pj)); del _p4["userConfig"]
        json.dump(_p4, open(os.path.join(_d, ".claude-plugin", "plugin.json"), "w", encoding="utf-8"))
        assert any("literal" in b for b in userconfig_safety(_d)), "a missing block is not caught"

        #  Giving a default instead of required **is safe** (row 1 of the matrix)
        _p5 = json.loads(json.dumps(_pj))
        _p5["userConfig"]["vault_dir"].pop("required", None)
        _p5["userConfig"]["vault_dir"]["default"] = "/notes"
        json.dump(_p5, open(os.path.join(_d, ".claude-plugin", "plugin.json"), "w", encoding="utf-8"))
        assert userconfig_safety(_d) == [], "a key with a default was flagged"

    #  ⑧ skill frontmatter —— the place `claude plugin validate` does not check
    assert skills_frontmatter() == [], f"the skills are already divergent: {skills_frontmatter()}"
    with _tf.TemporaryDirectory() as _d:
        _sk = os.path.join(_d, "skills", "probe"); os.makedirs(_sk)
        _f = os.path.join(_sk, "SKILL.md")
        open(_f, "w").write("---\nname: probe\ndescription: x\n---\nbody\n")
        assert skills_frontmatter(_d) == [], "a healthy case was flagged"
        open(_f, "w").write("---\ndescription: x\n---\nbody\n")
        assert any("has no `name`" in b for b in skills_frontmatter(_d)), "a missing name is not caught"
        open(_f, "w").write("---\nname: probe\n---\nbody\n")
        assert any("has no `description`" in b for b in skills_frontmatter(_d)), "a missing description is not caught"
        open(_f, "w").write("only a body\n")
        assert any("no frontmatter block" in b for b in skills_frontmatter(_d)), "a missing block is not caught"
        open(_f, "w").write("---\nname: other-name\ndescription: x\n---\nbody\n")
        assert any("differs from the directory name" in b for b in skills_frontmatter(_d)), "a name mismatch is not caught"

    #  The quoted-message contract, both directions.  Asserting the live repository first
    #  matters more than the mutations: a guard nobody satisfies is worse than no guard.
    assert skills_quoted_messages() == [], \
        f"the quoted messages have already drifted: {skills_quoted_messages()}"

    _repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _repo_copy():
        _d = _tf.mkdtemp()
        for rel in {r for pair in QUOTED_MESSAGES.values() for r in pair}:
            os.makedirs(os.path.join(_d, os.path.dirname(rel)), exist_ok=True)
            _sh.copy(os.path.join(_repo, rel), os.path.join(_d, rel))
        return _d

    def _sub(_p, _a, _b):
        #  ⚠ read **then** open for writing.  `open(p,"w").write(open(p).read()…)` truncates
        #    before the read happens and silently mutates every case into "the file is empty";
        #    it passed a first draft of these very assertions for the wrong reason.
        _t = open(_p, encoding="utf-8").read()
        assert _a in _t, f"fixture text missing: {_a!r}"
        open(_p, "w", encoding="utf-8").write(_t.replace(_a, _b))

    _d = _repo_copy()
    assert skills_quoted_messages(_d) == [], "a healthy copy was flagged"
    _sub(os.path.join(_d, "src/kal_lock.py"), "the DB is already in use — ", "busy: ")
    assert any("no longer emits" in b for b in skills_quoted_messages(_d)), \
        "rewording the source is not caught —— the skill would quote text nothing emits"
    _d = _repo_copy()
    _sub(os.path.join(_d, "skills/kal-maintain/SKILL.md"),
         "needs the claude CLI and it is unavailable", "(removed)")
    assert any("no longer shows" in b for b in skills_quoted_messages(_d)), \
        "dropping the line from the skill is not caught —— an agent meeting it gets no guidance"

    print("  ✅ plugin_probe self-check — healthy · empty version · missing tool · stdout pollution · not started · "
          "hardened flags · manifest drift · userConfig safety · skill frontmatter · quoted messages (both directions)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default=os.environ.get("KAL_IMAGE", "kal:local"))
    ap.add_argument("--kal", default=os.environ.get("KAL_HOME", os.path.expanduser("~/.kal")))
    ap.add_argument("--vault", default=os.environ.get("KAL_VAULT_DIR", ""))
    ap.add_argument("--no-hardening", action="store_true",
                    help="without the hardened flags —— only when isolating what breaks it")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--expect-empty", action="store_true",
                    help="hand it an empty DB and see whether **the guidance sentence** appears (a traceback fails)")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()

    if not a.vault:
        sys.exit("a vault path is required: --vault <path> or KAL_VAULT_DIR")
    if a.expect_empty:
        #  ⚠ Piping JSON-RPC by hand here gives stdin an immediate EOF, so the server answers
        #    initialize and dies.  A CI draft really did fall into that trap.
        #    `probe()` is the only place that knows the stdio conversation, so it is used here too.
        _, seen, err, _ = probe(a.image, a.kal, a.vault, hardened=not a.no_hardening,
                                call_tool="kal_search")
        txt = json.dumps(seen.get(3, {}), ensure_ascii=False)
        for need in ("the index has not been run", "Run this once first"):
            if need not in txt:
                print(f"❌ the empty-DB guidance lacks '{need}'")
                print(f"   response: {txt[:300]}")
                if err.strip():
                    print(f"   stderr: {err.strip().splitlines()[-1][:150]}")
                sys.exit(1)
        if "Traceback" in txt or "ValueError" in txt:
            print(f"❌ a traceback reaches the user: {txt[:200]}")
            sys.exit(1)
        print("  ✅ an empty DB fails with a guidance sentence (not a traceback)")
        return

    if not os.path.isdir(os.path.join(a.kal, "db")):
        sys.exit(f"there is no knowledge DB: {a.kal}/db —— index it first (python src/schema_v3.py)")

    el, seen, err, dirty = probe(a.image, a.kal, a.vault, hardened=not a.no_hardening)
    bad = check(seen, dirty) + manifest_drift() + userconfig_safety() + skills_frontmatter() + skills_quoted_messages()
    if bad:
        print(f"❌ the plugin does not start properly ({el:.1f}s)")
        for b in bad:
            print(f"   · {b}")
        if err.strip():
            print("   ── stderr ──")
            for l in err.strip().splitlines()[-6:]:
                print(f"   {l}")
        sys.exit(1)

    info = seen[1]["result"]["serverInfo"]
    tools = sorted(t["name"] for t in seen[2]["result"]["tools"])
    print(f"  ✅ plugin started — {info['name']} v{info['version']} · {len(tools)} tool(s) · "
          f"{el:.1f}s · stdout clean")
    print(f"     {' · '.join(tools)}")


if __name__ == "__main__":
    main()
