"""media.py —— scan / link / derive, against a temp vault and a temp KAL_HOME.  Never touches ~/.kal.

    uv run python -m unittest src/test_media.py -v

OCR is mocked (tesseract is optional); the graph is a monkeypatched loader (no lancedb needed).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import media
from PIL import Image


def gps_jpeg(path, size=(3000, 2000)):
    im = Image.new("RGB", size, (200, 30, 30))
    ex = Image.Exif()
    ex[0x0132] = "2026:03:04 10:11:12"
    ex.get_ifd(0x8769)[0x9003] = "2026:03:04 10:11:12"
    gps = ex.get_ifd(0x8825)
    gps[1], gps[2], gps[3], gps[4] = "N", (37.0, 30.0, 0.0), "E", (127.0, 0.0, 0.0)
    im.save(path, "JPEG", exif=ex)


def png(path, color="blue", size=(64, 48)):
    Image.new("RGB", size, color).save(path)


NOTE = """---
title: Checkout
---
# Redis checkout
The Redis flow is described here. ![[shot.jpg]]

# Other section
Kal is mentioned only down here.
![alt](img/inline.png)
![remote](https://example.com/x.png)
"""
NOTE_FM = """---
media: [board.png]
entities: [Payment Module]
---
# Board
"""
NO_LLM_NOTE = "---\nno_llm: true\n---\n# Secret\n![[secret.png]] Redis\n"

ENTS = [
    {"name": "Redis", "name_norm": "redis", "type": "tool", "doc_ids": [1], "degree": 3},
    {"name": "Kal", "name_norm": "kal", "type": "project", "doc_ids": [1], "degree": 5},
    {"name": "KAL_CLOUD_TOKEN", "name_norm": "kal_cloud_token", "type": "config", "doc_ids": [3], "degree": 2},
    {"name": "Payment Module", "name_norm": "payment module", "type": "concept", "doc_ids": [], "degree": 1},
    {"name": "Rare", "name_norm": "rare", "type": "tool", "doc_ids": [], "degree": 1},
]
DOCS = {"a.md": (1, False), "fm.md": (2, False), "secret.md": (4, True)}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.vault, self.home = os.path.join(self.tmp, "vault"), os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.vault, "img"))
        os.makedirs(self.home)
        gps_jpeg(os.path.join(self.vault, "shot.jpg"))
        png(os.path.join(self.vault, "img", "inline.png"), "green")
        png(os.path.join(self.vault, "board.png"), "white")
        png(os.path.join(self.vault, "ocr.png"), "black")
        png(os.path.join(self.vault, "secret.png"), "red")
        w = lambda n, t: open(os.path.join(self.vault, n), "w", encoding="utf-8").write(t)
        w("a.md", NOTE)
        w("fm.md", NOTE_FM)
        w("secret.md", NO_LLM_NOTE)
        w("board.png.md", "---\nentities: [Rare, Unknown Thing]\n---\n")
        w("ocr.png.json", json.dumps({"entities": []}))
        env = mock.patch.dict(os.environ, {"KAL_HOME": self.home, "KAL_VAULT": self.vault,
                                           "KAL_MEDIA_DIRS": "[]", "KAL_PATH": os.path.join(self.tmp, "nodb")})
        env.start()
        self.addCleanup(env.stop)
        for p in (mock.patch.object(media, "load_graph", lambda: (ENTS, DOCS)),
                  mock.patch.object(media, "ocr_text",
                                    lambda path: "fatal: KAL_CLOUD_TOKEN missing" if path.endswith("ocr.png") else "")):
            p.start()
            self.addCleanup(p.stop)

    def scan(self):
        m = media.scan()
        return m, {x["source"]: x for x in m["media"]}

    @staticmethod
    def links(m):
        return {l["entity"]: l for l in m["links"]}


class ScanTest(Base):
    def test_manifest_shape_and_links(self):
        m, by = self.scan()
        on_disk = json.load(open(os.path.join(self.home, "media", "manifest.json")))
        self.assertEqual(on_disk["version"], 1)
        self.assertRegex(on_disk["generated_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        for x in on_disk["media"]:
            self.assertEqual(set(x), {"sha256", "kind", "mime", "bytes", "width", "height", "duration_s",
                                      "taken_at", "source", "variants", "ocr", "links", "docs"})
            self.assertRegex(x["sha256"], r"^[0-9a-f]{64}$")
            self.assertIn("orig", x["variants"])
            for l in x["links"]:
                self.assertEqual(set(l), {"entity", "name", "type", "basis", "score", "doc"})

        shot = by["vault:a.md#shot.jpg"]
        self.assertEqual(shot["taken_at"], "2026-03-04T10:11:12")
        self.assertEqual(shot["docs"], ["a.md"])
        # same section as the embed → Redis; Kal is named only in the next section → no link
        self.assertEqual(self.links(shot).keys(), {"redis"})
        self.assertEqual(self.links(shot)["redis"]["basis"], "embed")
        self.assertEqual(self.links(shot)["redis"]["score"], 0.9)

        inline = by["vault:a.md#img/inline.png"]       # relative-path embed; its section names Kal only
        self.assertEqual(self.links(inline).keys(), {"kal"})
        self.assertEqual(len(on_disk["media"]), 3)     # shot, inline, board —— not secret, not the URL, not unembedded ocr.png

    def test_manual_links(self):
        _, by = self.scan()
        board = by["vault:fm.md#board.png"]
        ln = self.links(board)
        # sidecar entities + note frontmatter entities, all manual 1.0; unknown names kept with type ""
        self.assertEqual(ln.keys(), {"rare", "unknown thing", "payment module"})
        self.assertTrue(all(l["basis"] == "manual" and l["score"] == 1.0 for l in ln.values()))
        self.assertEqual(ln["unknown thing"]["type"], "")
        self.assertEqual(ln["rare"]["type"], "tool")

    def test_ocr_link_and_threshold(self):
        # ocr.png is not embedded anywhere; reach it through a media dir
        with mock.patch.dict(os.environ, {"KAL_MEDIA_DIRS": json.dumps([{"path": self.vault, "alias": "v"}])}):
            _, by = self.scan()
        ocr = by["dir:v/ocr.png"]
        self.assertEqual(self.links(ocr)["kal_cloud_token"]["basis"], "ocr")
        self.assertEqual(self.links(ocr)["kal_cloud_token"]["score"], 0.7)
        self.assertEqual(self.links(ocr).keys(), {"kal_cloud_token"})

    def test_ocr_pool_min_length_and_degree(self):
        ents = media.Entities(ENTS + [{"name": "abc", "name_norm": "abc", "type": "", "doc_ids": [], "degree": 9}])
        self.assertEqual({r["name"] for r in ents.ocr_pool}, {"Redis", "KAL_CLOUD_TOKEN"})

    def test_no_llm_excluded(self):
        m, by = self.scan()
        self.assertFalse([x for x in m["media"] if "secret" in x["source"]])
        self.assertNotIn("secret.md", json.dumps(m))
        # a media dir that happens to hold the same file keeps it, but never records the blocked note
        with mock.patch.dict(os.environ, {"KAL_MEDIA_DIRS": json.dumps([{"path": self.vault, "alias": "v"}])}):
            m, _ = self.scan()
        sec = [x for x in m["media"] if x["source"] == "dir:v/secret.png"]
        self.assertEqual(len(sec), 1)
        self.assertEqual(sec[0]["docs"], [])
        self.assertEqual(sec[0]["links"], [])

    def test_private_dir_skipped(self):
        d = os.path.join(self.tmp, "priv")
        os.makedirs(d)
        png(os.path.join(d, "p.png"), "yellow")
        with mock.patch.dict(os.environ, {"KAL_MEDIA_DIRS": json.dumps([{"path": d, "alias": "p", "private": True}])}):
            m, _ = self.scan()
        self.assertFalse([x for x in m["media"] if x["source"].startswith("dir:p")])

    def test_exif_stripped_and_view_size(self):
        m, by = self.scan()
        shot = by["vault:a.md#shot.jpg"]
        d = os.path.join(self.home, "media", shot["sha256"])
        orig = Image.open(os.path.join(d, "orig.jpg"))
        self.assertTrue(orig.getexif().get_ifd(0x8825), "fixture lost its GPS —— the test proves nothing")
        for f in ("view.jpg", "thumb.webp"):
            im = Image.open(os.path.join(d, f))
            self.assertFalse(im.getexif().get_ifd(0x8825), f"{f} kept GPS")
            self.assertEqual(len(im.getexif()), 0, f"{f} kept EXIF")
            with open(os.path.join(d, f), "rb") as fh:
                self.assertNotIn(b"Exif", fh.read(), f)
        self.assertEqual(max(Image.open(os.path.join(d, "view.jpg")).size), 2048)
        self.assertEqual(max(Image.open(os.path.join(d, "thumb.webp")).size), 256)
        self.assertEqual((shot["width"], shot["height"]), (3000, 2000))
        self.assertEqual(shot["variants"], ["thumb", "view", "orig"])

    def test_idempotent(self):
        m1, _ = self.scan()
        d = os.path.join(self.home, "media", m1["media"][0]["sha256"])
        before = {f: os.stat(os.path.join(d, f)).st_mtime_ns for f in os.listdir(d)}
        with mock.patch.object(media, "derive_image", side_effect=AssertionError("re-derived")):
            m2, _ = self.scan()
        self.assertEqual(before, {f: os.stat(os.path.join(d, f)).st_mtime_ns for f in os.listdir(d)})
        strip = lambda m: [{k: v for k, v in x.items()} for x in m["media"]]
        self.assertEqual(strip(m1), strip(m2))


class PushTest(Base):
    def test_push_uploads_missing_variants_once(self):
        import http.server
        import threading
        seen, have = [], set()

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_HEAD(self):
                seen.append(("HEAD", self.path, self.headers["Authorization"]))
                self.send_response(200 if self.path in have else 404)
                self.end_headers()

            def do_PUT(self):
                n = int(self.headers["Content-Length"])
                self.rfile.read(n)
                seen.append(("PUT", self.path, self.headers["X-Kal-Content-Type"] or self.headers["Content-Type"], self.headers["Content-Type"]))
                have.add(self.path)
                self.send_response(204)
                self.end_headers()

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.scan()
        with mock.patch.dict(os.environ, {"KAL_CLOUD_URL": f"http://127.0.0.1:{srv.server_port}", "KAL_CLOUD_TOKEN": "fixture"}):
            media.push()
            puts1 = [x for x in seen if x[0] == "PUT"]
            media.push()
        puts2 = [x for x in seen if x[0] == "PUT"]
        blobs = [x for x in puts1 if "/api/media/blob/" in x[1]]
        self.assertEqual(len(blobs), 9)                              # 3 media x thumb/view/orig
        self.assertEqual({x[2] for x in blobs if x[1].endswith("/thumb")}, {"image/webp"})
        self.assertEqual({x[2] for x in blobs if x[1].endswith("/view")}, {"image/jpeg"})
        self.assertTrue(puts1[-1][1].endswith("/api/media/manifest") and puts1[-1][2] == "application/json")
        #  The server's guard only takes octet-stream bodies for blobs; the real type rides in a header.
        self.assertEqual({x[3] for x in blobs}, {"application/octet-stream"})
        self.assertEqual(len(puts2) - len(puts1), 1)                 # second push: only the manifest
        self.assertTrue(all(x[2] == "Bearer fixture" for x in seen if x[0] == "HEAD"))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe not installed")
class VideoTest(Base):
    def test_video(self):
        v = os.path.join(self.vault, "clip.mov")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
                        "-t", "2", "-pix_fmt", "yuv420p", v], check=True)
        with open(os.path.join(self.vault, "a.md"), "a") as fh:
            fh.write("\n![[clip.mov]]\n")
        m, by = self.scan()
        clip = by["vault:a.md#clip.mov"]
        self.assertEqual(clip["kind"], "video")
        self.assertAlmostEqual(clip["duration_s"], 2.0, delta=0.3)
        self.assertEqual((clip["width"], clip["height"]), (320, 240))
        self.assertEqual(clip["mime"], "video/quicktime")
        self.assertEqual(clip["variants"], ["thumb", "view", "orig"])
        d = os.path.join(self.home, "media", clip["sha256"])
        self.assertEqual(max(Image.open(os.path.join(d, "thumb.webp")).size), 256)
        self.assertTrue(os.path.getsize(os.path.join(d, "view.mp4")) > 0)


if __name__ == "__main__":
    unittest.main()
