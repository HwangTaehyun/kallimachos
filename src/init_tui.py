#!/usr/bin/env python3
"""`just init` —— the setup TUI.  It asks, shows how long it will take, and runs it through.

Why it is needed
  Until now a first-time user had to **work this sequence out for themselves**:

      just setup → check VAULT_DIR in .env → just vault …
      → just run index → (if the knowledge graph is wanted) just run extract → then index again
      → just run export → just up

  Seven steps, and two of them **cannot be guessed**:
    · `index` has to run **again** after `extract` (extract only writes a file)
    · the vault must be written in **two places** (config.json for the CLI, .env for the container)
  Getting it wrong raises no error.  Search just comes back empty, or relations never appear,
  and the cause is written nowhere on screen.

What it is built with
  [Textual](https://textual.textualize.io) 8.2.8.  Hand-written ANSI was tried and removed ——
  progress bars, ETA, a scrolling log and keyboard navigation are already solved problems.
  `ProgressBar` carries `show_eta` as a widget default.

  ⚠ Estimation and execution run **on worker threads**.  Importing `schema_v3` alone takes
    about 6 seconds because of torch, and doing that on the UI thread freezes the screen ——
    which the user reads as "it has hung".

Usage
  just init                 interactive
  just init -- --dry        the plan only (does not run)
  python src/init_tui.py --headless-selftest
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PY = os.environ.get("KAL_PYTHON", sys.executable)

from textual import on, work                                    # noqa: E402
from textual.app import App, ComposeResult                      # noqa: E402
from textual.containers import Horizontal, Vertical, VerticalScroll  # noqa: E402
from textual.widgets import (                                   # noqa: E402
    Button, DataTable, Footer, Header, Input, Label,
    OptionList, ProgressBar, RichLog, Rule, Static, Switch,
)
from textual.widgets.option_list import Option                  # noqa: E402

#  The `N/M` a child prints is read as progress.  lr_extract prints this shape every 25 items.
#  **It has to be at the start of the line** —— matching any digit pair anywhere turns a
#  sentence like "2 of 376 documents" into progress.
PROGRESS_RE = re.compile(r"^\s*(\d+)/(\d+)\b")


# ── Finding vault candidates ───────────────────────────────────────────────

def count_md(d: str, cap: int = 20000) -> int:
    """The `.md` count.  Capped so it does not stall on a large tree."""
    n = 0
    for root, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if not x.startswith(".") and x != "node_modules"]
        n += sum(1 for f in files if f.endswith(".md"))
        if n >= cap:
            break
    return n


def _env_file_value(key: str) -> str | None:
    p = os.path.join(REPO, ".env")
    if not os.path.exists(p):
        return None
    for ln in open(p, encoding="utf-8"):
        if ln.startswith(f"{key}="):
            return ln.split("=", 1)[1].strip()
    return None


def candidates() -> list[dict]:
    """Vault candidates.  **Obsidian vaults come first** —— the graph viewer assumes one."""
    try:
        import kal_config
        cfg = kal_config.path_override("vault")
    except Exception:
        cfg = None
    guesses = [
        os.environ.get("KAL_VAULT"),
        _env_file_value("VAULT_DIR"),
        cfg,
        os.path.dirname(REPO),                       # when the repository sits inside the vault
        os.path.expanduser("~/Documents/Obsidian"),
        os.path.expanduser("~/Obsidian"),
        os.path.expanduser("~/notes"),
    ]
    seen, out = set(), []
    for g in guesses:
        if not g:
            continue
        g = os.path.abspath(os.path.expanduser(g))
        if g in seen or not os.path.isdir(g):
            continue
        seen.add(g)
        n = count_md(g)
        if n:
            out.append({"path": g, "md": n,
                        "obsidian": os.path.isdir(os.path.join(g, ".obsidian"))})
    out.sort(key=lambda x: (not x["obsidian"], -x["md"]))
    return out


def plan_for(want_kg: bool) -> list[str]:
    """The steps to run.  **The index after extract is the point** —— without it the extracted
    entities stay in a file and never reach search.  Nobody should have to memorise the order."""
    return ["index"] + (["extract", "index"] if want_kg else []) + ["export"]


def fmt(sec: float) -> str:
    sec = int(max(0, sec))
    return f"{sec // 60}:{sec % 60:02d}" if sec >= 60 else f"{sec}s"


# ── The app ───────────────────────────────────────────────────────────────

class SetupApp(App):
    CSS = """
    Screen { layout: vertical; }
    #body { padding: 1 2; }
    .sect { color: $accent; text-style: bold; margin-top: 1; }
    .hint { color: $text-muted; }
    .warn { color: $warning; }
    #vaults { height: auto; max-height: 10; border: round $panel; }
    #custom { display: none; }
    #plan { height: auto; max-height: 12; }
    #runbox { display: none; height: 1fr; }
    #log { height: 1fr; border: round $panel; }
    Switch { margin-right: 1; }
    #total { text-style: bold; }
    """
    BINDINGS = [("q", "quit", "quit"), ("enter", "noop", "select")]
    TITLE = "kallimachos setup"
    SUB_TITLE = "Pick a notes folder and get to a searchable state"

    def __init__(self, dry: bool = False):
        super().__init__()
        self.dry = dry
        self.cands: list[dict] = []
        self.vault: str | None = None
        self.est: dict = {}
        self.started = 0.0

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="body"):
            yield Label("1 · the notes folder to index", classes="sect")
            yield OptionList(id="vaults")
            yield Input(placeholder="type a folder path and press Enter", id="custom")
            yield Label("", id="vaultnote", classes="hint")

            yield Rule()
            yield Label("2 · how far to build", classes="sect")
            with Horizontal():
                yield Switch(value=False, id="kg")
                yield Label("knowledge graph (entities and relations) — calls an LLM", id="kglabel")
            yield Label("", id="kgnote", classes="hint")
            with Horizontal():
                yield Switch(value=False, id="web")
                yield Label("open the web UI when it finishes (docker)", id="weblabel")

            yield Rule()
            yield Label("3 · the plan", classes="sect")
            yield DataTable(id="plan", cursor_type="none")
            yield Label("", id="total")
            with Horizontal():
                yield Button("start", variant="primary", id="start")
                yield Button("quit", id="quit")

        with Vertical(id="runbox"):
            yield Label("", id="steplabel", classes="sect")
            yield ProgressBar(id="bar", show_eta=True)
            yield RichLog(id="log", wrap=False, max_lines=2000)
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#plan", DataTable)
        t.add_columns("", "step", "estimate")
        self.query_one("#vaults", OptionList).add_option(
            Option("looking for candidates…", disabled=True))
        self.scan()

    # ── Finding candidates (worker) ──
    @work(thread=True, exclusive=True)
    def scan(self) -> None:
        cands = candidates()
        self.call_from_thread(self._show_cands, cands)

    def _show_cands(self, cands: list[dict]) -> None:
        self.cands = cands
        ol = self.query_one("#vaults", OptionList)
        ol.clear_options()
        for x in cands:
            tag = "  [green]· an Obsidian vault[/]" if x["obsidian"] else ""
            ol.add_option(Option(f"{x['path']}\n   [dim]{x['md']:,} .md[/]{tag}"))
        ol.add_option(Option("type it in…"))
        if cands:
            ol.highlighted = 0
            self._pick(cands[0]["path"])

    # ── Selection ──
    @on(OptionList.OptionSelected, "#vaults")
    def _on_pick(self, ev: OptionList.OptionSelected) -> None:
        if ev.option_index >= len(self.cands):
            box = self.query_one("#custom", Input)
            box.styles.display = "block"
            box.focus()
            return
        self._pick(self.cands[ev.option_index]["path"])

    @on(Input.Submitted, "#custom")
    def _on_custom(self, ev: Input.Submitted) -> None:
        p = os.path.abspath(os.path.expanduser(ev.value.strip()))
        if not os.path.isdir(p):
            self.query_one("#vaultnote", Label).update(
                f"[red]no such folder: {p}[/]")
            return
        self._pick(p)

    def _pick(self, path: str) -> None:
        self.vault = path
        obs = os.path.isdir(os.path.join(path, ".obsidian"))
        note = f"selected: {path}"
        if not obs:
            #  It does not block —— search and MCP still work.  It says **in advance** what will not.
            note += "\n[yellow]⚠ not an Obsidian vault —— search and MCP work, but the graph viewer cannot be used.[/]"
        self.query_one("#vaultnote", Label).update(note)
        self.recompute()

    @on(Switch.Changed)
    def _on_switch(self, _ev: Switch.Changed) -> None:
        self.recompute()

    # ── Estimating (worker) ──
    def recompute(self) -> None:
        if not self.vault:
            return
        self.query_one("#total", Label).update("[dim]calculating…[/]")
        self.estimate_worker(self.vault, self.query_one("#kg", Switch).value)

    @work(thread=True, exclusive=True)
    def estimate_worker(self, vault: str, want_kg: bool) -> None:
        #  Estimating walks the whole vault and loads torch —— on the UI thread it freezes.
        os.environ["KAL_VAULT"] = vault
        import importlib
        import estimate
        importlib.reload(estimate)
        rows, total = [], 0.0
        for sid in plan_for(want_kg):
            try:
                e = estimate.estimate(sid)
            except Exception as ex:            # a failed estimate must not block setup
                e = {"title": sid, "seconds": 0, "units": None,
                     "unit": "", "basis": f"estimate failed: {ex}"}
            total += e["seconds"]
            rows.append(e)
        try:
            ext = estimate.estimate("extract")
        except Exception:
            ext = None
        self.call_from_thread(self._show_plan, rows, total, ext)

    def _show_plan(self, rows: list[dict], total: float, ext: dict | None) -> None:
        self.est = {"rows": rows, "total": total}
        t = self.query_one("#plan", DataTable)
        t.clear()
        for i, e in enumerate(rows, 1):
            work = f"{e['units']:,} {e['unit']}" if e.get("units") is not None else ""
            t.add_row(str(i), f"{e['title']}  [dim]{work}[/]", fmt(e["seconds"]))
        self.query_one("#total", Label).update(f"total estimate  {fmt(total)}")
        if ext:
            n = ext.get("units")
            self.query_one("#kgnote", Label).update(
                f"[dim]work {n:,} {ext['unit']} · estimate {fmt(ext['seconds'])}"
                f" — document content leaves this machine through `claude -p`.[/]"
                if n else "[dim]no new chunks to extract (they are already cached).[/]")

    # ── Running ──
    @on(Button.Pressed, "#quit")
    def _quit(self) -> None:
        self.exit(0)

    @on(Button.Pressed, "#start")
    def _start(self) -> None:
        if not self.vault:
            return
        #  The vault is written **in two places**.  Writing one leaves the CLI and the web
        #  looking at different places —— with no error, just a screen that disagrees with the DB.
        import kal_config
        import set_vault
        kal_config.save_path("vault", self.vault)
        set_vault.env_set("VAULT_DIR", self.vault)
        set_vault.env_set("VAULT_NAME", os.path.basename(self.vault))
        if self.dry:
            self.exit(0)
            return
        self.query_one("#body").styles.display = "none"
        self.query_one("#runbox").styles.display = "block"
        self.started = time.time()
        self.run_plan(plan_for(self.query_one("#kg", Switch).value),
                      self.query_one("#web", Switch).value)

    @work(thread=True, exclusive=True)
    def run_plan(self, plan: list[str], want_web: bool) -> None:
        from status import STEP_BY_ID
        for i, sid in enumerate(plan, 1):
            st = STEP_BY_ID[sid]
            self.call_from_thread(self._step_begin, i, len(plan), st["title"])
            cmd = list(st["cmd"])
            head = os.path.join(HERE, cmd[0])
            argv = ([head] if cmd[0].endswith(".sh") else [PY, head]) + cmd[1:]
            p = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, cwd=HERE,
                env={**os.environ, "PYTHONUNBUFFERED": "1", "KAL_VAULT": self.vault})
            for line in p.stdout:
                self.call_from_thread(self._log, line.rstrip())
                m = PROGRESS_RE.match(line)
                if m:
                    self.call_from_thread(self._progress, int(m.group(1)), int(m.group(2)))
            code = p.wait()
            if code:
                self.call_from_thread(self._failed, sid, code)
                return
        self.call_from_thread(self._done, want_web)

    def _step_begin(self, i: int, n: int, title: str) -> None:
        self.query_one("#steplabel", Label).update(f"[{i}/{n}]  {title}")
        #  The total is not known yet → an indeterminate bar.  It becomes determinate once the child prints `N/M`.
        #  Filling it from elapsed/estimated would be a lie that sits at 100% while still running.
        self.query_one("#bar", ProgressBar).update(total=None, progress=0)

    def _progress(self, done: int, total: int) -> None:
        self.query_one("#bar", ProgressBar).update(total=total, progress=done)

    def _log(self, line: str) -> None:
        self.query_one("#log", RichLog).write(line)

    def _failed(self, sid: str, code: int) -> None:
        self.query_one("#steplabel", Label).update(
            f"[red]✗ stopped at {sid} (exit code {code})[/]  ——  again: just run {sid}")

    def _done(self, want_web: bool) -> None:
        el = time.time() - self.started
        self.query_one("#steplabel", Label).update(
            f"[green]✅ finished  {fmt(el)}[/]   (estimated {fmt(self.est.get('total', 0))})")
        self.query_one("#bar", ProgressBar).update(total=1, progress=1)
        log = self.query_one("#log", RichLog)
        log.write("")
        log.write('next:   just search "query"   ·   just status   ·   just up')
        if want_web:
            log.write("opening the web UI…")
            subprocess.run(["docker", "compose", "up", "-d"], cwd=REPO)
            log.write("→ http://localhost:5173")


def main() -> int:
    #  ⚠ **It does not start outside a terminal.**  A TUI waits for input, so called from a
    #    pipe or CI it hangs forever —— measured (2026-08-24): `just init | head` sat there
    #    until it timed out.  From outside, hung and running look the same.
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("  This command has to be run directly in a terminal (it is a TUI and needs input).\n"
              "  From a script, use instead:\n"
              "    just vault <path>     set where to index\n"
              "    just run index -y     index without confirming\n"
              "    just plan             only how long it will take",
              file=sys.stderr)
        return 2
    app = SetupApp(dry="--dry" in sys.argv)
    app.run()
    return 0


# ── Self-check ────────────────────────────────────────────────────────────
#    The TUI itself is started headless to see whether the widgets really attach.  A typo in a
#    widget id only blows up at runtime in `query_one`, so it is missed unless it is started.

def _selftest() -> None:
    import asyncio
    ok = 0

    #  ① the plan —— index has to come **again** after extract (the reason this wizard exists)
    assert plan_for(False) == ["index", "export"]
    assert plan_for(True) == ["index", "extract", "index", "export"], plan_for(True)
    ok += 2

    #  ② progress parsing —— only an N/M at the start of the line
    assert PROGRESS_RE.match("  350/1139  4.2 min  (7 min left)").groups() == ("350", "1139")
    assert PROGRESS_RE.match("of 376 documents, 2/3") is None, "it matched digits inside a sentence"
    ok += 2

    #  ③ the time format
    assert fmt(59) == "59s" and fmt(60) == "1:00" and fmt(-5) == "0s"
    ok += 1

    #  ④ counting `.md` —— dot folders and node_modules are not counted (.obsidian holds md too)
    import tempfile
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, ".obsidian"))
    os.makedirs(os.path.join(d, "node_modules"))
    for p in ("a.md", "b.md", ".obsidian/x.md", "node_modules/y.md"):
        open(os.path.join(d, p), "w").close()
    assert count_md(d) == 2, count_md(d)
    ok += 1

    #  ⑤ **It does not start outside a terminal.**  Miss this and a pipe or CI hangs forever
    #     —— from outside, hung and running look the same (measured 2026-08-24).
    assert main() == 2, "it started the TUI outside a TTY"
    ok += 1

    #  ⑥ **Do the widgets really attach.**  A typo in an id is caught only here.
    async def _boot():
        app = SetupApp(dry=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            for wid in ("#vaults", "#custom", "#kg", "#web", "#plan",
                        "#total", "#start", "#quit", "#bar", "#log",
                        "#steplabel", "#vaultnote", "#kgnote"):
                assert app.query_one(wid) is not None, wid
            #  The run screen must start hidden
            assert app.query_one("#runbox").styles.display == "none"
            #  Does the progress update run without an exception (indeterminate → determinate)
            app._step_begin(1, 2, "test")
            app._progress(3, 10)
            app._log("one line")
    asyncio.run(_boot())
    ok += 13

    #  ⚠ Printed through `sys.__stdout__`.  Textual's `run_test()` captures stdout and **does
    #    not give it back**, so an ordinary print disappears entirely.
    #    Measured (2026-08-24): exit=0 with nothing on screen —— indistinguishable from the
    #    check not running.  (The assertions themselves were fine.  Confirmed by breaking one on purpose.)
    print(f"  ✅ init_tui self-check —— {ok} case(s) "
          f"(plan order · progress parsing · time · md counting · 13 widgets attached)",
          file=sys.__stdout__, flush=True)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        raise SystemExit(0)
    raise SystemExit(main())
