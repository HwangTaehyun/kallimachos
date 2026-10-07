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

    def test_ocr_skips_ordinary_words(self):
        # "index" is an entity from 2 notes but the word is in 20 → an ordinary word, no OCR link.
        # "LanceDB" is in 3 notes and was extracted from 2 → a name, linked.  (measured on a real graph, 2026-10-06)
        rows = [{"name": "index", "name_norm": "index", "type": "", "doc_ids": [1, 2], "degree": 9},
                {"name": "LanceDB", "name_norm": "lancedb", "type": "tool", "doc_ids": [1, 2], "degree": 9}]
        texts = ["the index page"] * 20 + ["lancedb store"] * 3
        calls = []
        ents = media.Entities(rows, texts=lambda: calls.append(1) or texts)
        self.assertTrue(ents.generic(rows[0]))
        self.assertFalse(ents.generic(rows[1]))
        self.assertEqual(len(calls), 1, "the corpus is read once, not per entity")
        self.assertFalse(media.Entities(rows).generic(rows[0]), "no texts → no check (manual-only setups)")

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

    def test_private_subfolder_of_a_public_dir(self):
        # public ~/pics, private ~/pics/secret: the walk of the public dir must not descend into the private one,
        # and no embed / frontmatter `media:` may reach into it either
        pub, priv = os.path.join(self.vault, "pics"), os.path.join(self.vault, "pics", "secret")
        os.makedirs(priv)
        png(os.path.join(pub, "pub.png"), "purple")
        hidden = os.path.join(priv, "hidden.png")
        png(hidden, "orange")
        with open(os.path.join(self.vault, "a.md"), "a", encoding="utf-8") as fh:
            fh.write("\n![[hidden.png]]\n![h](pics/secret/hidden.png)\n")
        with open(os.path.join(self.vault, "fm2.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nmedia: [hidden.png]\nentities: [Rare]\n---\n")
        dirs = [{"path": pub, "alias": "pics"}, {"path": priv, "alias": "s", "private": True}]
        with mock.patch.dict(os.environ, {"KAL_MEDIA_DIRS": json.dumps(dirs)}):
            m, by = self.scan()
        self.assertIn("dir:pics/pub.png", by, "the public parent is still walked")
        self.assertNotIn(media.sha256_of(hidden), {x["sha256"] for x in m["media"]})
        self.assertNotIn("hidden.png", json.dumps(m))

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


class Fake:
    """A media API on 127.0.0.1: HEAD / PUT blob, PUT manifest, GET usage, DELETE blob.  `fail` maps a path suffix
    to the status its PUT answers with; `usage` is what the server holds ({key: bytes}).  Like the real one it
    enforces the quota: a blob PUT that would take usage over `quota` answers 507; PUT / DELETE update `usage`."""

    def __init__(self, test):
        import http.server
        import threading
        self.seen, self.have, self.fail, self.usage, self.manifest = [], set(), {}, {}, None
        self.usage_status, self.quota = 200, 1 << 34
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, body=b""):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_HEAD(self):
                fake.seen.append(("HEAD", self.path, self.headers["Authorization"]))
                self.send_response(200 if self.path in fake.have else 404)
                self.end_headers()

            def do_PUT(self):
                n = int(self.headers["Content-Length"])
                data = self.rfile.read(n)
                fake.seen.append(("PUT", self.path, self.headers["X-Kal-Content-Type"] or self.headers["Content-Type"],
                                  self.headers["Content-Type"]))
                code = next((c for k, c in fake.fail.items() if self.path.endswith(k)), 204)
                key = self.path.split("/api/media/blob/")[-1]
                if code == 204 and "/api/media/blob/" in self.path:
                    if sum(fake.usage.values()) - fake.usage.get(key, 0) + n > fake.quota:
                        code = 507
                    else:
                        fake.usage[key] = n
                if code == 204:
                    fake.have.add(self.path)
                    if self.path.endswith("/manifest"):
                        fake.manifest = json.loads(data)
                self.reply(code, b'{"error":"refused"}' if code != 204 else b"")

            def do_GET(self):
                fake.seen.append(("GET", self.path))
                if fake.usage_status != 200:
                    return self.reply(fake.usage_status)
                body = json.dumps({"bytes": fake.usage, "total": sum(fake.usage.values()), "quota": fake.quota})
                self.reply(200, body.encode())

            def do_DELETE(self):
                fake.seen.append(("DELETE", self.path))
                fake.have.discard(self.path)
                fake.usage.pop(self.path.split("/api/media/blob/")[-1], None)
                self.reply(200, b'{"ok":true,"deleted":true}')

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        test.addCleanup(self.srv.shutdown)
        env = mock.patch.dict(os.environ, {"KAL_CLOUD_URL": f"http://127.0.0.1:{self.srv.server_port}",
                                           "KAL_CLOUD_TOKEN": "fixture", "KAL_MEDIA_ORIG": "1"})
        env.start()
        test.addCleanup(env.stop)

    def blobs(self, method="PUT"):
        return [x for x in self.seen if x[0] == method and "/api/media/blob/" in x[1]]


class PushTest(Base):
    def setUp(self):
        super().setUp()
        self.m, self.by = self.scan()
        self.fake = Fake(self)

    def push(self, *a, **kw):
        with mock.patch("sys.stderr", new=__import__("io").StringIO()) as err:
            media.push(*a, **kw)
        return err.getvalue()

    def test_push_uploads_missing_variants_once(self):
        f = self.fake
        self.push()
        puts1 = [x for x in f.seen if x[0] == "PUT"]
        self.push()
        puts2 = [x for x in f.seen if x[0] == "PUT"]
        blobs = [x for x in puts1 if "/api/media/blob/" in x[1]]
        self.assertEqual(len(blobs), 9)                              # 3 media x thumb/view/orig
        self.assertEqual({x[2] for x in blobs if x[1].endswith("/thumb")}, {"image/webp"})
        self.assertEqual({x[2] for x in blobs if x[1].endswith("/view")}, {"image/jpeg"})
        self.assertTrue(puts1[-1][1].endswith("/api/media/manifest") and puts1[-1][2] == "application/json")
        #  The server's guard only takes octet-stream bodies for blobs; the real type rides in a header.
        self.assertEqual({x[3] for x in blobs}, {"application/octet-stream"})
        self.assertEqual(len(puts2) - len(puts1), 1)                 # second push: only the manifest
        self.assertTrue(all(x[2] == "Bearer fixture" for x in f.seen if x[0] == "HEAD"))

    def test_caps_skip_variant(self):
        # thumb over its cap: skipped, the item stays.  orig over its cap too: the view still carries the item.
        with mock.patch.dict(media.CAP, {"thumb": 10, "orig": 10}):
            err = self.push()
        self.assertIn("over the server's", err)
        self.assertEqual({x[1].rsplit("/", 1)[1] for x in self.fake.blobs()}, {"view"})
        self.assertEqual(len(self.fake.manifest["media"]), 3)
        self.assertEqual({tuple(x["variants"]) for x in self.fake.manifest["media"]}, {("view",)})
        # neither view nor orig fits → the item is left out
        with mock.patch.dict(media.CAP, {"view": 10, "orig": 10}):
            self.push()
        self.assertEqual(self.fake.manifest["media"], [])

    def test_refused_file_does_not_stop_the_run(self):
        shot = self.by["vault:a.md#shot.jpg"]["sha256"]
        self.fake.fail = {f"{shot}/view": 413, f"{shot}/orig": 415}
        err = self.push()
        self.assertIn("413", err)
        got = {x["sha256"]: x for x in self.fake.manifest["media"]}
        self.assertNotIn(shot, got, "an item left with only a thumb must be dropped")
        self.assertEqual(len(got), 2, "the other files still went up and the manifest was still sent")
        # one refused variant of an item that keeps another viewable one: only that variant drops
        self.fake.fail = {f"{shot}/orig": 400}
        self.push()
        got = {x["sha256"]: x for x in self.fake.manifest["media"]}
        self.assertEqual(got[shot]["variants"], ["thumb", "view"])

    def test_hard_errors_still_abort(self):
        self.fake.fail = {"/thumb": 402}
        with self.assertRaises(SystemExit):
            self.push()
        self.assertIsNone(self.fake.manifest)

    def test_manifest_size_checked_before_any_blob(self):
        with mock.patch.object(media, "MANIFEST_CAP", 100):
            with self.assertRaises(SystemExit) as cm:
                self.push()
        self.assertIn("Nothing was uploaded", str(cm.exception))
        self.assertRegex(str(cm.exception), r"cut about \d+ of 3 items")
        self.assertEqual([x for x in self.fake.seen if x[0] in ("PUT", "HEAD")], [])

    def test_orig_opt_out(self):
        with mock.patch.dict(os.environ, {"KAL_MEDIA_ORIG": "0"}):
            self.push()
        self.assertFalse([x for x in self.fake.blobs() if x[1].endswith("/orig")])
        self.assertTrue(all("orig" not in x["variants"] for x in self.fake.manifest["media"]))
        cfg = os.path.join(self.home, "config.json")
        with open(cfg, "w") as fh:
            json.dump({"media_orig": False}, fh)
        import kal_config
        os.environ.pop("KAL_MEDIA_ORIG")                      # the config key alone also turns it off
        with mock.patch.object(kal_config, "CONFIG_PATH", cfg):
            self.assertFalse(media.want_orig())

    def test_multi_device_warning_and_prune(self):
        other = "f" * 64
        shot = self.by["vault:a.md#shot.jpg"]["sha256"]
        thumb = os.path.getsize(os.path.join(self.home, "media", shot, "thumb.webp"))
        self.fake.usage = {f"{other}/thumb": 1000, f"{other}/orig": 5_000_000, f"{shot}/thumb": thumb}
        err = self.push()
        self.assertIn("1 item(s) this device does not list", err)
        self.assertEqual(self.fake.blobs("DELETE"), [], "nothing is ever deleted without --prune")
        with mock.patch.dict(os.environ, {"KAL_MEDIA_ORIG": "0"}):       # the shot's orig is now unlisted too
            self.fake.usage[f"{shot}/orig"] = 7
            out = __import__("io").StringIO()
            with mock.patch("sys.stdout", new=out):
                self.push(prune=True)
        deleted = {x[1].split("/api/media/blob/")[1] for x in self.fake.blobs("DELETE")}
        # the first push recorded every original (the fake keeps what it stores) → all of them are now unlisted
        origs = {f"{x['sha256']}/orig" for x in self.m["media"]}
        self.assertIn(f"{shot}/orig", origs)
        self.assertEqual(deleted, {f"{other}/thumb", f"{other}/orig"} | origs)
        self.assertIn("deleted 5 blob(s)", out.getvalue())
        self.assertIn("media_orig is off —— 4 original(s) uploaded earlier will be deleted", out.getvalue())
        put_manifest = max(i for i, x in enumerate(self.fake.seen) if x[0] == "PUT" and x[1].endswith("/manifest"))
        first_delete = min(i for i, x in enumerate(self.fake.seen) if x[0] == "DELETE")
        self.assertLess(put_manifest, first_delete, "prune runs only after the manifest is in place")

    def test_main_parses_prune(self):
        with mock.patch.object(media, "push") as p:
            media.main(["media.py", "push", "--prune"])
            media.main(["media.py", "push"])
        self.assertEqual([c.kwargs for c in p.call_args_list], [{"prune": True}, {"prune": False}])
        with self.assertRaises(SystemExit):
            media.main(["media.py", "push", "--prnue"])

    def test_any_other_status_stops_the_run(self):
        self.fake.fail = {"/thumb": 410}         # 410 is not a per-file status: stop, never a partial manifest
        with self.assertRaises(SystemExit) as cm:
            self.push()
        self.assertIn("410", str(cm.exception))
        self.assertIsNone(self.fake.manifest)

    def test_old_server_without_usage(self):
        self.fake.usage_status = 404
        self.fake.usage = {"f" * 64 + "/thumb": 1}           # would warn, but the old server never says so
        err = self.push()
        self.assertNotIn("does not list", err)
        self.assertEqual(len(self.fake.manifest["media"]), 3, "no precheck: everything still goes up")
        self.fake.seen.clear()
        with self.assertRaises(SystemExit) as cm:
            self.push(prune=True)
        self.assertIn("nothing to prune", str(cm.exception))
        self.assertEqual([x for x in self.fake.seen if x[0] in ("PUT", "HEAD", "DELETE")], [])

    def test_quota_precheck_stops_before_any_blob(self):
        other = "f" * 64
        self.fake.usage, self.fake.quota = {f"{other}/orig": 5_000_000}, 5_000_100
        with self.assertRaises(SystemExit) as cm:
            self.push()
        msg = str(cm.exception)
        self.assertIn("MiB over the storage quota", msg)
        self.assertIn("--prune", msg)
        self.assertIn("media_orig", msg)
        self.assertEqual([x for x in self.fake.seen if x[0] in ("PUT", "HEAD", "DELETE")], [])
        # it fits once the other device's file is gone → --prune frees that room before the upload
        out = __import__("io").StringIO()
        with mock.patch("sys.stdout", new=out):
            self.push(prune=True)
        self.assertIn("pruning before the upload", out.getvalue())
        order = [x[1] for x in self.fake.seen if x[0] in ("PUT", "DELETE") and "/api/media/blob/" in x[1]]
        self.assertTrue(order[0].endswith(f"{other}/orig"), "the delete comes before the first blob")
        self.assertEqual(len(self.fake.manifest["media"]), 3)
        # what the server already records at the same size costs nothing: a full quota still passes
        self.fake.seen.clear()
        self.fake.usage = {x[1].split("/api/media/blob/")[1]: s for x, s in self.sizes().items()}
        self.fake.quota = sum(self.fake.usage.values())
        self.push()
        self.assertEqual(self.fake.blobs(), [])

    def sizes(self):
        """{("PUT", blob path): local size} for every variant this device holds."""
        out = {}
        for x in self.m["media"]:
            files = json.load(open(os.path.join(self.home, "media", x["sha256"], "meta.json")))["files"]
            for v, f in files.items():
                out[("PUT", f"/api/media/blob/{x['sha256']}/{v}")] = os.path.getsize(
                    os.path.join(self.home, "media", x["sha256"], f))
        return out

    def test_507_mid_run_stops(self):
        self.fake.fail = {"/view": 507}
        with self.assertRaises(SystemExit) as cm:
            self.push()
        self.assertIn("storage quota is full", str(cm.exception))
        self.assertIn("--prune", str(cm.exception))
        self.assertEqual(len([x for x in self.fake.blobs() if x[1].endswith("/view")]), 1, "stops at the first 507")
        self.assertIsNone(self.fake.manifest)

    def test_shrinking_copies_free_room_before_new_uploads(self):
        # near the quota: shot's view is recorded at an older, bigger derivation (shrinks when replaced) and the inline
        # image is new.  The end state fits exactly; uploading the new item before the delete would hit 507 mid-run.
        sizes = {x[1].split("/api/media/blob/")[1]: s for x, s in self.sizes().items()}
        shot, new = self.by["vault:a.md#shot.jpg"]["sha256"], self.by["vault:a.md#img/inline.png"]["sha256"]
        self.assertLess(self.m["media"].index(self.by["vault:a.md#img/inline.png"]),
                        self.m["media"].index(self.by["vault:a.md#shot.jpg"]), "fixture: the new item comes first")
        start = {k: s for k, s in sizes.items() if not k.startswith(new)}
        start[f"{shot}/view"] += 60_000
        self.fake.usage, self.fake.have = dict(start), {"/api/media/blob/" + k for k in start}
        self.fake.quota = sum(sizes.values()) - 1                     # one byte short of the end state → refused up front
        with self.assertRaises(SystemExit) as cm:
            self.push()
        self.assertIn("over the storage quota", str(cm.exception))
        self.assertEqual([x for x in self.fake.seen if x[0] in ("PUT", "DELETE")], [])
        self.fake.quota += 1
        self.push()
        self.assertEqual(self.fake.usage, sizes)
        self.assertEqual(len(self.fake.manifest["media"]), 3)
        order = [x[:2] for x in self.fake.seen if x[0] in ("PUT", "DELETE")]
        self.assertEqual(order[0], ("DELETE", f"/api/media/blob/{shot}/view"), "the delete frees room first")

    def test_one_image_in_51_notes(self):
        for i in range(51):
            with open(os.path.join(self.vault, f"n{i:02}.md"), "w", encoding="utf-8") as fh:
                fh.write("![[board.png]]\n")
        m, _ = self.scan()
        board = next(x for x in m["media"] if x["sha256"] == media.sha256_of(os.path.join(self.vault, "board.png")))
        self.assertEqual(board["docs"], sorted(["fm.md"] + [f"n{i:02}.md" for i in range(51)])[:50])
        self.assertEqual(media.manifest_errors(m), [])
        self.push()
        got = next(x for x in self.fake.manifest["media"] if x["sha256"] == board["sha256"])
        self.assertEqual(len(got["docs"]), 50)

    def test_long_note_path_is_dropped_not_cut(self):
        # 3 × 80 "€" (3 bytes each) = 720 bytes but only 245 characters: a character cut would have kept it whole
        deep = os.path.join(self.vault, *["€" * 80] * 3)
        os.makedirs(deep)
        with open(os.path.join(deep, "n.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nmedia: [board.png]\nentities: [Kal]\n---\n![[board.png]]\n")
        m, _ = self.scan()
        board = next(x for x in m["media"] if x["sha256"] == media.sha256_of(os.path.join(self.vault, "board.png")))
        self.assertEqual(board["docs"], ["fm.md"])
        self.assertEqual({l["entity"]: l["doc"] for l in board["links"]}["kal"], "", "the link stays, its doc does not")
        self.assertEqual(media.manifest_errors(m), [])

    def test_invalid_manifest_stops_before_any_request(self):
        mp = os.path.join(self.home, "media", "manifest.json")
        man = json.load(open(mp))
        man["media"][0]["docs"] = [f"n{i}.md" for i in range(51)]
        json.dump(man, open(mp, "w"))
        with self.assertRaises(SystemExit) as cm:
            self.push(prune=True)
        self.assertIn("51 docs", str(cm.exception))
        self.assertIn("nothing was sent", str(cm.exception))
        self.assertEqual(self.fake.seen, [], "not even the usage GET")

    def test_manifest_errors_mirrors_the_server(self):
        good = dict(self.m["media"][0], links=[{"entity": "kal", "name": "Kal", "type": "", "basis": "embed",
                                               "score": 0.9, "doc": "a.md"}])
        ok = lambda item, version=1: media.manifest_errors({"version": version, "media": [item]})
        self.assertEqual(ok(good), [])
        self.assertTrue(ok(good, version=2))
        for k, v in [("sha256", "A" * 64), ("kind", "audio"), ("links", None), ("variants", []), ("variants", ["raw"]),
                     ("mime", ""), ("mime", "a" * 101), ("bytes", -1), ("source", "s" * 513), ("ocr", "€" * 2001),
                     ("taken_at", "€" * 14), ("docs", ["d"] * 51), ("docs", ["€" * 171])]:
            with self.subTest(field=k, value=str(v)[:12]):
                self.assertTrue(ok(dict(good, **{k: v})))
        for k, v in [("entity", ""), ("name", ""), ("name", "n" * 121), ("entity", "€" * 171), ("doc", "€" * 171),
                     ("basis", "doc"), ("score", 1.5), ("score", None)]:
            with self.subTest(link=k, value=str(v)[:12]):
                self.assertTrue(ok(dict(good, links=[dict(good["links"][0], **{k: v})])))

    def test_rederived_copy_replaces_the_server_one(self):
        self.push()
        shot = self.by["vault:a.md#shot.jpg"]["sha256"]
        self.fake.usage = {x[1].split("/api/media/blob/")[1]: s for x, s in self.sizes().items()}
        self.fake.usage[f"{shot}/view"] += 1          # the server holds an older derivation
        self.fake.usage[f"{shot}/orig"] += 1          # orig is content-addressed: never replaced
        self.fake.seen.clear()
        self.push()
        self.assertEqual([x[1] for x in self.fake.blobs("DELETE")], [f"/api/media/blob/{shot}/view"])
        self.assertEqual([x[1] for x in self.fake.blobs()], [f"/api/media/blob/{shot}/view"])
        i = [x[:2] for x in self.fake.seen]
        self.assertLess(i.index(("DELETE", f"/api/media/blob/{shot}/view")),
                        [n for n, x in enumerate(self.fake.seen) if x[0] == "PUT"][0])


class ScanExtraTest(Base):
    def test_no_doc_basis(self):
        with mock.patch.dict(os.environ, {"KAL_MEDIA_DIRS": json.dumps([{"path": self.vault, "alias": "v"}])}):
            m, _ = self.scan()
        bases = {l["basis"] for x in m["media"] for l in x["links"]}
        self.assertTrue(bases and bases <= {"manual", "embed", "ocr"}, bases)

    def test_icc_kept_exif_stripped(self):
        from PIL import ImageCms
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        p = os.path.join(self.vault, "p3.jpg")
        ex = Image.Exif()
        ex.get_ifd(0x8825)[1] = "N"
        Image.new("RGB", (3000, 100), "teal").save(p, "JPEG", icc_profile=icc, exif=ex)
        with open(os.path.join(self.vault, "a.md"), "a") as fh:
            fh.write("\n![[p3.jpg]]\n")
        _, by = self.scan()
        d = os.path.join(self.home, "media", by["vault:a.md#p3.jpg"]["sha256"])
        for f in ("view.jpg", "thumb.webp"):
            im = Image.open(os.path.join(d, f))
            self.assertEqual(im.info.get("icc_profile"), icc, f"{f} lost its colour profile")
            self.assertEqual(len(im.getexif()), 0, f"{f} kept EXIF")

    def test_stale_meta_is_rederived(self):
        m, _ = self.scan()
        mj = os.path.join(self.home, "media", m["media"][0]["sha256"], "meta.json")
        meta = json.load(open(mj))
        self.assertEqual(meta["derive_version"], media.DERIVE_VERSION)
        del meta["derive_version"]                    # a copy from before DERIVE_VERSION existed
        json.dump(meta, open(mj, "w"))
        with mock.patch.object(media, "derive_image", wraps=media.derive_image) as d:
            self.scan()
        self.assertEqual(d.call_count, 1, "only the stale item is derived again")
        self.assertEqual(json.load(open(mj))["derive_version"], media.DERIVE_VERSION)

    def test_heic(self):
        try:
            import pillow_heif
            p = os.path.join(self.vault, "phone.heic")
            pillow_heif.from_pillow(Image.new("RGB", (64, 48), "orange")).save(p)
        except Exception as e:      # no HEIF encoder in this build
            self.skipTest(f"cannot write a HEIC fixture: {e}")
        with open(os.path.join(self.vault, "a.md"), "a") as fh:
            fh.write("\n![[phone.heic]]\n")
        _, by = self.scan()
        x = by["vault:a.md#phone.heic"]
        self.assertEqual((x["mime"], x["variants"]), ("image/heic", ["thumb", "view", "orig"]))


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

    def test_tall_h264_is_reencoded(self):
        # an H.264 mp4 over 720p used to be re-muxed as is (4K stayed 4K); now its view copy is ≤720p
        v = os.path.join(self.vault, "tall.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=1280x960:rate=5",
                        "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", v], check=True)
        with open(os.path.join(self.vault, "a.md"), "a") as fh:
            fh.write("\n![[tall.mp4]]\n")
        _, by = self.scan()
        d = os.path.join(self.home, "media", by["vault:a.md#tall.mp4"]["sha256"])
        vs = media.probe_video(os.path.join(d, "view.mp4"))["streams"][0]
        self.assertEqual((vs["width"], vs["height"]), (960, 720))

    def clip(self, name, size, pix_fmt, codec="libx264"):
        v = os.path.join(self.vault, name)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=5",
                        "-t", "1", "-c:v", codec, "-pix_fmt", pix_fmt, v], check=True)
        with open(os.path.join(self.vault, "a.md"), "a") as fh:
            fh.write(f"\n![[{name}]]\n")
        return v

    def view_stream(self, name):
        _, by = self.scan()
        d = os.path.join(self.home, "media", by[f"vault:a.md#{name}"]["sha256"])
        return media.probe_video(os.path.join(d, "view.mp4"))["streams"][0]

    def test_odd_height_reencodes(self):
        # 640x479 used to fail: "height not divisible by 2"
        self.clip("odd.mov", "640x479", "yuv444p")
        vs = self.view_stream("odd.mov")
        self.assertEqual((vs["width"] % 2, vs["height"] % 2, vs["pix_fmt"]), (0, 0, "yuv420p"))
        self.assertEqual(vs["height"], 478)

    def test_yuv444_h264_is_reencoded(self):
        # ≤720p H.264 mp4, but 4:4:4 —— browsers cannot play it, so no fast re-mux
        self.clip("full.mp4", "320x240", "yuv444p")
        self.assertEqual(self.view_stream("full.mp4")["pix_fmt"], "yuv420p")

    def test_pcm_audio_is_reencoded(self):
        # ≤720p 8-bit H.264 mp4, but PCM audio —— browsers cannot play it, so no fast re-mux; audio → AAC
        v = os.path.join(self.vault, "pcm.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=5",
                        "-f", "lavfi", "-i", "sine=d=1", "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "pcm_s16le", v], check=True)
        with open(os.path.join(self.vault, "a.md"), "a") as fh:
            fh.write("\n![[pcm.mp4]]\n")
        _, by = self.scan()
        d = os.path.join(self.home, "media", by["vault:a.md#pcm.mp4"]["sha256"])
        streams = media.probe_video(os.path.join(d, "view.mp4"))["streams"]
        self.assertEqual([s["codec_name"] for s in streams if s["codec_type"] == "audio"], ["aac"])

    def test_remux_over_view_cap_is_reencoded(self):
        self.clip("small.mp4", "320x240", "yuv420p")
        cmds = []
        real = media.run
        with mock.patch.object(media, "run", lambda c, **kw: cmds.append(c) or real(c, **kw)), \
                mock.patch.dict(media.CAP, {"view": 10}):
            self.view_stream("small.mp4")
        enc = [c for c in cmds if c[0] == "ffmpeg" and "small.mp4" in c[c.index("-i") + 1]]
        self.assertIn("copy", enc[-2], "the re-mux was tried first")
        self.assertIn("libx264", enc[-1], "then re-encoded because the re-mux was over the cap")


if __name__ == "__main__":
    unittest.main()
