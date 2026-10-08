"""kal_mcp's media reader against a hand-written manifest.  Never touches ~/.kal.

    uv run python -m unittest src/test_kal_mcp_media.py -v

`"media": null` or an item's `"links"` / `"docs"`: null raised TypeError and broke kal_entity and kal_media
outright (review 2026-10-09).  A null collection must read as an empty one.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kal_mcp

SHA = "a" * 64


class NullCollections(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        for p in (mock.patch.dict(os.environ, {"KAL_MEDIA_DIR": self.d}),
                  mock.patch.object(kal_mcp, "fresh", lambda: None),
                  mock.patch.object(kal_mcp, "_released_paths", lambda: {"a.md"}),
                  mock.patch.dict(kal_mcp._MEDIA_CACHE, {"mtime": None, "path": None, "data": {}})):
            p.start()
            self.addCleanup(p.stop)

    def write(self, manifest):
        p = os.path.join(self.d, "manifest.json")
        with open(p, "w") as fh:
            json.dump(manifest, fh)
        kal_mcp._MEDIA_CACHE["mtime"] = None       # same-second rewrites keep the mtime

    def test_media_null(self):
        self.write({"version": 1, "media": None})
        self.assertEqual(kal_mcp._media_manifest(), {})
        self.assertEqual(kal_mcp.media_of("redis"), [])

    def test_links_and_docs_null(self):
        self.write({"version": 1, "media": [{"sha256": SHA, "kind": "image", "links": None, "docs": None}]})
        self.assertEqual(kal_mcp.media_of("redis"), [])
        r = kal_mcp.kal_media(SHA)
        self.assertIsInstance(r, str)
        self.assertEqual(json.loads(r.split("\n(no thumbnail")[0])["docs"], [])

    def test_a_listed_item_still_reads(self):
        link = {"entity": "redis", "name": "Redis", "type": "tool", "basis": "embed", "score": 0.9, "doc": "a.md"}
        self.write({"version": 1, "media": [{"sha256": SHA, "kind": "image", "links": [link], "docs": ["a.md"]}]})
        self.assertEqual([m["sha256"] for m in kal_mcp.media_of("redis")], [SHA])


if __name__ == "__main__":
    unittest.main()
