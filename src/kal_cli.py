#!/usr/bin/env python3
"""`kal` —— the entry point installed through `[project.scripts]` in pyproject.toml.

`pyproject.toml` enables packaging and installs this dispatcher as the `kal` command.
The wheel carries the pipeline's Python modules, so an installed command does not need a
source checkout. In a checkout, `uv sync` installs it into the project environment.
The source recipes remain available: `just login` / `just logout` / `just whoami` /
`just sync-github`, or `python3 src/kal_cli.py <subcommand>` directly. Those invocations use
this same dispatcher; installing the package changes the entry point, not the subcommands.
"""
import sys

import device_auth
import github_sync


def main(argv=None):
    import argparse

    p = argparse.ArgumentParser(prog="kal", description="Kal device login and GitHub sync")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="log this device in to kal cloud (device flow modeled on RFC 8628)")
    p_login.add_argument("--url", default=None, help=f"kal cloud URL (default: {device_auth.DEFAULT_URL})")
    p_login.add_argument("--name", default=None, help="device name shown at approval and in owns paths")

    sub.add_parser("logout", help="remove this device's credential")
    sub.add_parser("whoami", help="show this device's login (url + device name — never the token)")
    p_sync = sub.add_parser("sync", help="pull, distil, export, commit, push — this device's turn")
    p_auto = sub.add_parser("autosync", help="run sync now and every 30 minutes in the foreground; opt-in only")
    p_end = sub.add_parser("session-end", help="run one sync cycle from a session-end hook")
    for command in (p_sync, p_auto, p_end):
        command.add_argument("--repository", default=None, metavar="ID",
                             help="repository ID from `kal repositories`; required with multiple repositories")
        command.add_argument("--notify-build", action="store_true",
                             help="request a cloud build after a successful push; off by default")
    p_auto.add_argument("--interval", type=int, default=1800, metavar="SECONDS",
                        help="seconds between completed cycles (default: 1800)")
    p_sync.add_argument("--status", action="store_true",
                        help="show the outcome of the last sync (including hook and autosync runs) and exit")
    p_end.add_argument("--print-hook", action="store_true",
                       help="print a Claude Code SessionEnd hook snippet without installing or running it")
    sub.add_parser("repositories", help="list connected repository IDs using this device's credential")

    args = p.parse_args(argv)
    if args.cmd in ("sync", "autosync", "session-end") and args.repository is not None and not args.repository.strip():
        p.error("--repository must be a non-empty ID from `kal repositories`")
    if args.cmd == "autosync" and args.interval <= 0:
        p.error("--interval must be a positive number of seconds")
    if args.cmd == "sync" and args.status:
        import time

        last = github_sync.read_last_result()
        if last is None:
            print("  no sync has been recorded yet")
            return 0
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last.get("time", 0)))
        print(f"  last sync: {'ok' if last['ok'] else 'FAILED'} at {when}")
        if last.get("message"):
            print(f"  {last['message']}")
        return 0 if last["ok"] else 1
    if args.cmd == "session-end" and args.print_hook:
        import json
        import shlex

        command = [sys.executable, "-m", "kal_cli", "session-end"]
        if args.repository is not None:
            command.extend(["--repository", str(args.repository)])
        if args.notify_build:
            command.append("--notify-build")
        print(json.dumps({"hooks": {"SessionEnd": [{"hooks": [
            {"type": "command", "command": shlex.join(command), "timeout": 1800}
        ]}]}}, indent=2))
        return 0

    if args.cmd == "login":
        try:
            device_auth.login(url=args.url, device_name=args.name)
        except device_auth.LoginError as e:
            print(f"  ❌ {e}", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "logout":
        device_auth.logout()
        return 0
    if args.cmd == "whoami":
        device_auth.whoami()
        return 0
    if args.cmd in ("sync", "session-end", "autosync", "repositories"):
        try:
            if args.cmd == "repositories":
                cred = device_auth.load_credential()
                if cred is None:
                    raise github_sync.SyncError("not logged in — run `kal login` first")
                for repo in github_sync.list_repositories(cred["url"], cred["token"]):
                    state = "enabled" if repo.get("enabled") else "disabled"
                    print(f"{repo['id']}\t{repo['owner']}/{repo['name']}\t{state}\t{repo.get('status', '')}")
            elif args.cmd == "autosync":
                import signal
                import threading

                stop = threading.Event()
                previous = signal.signal(signal.SIGTERM, lambda *_: stop.set())
                try:
                    github_sync.autosync(repository_id=args.repository, interval=args.interval,
                                         stop=stop, notify_build=args.notify_build)
                except KeyboardInterrupt:
                    return 130
                finally:
                    signal.signal(signal.SIGTERM, previous)
            else:
                github_sync.sync(repository_id=args.repository, notify_build=args.notify_build)
        except github_sync.BuildNotificationError as e:
            print(str(e), file=sys.stderr)
            return 2
        except github_sync.SyncError as e:
            print(f"  ❌ {e}", file=sys.stderr)
            return 1
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
