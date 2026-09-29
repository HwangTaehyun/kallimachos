#!/usr/bin/env python3
"""Device-code login against `kal cloud`.

`kal login` gets this machine a long-lived **device credential** (a `kal_dev_...` token, scope
`device`) in a flow modeled on RFC 8628 (OAuth 2.0 Device Authorization Grant, published 2019-08,
https://datatracker.ietf.org/doc/html/rfc8628) against the server endpoints already implemented in
`cloud/api/device.go`:

    POST /api/device/code   {device_name}              -> {device_code, user_code, verification_uri,
                                                             verification_uri_complete, expires_in, interval}
                                                          (legacy verify_uri / verify_uri_complete also accepted)
    POST /api/device/token  {device_code}  (polled)     -> {access_token, token_type}
                                                          | {error: authorization_pending|slow_down
                                                                   |expired_token|access_denied}

The credential is **not** a GitHub token — it only proves "this device" to `kal cloud` later, when
`github_sync.py` asks for a short-lived GitHub installation token. It is never printed after
the first save, never logged, and lives outside `~/.kal` on purpose (~/.kal is a docker
volume mount — do not add a secret to a surface that already gets backed up/cloned/imaged).

Storage: `~/.config/kal/device.json` (dir 0700, file 0600) — `{url, device_name, token, saved_at}`.
"""
import json
import os
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CRED_DIR = os.path.expanduser("~/.config/kal")
CRED_PATH = os.path.join(CRED_DIR, "device.json")

DEFAULT_URL = "https://app.kallimachos.dev"
HTTP_TIMEOUT = 15


class LoginError(Exception):
    """A clean, user-facing failure — never a raw traceback (device flow, HTTP, or timeout)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: a Bearer token must never be replayed to a URL the user did not
    type. A 3xx surfaces as an HTTPError instead."""

    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(_NoRedirect)


def check_url(url):
    """Only https://, or plain http:// to a loopback host for local development. Raises ValueError."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return
    if parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1"):
        return
    raise ValueError("kal cloud URL must use https:// (http:// is allowed only for localhost)")


def _post(url, payload):
    """POST JSON, return (status, dict). Never raises on a non-2xx — device flow reads the body
    of 400s (authorization_pending, slow_down, ...) as normal control flow, not errors."""
    try:
        check_url(url)
    except ValueError as e:
        raise LoginError(str(e)) from e
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with OPENER.open(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return e.code, {}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LoginError(f"could not reach {url}: {e}") from e


def save_credential(url, device_name, token):
    """Write the device credential. 0700 dir, 0600 file — never inside ~/.kal."""
    os.makedirs(CRED_DIR, mode=0o700, exist_ok=True)
    os.chmod(CRED_DIR, 0o700)
    tmp = CRED_PATH + ".tmp"
    data = {"url": url.rstrip("/"), "device_name": device_name, "token": token, "saved_at": int(time.time())}
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(data).encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, CRED_PATH)  # atomic — never a half-written credential on disk


def load_credential():
    """The saved device credential, or None if not logged in / unreadable."""
    try:
        with open(CRED_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if not all(k in data for k in ("url", "device_name", "token")):
            return None
        return data
    except (FileNotFoundError, ValueError, OSError):
        return None


def delete_credential():
    """`kal logout`. No error if there was nothing to delete."""
    try:
        os.remove(CRED_PATH)
        return True
    except FileNotFoundError:
        return False


def login(url=None, device_name=None, sleep=time.sleep, now=time.time, print_fn=print):
    """Device-code flow modeled on RFC 8628. Blocks (polling) until approved, denied, or expired.

    `sleep`/`now`/`print_fn` are injected so the self-check can run this without a real clock or
    terminal — same pattern the rest of this pipeline uses for testability.
    """
    url = (url or DEFAULT_URL).rstrip("/")
    device_name = device_name or os.uname().nodename.split(".")[0]
    status, body = _post(url + "/api/device/code", {"device_name": device_name})
    if status != 200:
        raise LoginError(f"could not start login: HTTP {status} {body}")
    # RFC 8628 names first; the legacy verify_uri* names are the fallback for older servers.
    verification_uri = body.get("verification_uri") or body.get("verify_uri")
    verification_uri_complete = body.get("verification_uri_complete") or body.get("verify_uri_complete")
    if not body.get("device_code") or not verification_uri or not body.get("user_code"):
        raise LoginError("unexpected response from the login server: missing device_code, user_code or verification_uri")
    device_code = body["device_code"]
    interval = int(body.get("interval") or 5)
    expires_at = now() + int(body.get("expires_in") or 900)

    print_fn(f"  Open {verification_uri} and enter code: {body['user_code']}")
    if verification_uri_complete:
        print_fn(f"  (or open {verification_uri_complete} directly)")

    while now() < expires_at:
        sleep(interval)
        status, tok = _post(url + "/api/device/token", {"device_code": device_code})
        if status == 200 and tok.get("access_token"):
            save_credential(url, device_name, tok["access_token"])
            print_fn(f"  logged in as device {device_name!r} on {url}")
            return tok["access_token"]
        err = tok.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err == "expired_token":
            raise LoginError("login code expired — run `kal login` again")
        if err == "access_denied":
            raise LoginError("login was denied")
        raise LoginError(f"unexpected response: HTTP {status} {tok}")
    raise LoginError("login code expired — run `kal login` again")


def logout(print_fn=print):
    if delete_credential():
        print_fn("  logged out — device credential removed")
    else:
        print_fn("  not logged in (nothing to remove)")


def whoami(print_fn=print):
    """Never prints the token — only what identifies this device to a human."""
    cred = load_credential()
    if cred is None:
        print_fn("  not logged in — run `kal login`")
        return None
    print_fn(f"  {cred['device_name']}  ({cred['url']})")
    return cred


# ───────────────────────────── CLI ─────────────────────────────

def main(argv=None):
    import argparse

    p = argparse.ArgumentParser(prog="kal", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    p_login = sub.add_parser("login", help="log this device in to kal cloud")
    p_login.add_argument("--url", default=None)
    p_login.add_argument("--name", default=None)
    sub.add_parser("logout", help="remove this device's credential")
    sub.add_parser("whoami", help="show this device's login (never the token)")
    args = p.parse_args(argv)

    if args.cmd == "login":
        try:
            login(url=args.url, device_name=args.name)
        except LoginError as e:
            print(f"  ❌ {e}", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "logout":
        logout()
        return 0
    if args.cmd == "whoami":
        whoami()
        return 0
    return 1


def _selftest():
    """A fake HTTP server standing in for kal cloud (stdlib http.server — no network, no real
    server). Exercises: pending → approved happy path, slow_down backoff, expired_token, and that
    whoami/logout never touch the token string."""
    import http.server
    import threading
    import tempfile

    calls = {"code": 0, "token": 0}
    script = {"token_polls": ["authorization_pending", "slow_down", "approve"]}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass  # keep test output quiet

        def _json(self, status, obj):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/device/code":
                calls["code"] += 1
                self._json(200, {
                    "device_code": "dc1", "user_code": "ABCD-EFGH",
                    "verify_uri": "http://x/device", "verify_uri_complete": "http://x/device?user_code=ABCD-EFGH",
                    "expires_in": 900, "interval": 0,
                })
                return
            if self.path == "/api/device/token":
                calls["token"] += 1
                step = script["token_polls"].pop(0) if script["token_polls"] else "approve"
                if step == "authorization_pending":
                    self._json(400, {"error": "authorization_pending"})
                elif step == "slow_down":
                    self._json(400, {"error": "slow_down", "interval": 1})
                else:
                    self._json(200, {"access_token": "kal_dev_TESTTOKEN", "token_type": "bearer"})
                return
            self._json(404, {})

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{srv.server_port}"

    global CRED_DIR, CRED_PATH
    old_dir, old_path = CRED_DIR, CRED_PATH
    tmp = tempfile.mkdtemp(prefix="kal-device-auth-")
    CRED_DIR = tmp
    CRED_PATH = os.path.join(tmp, "device.json")
    try:
        printed = []
        token = login(url=url, device_name="test-mac", sleep=lambda s: None, print_fn=printed.append)
        assert token == "kal_dev_TESTTOKEN", token
        assert calls["code"] == 1 and calls["token"] == 3, calls  # pending, slow_down, approve
        assert not any("TESTTOKEN" in ln for ln in printed), "login printed the token"

        st = os.stat(CRED_PATH)
        assert stat.S_IMODE(st.st_mode) == 0o600, oct(st.st_mode)
        assert stat.S_IMODE(os.stat(CRED_DIR).st_mode) == 0o700

        who = []
        cred = whoami(print_fn=who.append)
        assert cred["device_name"] == "test-mac"
        assert not any("TESTTOKEN" in ln for ln in who), "whoami printed the token"

        out = []
        logout(print_fn=out.append)
        assert load_credential() is None
        assert whoami(print_fn=lambda *_: None) is None

        # ── expired_token → a clean LoginError, never a traceback ──────────────────────
        script["token_polls"] = []
        srv.RequestHandlerClass  # noqa — keep handler alive
        class ExpiredHandler(Handler):
            def do_POST(self):
                if self.path == "/api/device/code":
                    return Handler.do_POST(self)
                if self.path == "/api/device/token":
                    self._json(400, {"error": "expired_token"})
                    return
                self._json(404, {})
        srv.RequestHandlerClass = ExpiredHandler
        try:
            login(url=url, device_name="t2", sleep=lambda s: None, print_fn=lambda *_: None)
            assert False, "expired_token must raise LoginError"
        except LoginError as e:
            assert "expired" in str(e)

        print("  ✅ device_auth self-check —— pending/slow_down/approve polling · 0600/0700 perms · "
              "whoami/logout never print the token · expired_token raises a clean LoginError")
    finally:
        srv.shutdown()
        CRED_DIR, CRED_PATH = old_dir, old_path
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        sys.exit(main())
