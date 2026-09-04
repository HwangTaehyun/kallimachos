#!/usr/bin/env python3
"""Do the two pipeline shell scripts agree about writing into the vault.

`rebuild_all.sh` and `export_all.sh` both export the graph, and both can write **into the vault**:
`export_graph.py --obsidian "$VAULT/kg"` drops ~700 generated notes there, and the plugin install
creates `.obsidian/plugins/kal-galaxy/`.  Neither is wanted when `KAL_VAULT` points at an openwiki
bundle —— a published git repository —— and the bundle really did acquire 3.5 MB of generated pages
that way (2026-09-04).

The two scripts diverged for a day: `export_all.sh` gained the `.obsidian/` gate and
`rebuild_all.sh` did not, while the only check on either was `bash -n`, which is green for any
syntactically valid script.  That is this repository's recurring shape —— two code paths answering
the same question differently —— so the question is asked of both.

⚠ `KAL_PYTHON=/usr/bin/true` makes every python step a no-op, so **nothing is written**; only the
   branch taken is observed.  `bash -x` reports it on stderr.  (codex adversarial review 2026-09-04)
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
#  The two writes that must be gated, as they appear in a `bash -x` trace.
VAULT_WRITES = ("--obsidian", ".obsidian/plugins")


def trace(script, vault, home):
    """Run the script with every python step neutered.  → the `bash -x` trace."""
    env = {**os.environ, "KAL_VAULT": vault, "KAL_HOME": home,
           "KAL_PATH": os.path.join(home, "db"), "KAL_PYTHON": "/usr/bin/true",
           "SKIP_DISTILL": "1", "SKIP_KG": "1"}
    r = subprocess.run(["bash", "-x", os.path.join(HERE, script)],
                       env=env, capture_output=True, text=True)
    return r.stdout + r.stderr


def main():
    bad = []
    with tempfile.TemporaryDirectory() as d:
        plain = os.path.join(d, "plain-vault")      # no .obsidian/ —— an openwiki bundle
        obs = os.path.join(d, "obsidian-vault")     # a real Obsidian vault
        home = os.path.join(d, "home")
        for p in (plain, obs, home, os.path.join(obs, ".obsidian")):
            os.makedirs(p, exist_ok=True)

        for script in ("rebuild_all.sh", "export_all.sh"):
            #  ① a vault with no .obsidian/ must produce **no** vault write
            t = trace(script, plain, home)
            for w in VAULT_WRITES:
                if w in t:
                    bad.append(f"{script}: writes into a vault that is not an Obsidian vault "
                               f"({w!r} appears in the trace) —— an openwiki bundle would gain "
                               f"generated files that go out with it")
            #  ② …and the check must not be satisfied by the script simply doing nothing.
            #     Without this, deleting the export step entirely would pass ①.
            if "export_graph.py" not in t:
                bad.append(f"{script}: did not reach the graph export at all, so ① proves nothing")

            #  ③ a real Obsidian vault must still get its notes —— or the gate is over-blocking
            #     and someone will remove it the first time it costs them the feature.
            t2 = trace(script, obs, home)
            if "--obsidian" not in t2:
                bad.append(f"{script}: an Obsidian vault did not get the notes projection —— "
                           f"the gate blocks the case it exists to allow")

    for m in bad:
        print(f"  ❌ {m}", file=sys.stderr)
    if bad:
        sys.exit(1)
    print("  ✅ rebuild_all.sh · export_all.sh agree —— no vault writes without .obsidian/, "
          "and an Obsidian vault still gets them")


if __name__ == "__main__":
    main()
