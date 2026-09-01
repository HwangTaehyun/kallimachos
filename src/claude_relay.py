#!/usr/bin/env python3
"""A small relay that runs `claude -p` on the host on the container's behalf.

Why it is needed
  Inside a container `claude` is unauthenticated —— macOS keeps credentials in **the keychain**,
  so mounting `~/.claude` does not bring them (docs/STACK.md §6).  That made LLM steps such as
  extraction and distillation impossible to trigger from the web UI.

  This relay runs on the host, takes a prompt, runs `claude -p` and hands the text back.  When
  the container's claude_cli.run() sees KAL_CLAUDE_RELAY it sends here instead of running it itself.

What it does not do —— **it does not touch the DB.**
  This is a pure text function (prompt → text).  The container remains the only writer of
  LanceDB, so the host and the container never overwrite the same DB at once.  That property
  matters because flock was measured (2026-08-18) not to cross the container boundary.

Security
  · It binds to 127.0.0.1 only.  The container reaches it via host.docker.internal, which
    Docker Desktop connects to the host's loopback.
  · A token is required.  Without one it makes and prints a token at startup.
  · Tools run blocked by claude_cli.NO_TOOLS —— the text arriving through this channel includes
    external web clips from Clippings/, so an injection must not turn into an action.
  · Concurrency is limited.  Without it, a container pushing with 14 workers spawns that many
    claude processes on the host.

Usage:
    just relay                     # make a token and start
    KAL_RELAY_TOKEN=... just relay # a fixed token
"""
import argparse
import hmac
import json
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from claude_cli import NO_TOOLS, child_env  # noqa: E402,F401

# How many claude processes may run concurrently on the host.
# lr_extract pushes with 14 workers —— unlimited, that many appear.
MAX_INFLIGHT = int(os.environ.get("KAL_RELAY_CONCURRENCY", "8"))
_sem = threading.BoundedSemaphore(MAX_INFLIGHT)

# The child processes in flight.  A cancel kills these.
#
# Why it is needed —— cancelling the container's run does not kill a claude already started here.
# Measured: after a cancel, 8 claude processes stayed on the host and kept running, and had to
# be cleared by hand.  Those 8 holding the API also slow the next run.  (2026-08-19)
_procs: dict = {}
_procs_lock = threading.Lock()

TOKEN = os.environ.get("KAL_RELAY_TOKEN", "")


def _run_tracked(job, model, prompt, timeout):
    """Launch claude while **keeping a handle** so it can be cancelled.

    Using claude_cli.run() directly leaves no handle until subprocess.run returns, so there is
    no way to kill it.  Same flags and same env, opened with Popen.
    """
    import subprocess
    from claude_cli import NO_TOOLS, child_env
    cmd = ["claude", "-p", "--model", model] + NO_TOOLS
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, env=child_env())
    if job:
        with _procs_lock:
            _procs.setdefault(job, set()).add(p)
    try:
        out, err = p.communicate(prompt, timeout=timeout)
        if p.returncode == 0:
            return out.strip()
        # ⚠ stderr **must not be discarded.**  It used to be `out, _ =` and every failure came
        # back as an empty string.  So a decisive error like "Credit balance is too low" went
        # unseen for 19 hours —— the caller simply retried.
        # (2026-08-19)
        msg = (err or out or "").strip().splitlines()
        print(f"  ⚠ claude exit={p.returncode}: {msg[-1][:200] if msg else '(no output)'}",
              flush=True)
        return ""
    except subprocess.TimeoutExpired:
        p.kill()
        print(f"  ⚠ claude timed out after {timeout}s", flush=True)
        return ""
    except Exception as e:
        print(f"  ⚠ claude failed to run: {type(e).__name__}: {e}", flush=True)
        return ""
    finally:
        if job:
            with _procs_lock:
                _procs.get(job, set()).discard(p)
                if not _procs.get(job):
                    _procs.pop(job, None)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/health":
            return self._json(404, {"error": "not found"})
        # Health needs no token —— the container has to ask "is the relay up", and the only
        # thing leaked here is "it is up".
        self._json(200, {"ok": True, "inflight_max": MAX_INFLIGHT})

    def do_POST(self):
        if self.path != "/run":
            return self._json(404, {"error": "not found"})
        if TOKEN and not hmac.compare_digest(
                str(self.headers.get("X-Relay-Token") or ""), TOKEN):
            return self._json(401, {"error": "the token does not match"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._json(400, {"error": "no length"})
        if n <= 0 or n > 4 * 1024 * 1024:
            return self._json(413, {"error": "the body size is out of range"})
        try:
            body = json.loads(self.rfile.read(n))
        except Exception:
            return self._json(400, {"error": "not JSON"})
        #  ⚠ `"hi"` and `[1,2]` are **valid JSON** too —— and then `body.get` below dies with
        #    AttributeError, and that exception escapes do_POST, so **the connection closes
        #    with no response.**  The caller sees a connection error rather than a 400.
        if not isinstance(body, dict):
            return self._json(400, {"error": "must be a JSON object"})

        prompt = body.get("prompt") or ""
        model = str(body.get("model") or "haiku")
        #  A non-numeric timeout makes int() die —— the same no-response as above.
        #  A lower bound too: at 0 or negative, communicate times out at once and always answers empty.
        try:
            timeout = min(max(int(body.get("timeout") or 180), 1), 900)
        except (TypeError, ValueError):
            return self._json(400, {"error": "timeout must be an integer"})
        if not prompt:
            return self._json(400, {"error": "prompt is empty"})

        job = body.get("job") or ""      # the container's run id.  A cancel finds it by this
        with _sem:
            out = _run_tracked(job, model, prompt, timeout)
        # claude_cli.run reports failure as an empty string.  That distinction is passed through ——
        # the caller (lr_extract) already treats an empty string as "retry".
        self._json(200, {"text": out, "ok": bool(out)})

    def do_DELETE(self):
        """If POST /run carried a job, kill every call in flight for that job."""
        if not self.path.startswith("/job/"):
            return self._json(404, {"error": "not found"})
        if TOKEN and not hmac.compare_digest(
                str(self.headers.get("X-Relay-Token") or ""), TOKEN):
            return self._json(401, {"error": "the token does not match"})
        #  Percent-decoding is what makes it match the key it was stored under.  The run ids Go
        #  currently produces are ASCII, so there is no symptom, but without it the moment the
        #  id format changes it quietly becomes `killed: 0` —— the state where a cancel leaves
        #  the child alive.  (measured 2026-08-21: testing with a non-ASCII id did exactly that.)
        from urllib.parse import unquote
        job = unquote(self.path[len("/job/"):])
        killed = 0
        with _procs_lock:
            for p in list(_procs.get(job, ())):
                try:
                    p.kill()
                    killed += 1
                except Exception:
                    pass
        self._json(200, {"killed": killed})

    def log_message(self, fmt, *args):
        # The prompt is not in the URL, so only the path is logged.  Still, it stays quiet.
        if os.environ.get("KAL_RELAY_VERBOSE"):
            sys.stderr.write("  relay %s\n" % (fmt % args))



def _selftest():
    """**Actually start** the network boundary and knock on it.

    This file is the only network surface running on the host and it had no test at all
    (checked 2026-08-21).  The token check, the size limit and the input validation were all
    "believed to exist".  claude is stubbed out, so there is no real call and no cost.
    """
    global TOKEN
    import http.client
    import threading as _th

    TOKEN = "test-token"  # a fixed value used only inside the self-check.  main() returns without starting a server under --selftest (oh-my-airs:allow)
    calls = []

    def _stub(job, model, prompt, timeout):
        calls.append((job, model, prompt, timeout))
        return "STUBBED"

    _orig, globals()["_run_tracked"] = _run_tracked, _stub
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)   # 0 = pick a free port
    port = srv.server_address[1]
    _th.Thread(target=srv.serve_forever, daemon=True).start()

    def call(method, path, body=None, token=TOKEN, length=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        h = {"X-Relay-Token": token} if token else {}
        payload = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        if length is not None:                 # claim a length different from the real body
            h["Content-Length"] = str(length)
            c.putrequest(method, path)
            for k, v in h.items():
                c.putheader(k, v)
            c.endheaders()
        else:
            c.request(method, path, payload, h)
        r = c.getresponse()
        out = (r.status, json.loads(r.read() or b"{}"))
        c.close()
        return out

    try:
        # ── Authentication ──
        assert call("GET", "/health", token=None)[0] == 200, "health must work without a token"
        assert call("GET", "/nope")[0] == 404
        assert call("POST", "/run", {"prompt": "x"}, token=None)[0] == 401, "it passed with no token"
        assert call("POST", "/run", {"prompt": "x"}, token="wrong")[0] == 401, "a wrong token passed"
        assert call("POST", "/nope", {"prompt": "x"})[0] == 404

        # ── Input validation ──
        #    Non-object JSON used to die at body.get and **produce no response at all.**
        for bad in (b'"hi"', b"[1,2]", b"5"):
            st, _ = call("POST", "/run", bad)
            assert st == 400, (f"non-object JSON is not a 400: {bad!r}", st)
        assert call("POST", "/run", b"{not json")[0] == 400
        assert call("POST", "/run", {"prompt": "x", "timeout": "abc"})[0] == 400, "timeout is unvalidated"
        assert call("POST", "/run", {"prompt": ""})[0] == 400, "an empty prompt passed"
        assert call("POST", "/run", {"prompt": "x"}, length=5 * 1024 * 1024)[0] == 413, "no size limit"
        assert call("POST", "/run", {"prompt": "x"}, length=0)[0] == 413

        # ── The normal path · is timeout clamped into range ──
        st, body = call("POST", "/run", {"prompt": "hello", "timeout": 99999})
        assert st == 200 and body["text"] == "STUBBED" and body["ok"] is True, body
        assert calls[-1][3] == 900, ("the timeout upper bound did not apply", calls[-1])
        #  0 reads as "unspecified" through `0 or 180` and becomes the default (correct).
        #  What the lower bound really works on is a negative —— left alone, communicate dies at once.
        call("POST", "/run", {"prompt": "x", "timeout": 0})
        assert calls[-1][3] == 180, ("0 must become the default", calls[-1])
        call("POST", "/run", {"prompt": "x", "timeout": -5})
        assert calls[-1][3] == 1, ("the timeout lower bound did not apply —— it times out instantly", calls[-1])

        # ── Cancelling ──
        class _P:
            def __init__(self): self.dead = False
            def kill(self): self.dead = True
        p1, p2 = _P(), _P()
        #  ⚠ The job id here **stays non-ASCII on purpose.**  What this exercises is the
        #     percent-decoding in do_DELETE, and an ASCII id passes with or without it ——
        #     the 2026-08-21 measurement found the defect precisely by using one of these.
        _procs["작업 1"] = {p1, p2}
        assert call("DELETE", "/job/x")[1]["killed"] == 0, "it claims to have killed a job that does not exist"
        assert call("DELETE", "/nope")[0] == 404
        assert call("DELETE", "/job/x", token="wrong")[0] == 401, "cancelling works without a token"
        #  A percent-encoded id must match the stored key (it will, once a space or non-ASCII is in it)
        st, body = call("DELETE", "/job/%EC%9E%91%EC%97%85%201")
        assert body["killed"] == 2, ("percent-decoding is not happening", body)
        assert p1.dead and p2.dead
    finally:
        srv.shutdown()
        globals()["_run_tracked"] = _orig
        _procs.clear()
        TOKEN = os.environ.get("KAL_RELAY_TOKEN", "")

    print("  ✅ relay self-check passed — 4 auth · 7 input validation · 2 bounds · 4 cancel")


def main():
    global TOKEN
    if "--selftest" in sys.argv:
        return _selftest()
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("KAL_RELAY_PORT", "8791")))
    ap.add_argument("--host", default="127.0.0.1",
                    help="the default is 127.0.0.1.  Changing it opens an unauthenticated LLM execution channel to the whole network")
    a = ap.parse_args()

    if not TOKEN:
        TOKEN = secrets.token_urlsafe(24)
        print(f"  A new token was generated (pass KAL_RELAY_TOKEN to fix it)")
    print(f"  claude relay → http://{a.host}:{a.port}   at most {MAX_INFLIGHT} concurrent")
    print(f"  KAL_RELAY_TOKEN={TOKEN}")
    print(f"\n  Values for the container side (.env):")
    print(f"    KAL_CLAUDE_RELAY=http://host.docker.internal:{a.port}")
    print(f"    KAL_RELAY_TOKEN={TOKEN}\n")

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  relay stopped")


if __name__ == "__main__":
    main()
