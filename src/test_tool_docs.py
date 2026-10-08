"""The MCP tool list in the docs follows the code.

    uv run python -m unittest src/test_tool_docs.py -v

kal_media was added (2026-10-06) and "six tools" stayed in a dozen places, with tool tables that did
not list it (review 2026-10-09).  Every tool `kal_mcp.py` hands to `@app.tool` must be named in the
three places a user or an agent reads the list from, and no "six tools" phrasing may remain there.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = ("README.md", "docs/CONNECTING-AGENTS.md", "skills/kal-recall/SKILL.md")
STALE = re.compile(r"\b(six|6)\s+(read-only\s+|MCP\s+)?tools\b", re.I)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def registered():
    """Names of the functions decorated with `@app.tool(...)` (the write tools are registered elsewhere, opt-in)."""
    src = read("src/kal_mcp.py")
    return {re.search(r"^def (kal_\w+)", src[m.end():], re.M).group(1) for m in re.finditer(r"^@app\.tool\(", src, re.M)}


class ToolDocs(unittest.TestCase):
    def test_the_registry_is_read(self):
        self.assertGreaterEqual(len(registered()), 7, registered())     # a regex that finds nothing passes everything

    def test_every_tool_is_named(self):
        for rel in DOCS:
            text = read(rel)
            with self.subTest(doc=rel):
                self.assertEqual({t for t in registered() if not re.search(rf"\b{t}\b", text)}, set())

    def test_no_stale_count(self):
        for rel in DOCS:
            with self.subTest(doc=rel):
                self.assertEqual(STALE.findall(read(rel)), [])


if __name__ == "__main__":
    unittest.main()
