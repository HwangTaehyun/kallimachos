#!/usr/bin/env python3
"""Photos and videos linked to entities —— the device side.

    python src/media.py scan            collect → derive (thumb / view / OCR) → link → $KAL_HOME/media/manifest.json
    python src/media.py push [--prune]  upload what the cloud does not have yet, then the manifest

Sources
  (a) local media embedded in vault notes:  ![[file.png]]  and  ![alt](relative/path.png)
  (b) opt-in folders, config key `media_dirs` (env KAL_MEDIA_DIRS as a JSON list wins over config.json):
        [{"path": "~/Pictures/whiteboards", "alias": "wb", "private": false}]
      A folder with "private": true is skipped entirely, wherever it sits: nothing under it is read —— not by a
      public folder that contains it, not through a vault embed or a frontmatter `media:` entry.  Paths are compared
      by REAL path: a note, media file or sidecar symlinked into a private folder is not read either, and directory
      symlinks are never followed.

Linking (every link carries its basis; the same (media, entity) keeps only the strongest basis):
  manual 1.0   sidecar `<file>.md` / `<file>.json` with `entities: [...]`, or the note's frontmatter
               `media: [file, ...]` (those files get the note's `entities:` list)
  embed  0.9   the embed sits in a section (previous heading → next heading) that names an entity
               which was extracted from that very note
  ocr    0.7   an entity name (≥ 4 chars, degree ≥ 2) occurs in the picture's text, and the name is not an
               ordinary word: it appears in at most OCR_GENERIC× as many notes as the entity was extracted from
  Those three are the only bases.  The notes a file is embedded in go to the item's `docs` list, not to a link:
  sorted by path, at most MAX_DOCS (50, the server's limit).  A note path over 512 bytes is left out, never cut,
  and so is every link it would be the `doc` of; an item whose notes ALL have such paths is not listed (docs []
  would make it look like a media-dir item, which MCP shows ungated).
`no_llm` notes never contribute a reference, a link or a doc; a file referenced only by them is not listed.

The server never calls an LLM and never sees a file the device did not choose to upload: everything
interpretive happens here.

Privacy
  EXIF (incl. GPS) is stripped from the thumbnail and the JPEG view copy; their colour profile (ICC) is kept.
  A GIF's view copy is the file as it is, so whatever metadata the GIF carries is kept.
  The ORIGINAL is uploaded byte for byte —— its EXIF, including GPS, is kept.  To keep originals on the device,
  set `"media_orig": false` in config.json (or env KAL_MEDIA_ORIG=0): `orig` is then never uploaded nor listed.

Push
  Each variant over its server cap (thumb 1 MiB · view 300 MiB · orig 1 GiB) is skipped with a warning.
  Per-file statuses: 400 / 413 / 415 on a blob → that file is skipped with a warning and the run goes on.  An item
  left with neither view nor orig is dropped from the uploaded manifest.  ANY other non-2xx response (401, 402,
  409, 410, 411, 507, 5xx, …) and any network error stop the run.  (HEAD 404 only means "not there yet → upload", and
  a 404/405 from /api/media/usage means an older server: no quota precheck, no multi-device warning, no prune.)
  Before any request: the manifest is checked against every rule of the server's validateManifest, field types
  included (a violation stops the run with the list), and against its 16 MiB limit.  Before any blob: the peak usage of this push is
  checked against the storage quota (GET /api/media/usage).  A 507 mid-run (quota full) stops the run.
  thumb / view are derived copies: when the server records one at a size other than the local file (re-derived
  after DERIVE_VERSION changed), it is replaced: each stale copy is deleted right before its own re-upload, unless
  the usage peak of that order would go over the quota —— then (and push says so) all of them are deleted before
  the first upload, and if the run stops, those items have no preview until the next push.  orig is
  content-addressed and never replaced.
  The uploaded manifest REPLACES the server's (last push wins) —— one device per account.  When the server holds
  items this device does not list, push says so.  `push --prune` then deletes every recorded server file the
  uploaded manifest does not list; without the flag nothing is ever deleted.  Normally it runs after the manifest
  is in place; when the quota check needs the room it runs BEFORE the upload, and if the run then stops, the
  server's previous manifest can point at deleted files until the next successful push.
  A server that lost its records but kept the objects: push uploads and records this device's files again;
  objects this device no longer lists stay unrecorded (so not prunable) until the account is deleted.
"""
import contextlib
import datetime
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

import vault_path
from frontmatter import FM_RE

try:                            # HEIC / HEIF (iPhone photos); optional —— without it those files are skipped
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif"}
VID_EXT = {".mp4", ".mov", ".m4v", ".webm"}
MIME_FIX = {".heic": "image/heic", ".heif": "image/heif", ".m4v": "video/x-m4v", ".mov": "video/quicktime"}
THUMB_PX, VIEW_PX, OCR_CHARS = 256, 2048, 2000
#  The server's manifest limits (cloud/api/media.go validateManifest).  name · source · ocr count characters;
#  entity · doc · taken_at · mime count UTF-8 bytes.
CAPS = {"name": 120, "source": 512, "doc": 512, "entity": 512, "taken_at": 40, "mime": 100}
MAX_DOCS = 50                   # notes listed per item —— the server refuses more
BASIS_RANK = {"manual": 3, "embed": 2, "ocr": 1}
BASIS_SCORE = {"manual": 1.0, "embed": 0.9, "ocr": 0.7}
VARIANTS = ("thumb", "view", "orig")
CAP = {"thumb": 1 << 20, "view": 300 << 20, "orig": 1 << 30}     # the server's per-variant limits
MANIFEST_CAP = 16 << 20                                          # the server's manifest body limit
MAX_MEDIA = 50_000
DERIVE_VERSION = 2              # bump when thumb / view derivation changes: cached copies are re-derived, push replaces them


def home():
    return os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))


def media_dir():
    return os.path.join(home(), "media")


def warn(msg):
    print(f"  ⚠ {msg}", file=sys.stderr)


# ───────────────────────── config ─────────────────────────

def media_dirs():
    """env KAL_MEDIA_DIRS (JSON) > config.json `media_dirs` > []  —— the order every other setting follows."""
    raw = os.environ.get("KAL_MEDIA_DIRS")
    if raw:
        try:
            lst = json.loads(raw)
        except ValueError:
            warn("KAL_MEDIA_DIRS is not valid JSON —— ignored")
            lst = []
    else:
        import kal_config
        lst = kal_config.read_file()[0].get("media_dirs", [])
    out = []
    for d in lst if isinstance(lst, list) else []:
        if isinstance(d, dict) and d.get("path"):
            p = os.path.abspath(os.path.expanduser(str(d["path"])))
            out.append({"path": p, "alias": str(d.get("alias") or os.path.basename(p)),
                        "private": bool(d.get("private"))})
    return out


def want_orig():
    """env KAL_MEDIA_ORIG ("0" = off) > config.json `media_orig` > on.  Off → originals stay on the device."""
    raw = os.environ.get("KAL_MEDIA_ORIG")
    if raw is not None:
        return raw.strip() != "0"
    import kal_config
    return kal_config.read_file()[0].get("media_orig", True) not in (False, 0, "0")


# ───────────────────────── graph (read only) ─────────────────────────

def load_graph():
    """→ (entities, docs).  entities: list of dicts; docs: {rel path: (doc_id, no_llm)}.  No index → both empty."""
    db = os.environ.get("KAL_PATH", os.path.join(home(), "db"))
    if not os.path.isdir(db):
        warn(f"no index at {db} —— entity links (embed / ocr) need one; manual links still work")
        return [], {}
    import lancedb
    con = lancedb.connect(db)
    ents = con.open_table("lr_entities").to_arrow().to_pylist()
    docs = {d["path"]: (d["doc_id"], bool(d.get("no_llm")))
            for d in con.open_table("documents").to_arrow().to_pylist()}
    return ents, docs


def load_texts():
    """→ lowercased text of every indexed document (for the OCR word-frequency check).  No index → []."""
    db = os.environ.get("KAL_PATH", os.path.join(home(), "db"))
    if not os.path.isdir(db):
        return []
    import lancedb
    by = {}
    for c in lancedb.connect(db).open_table("chunks").to_arrow().select(["doc_id", "text"]).to_pylist():
        by[c["doc_id"]] = by.get(c["doc_id"], "") + " " + (c["text"] or "").lower()
    return list(by.values())


#  A name that occurs in many more notes than it was extracted from is an ordinary word that happens to be an
#  entity.  Measured on a real graph (1,657 notes, 2026-10-06): index · notes · rebuild · schema · search sit at
#  10–54×, real names (LanceDB · kallimachos · ffmpeg · Obsidian · MinIO) at ≤ 4×.  Without it a whiteboard
#  reading "LanceDB index rebuild notes" was linked to "index", "notes" and "rebuild".
OCR_GENERIC = 5


class Entities:
    """Name lookups over the entity rows.  Keys are the table's `name_norm` (what the server graph shares)."""

    def __init__(self, rows, docs=None, texts=None):
        self.docs = docs or {}
        self._texts, self._generic = texts, {}       # texts: a loader, called once on the first OCR hit
        from schema_v3 import norm_name
        self.norm_name = norm_name
        self.rows = [r for r in rows if r.get("name")]
        self.by_key = {}
        for r in self.rows:
            for k in (r.get("name_norm"), norm_name(r["name"]).lower(), r["name"].lower()):
                if k:
                    self.by_key.setdefault(k, r)
        self.by_doc = {}
        for r in self.rows:
            for d in r.get("doc_ids") or []:
                self.by_doc.setdefault(d, []).append(r)
        self.ocr_pool = [r for r in self.rows if len(r["name"]) >= 4 and (r.get("degree") or 0) >= 2]

    def generic(self, r):
        """Is this entity's name an ordinary word in this corpus (see OCR_GENERIC)?  No texts → no check."""
        k = r["name"].lower()
        if k not in self._generic:
            if callable(self._texts):
                self._texts = self._texts()
            df = sum(1 for t in self._texts or [] if occurs(r["name"], t))
            self._generic[k] = df > OCR_GENERIC * max(len(r.get("doc_ids") or []), 1)
        return self._generic[k]

    def lookup(self, name):
        """A listed name → (key, display name, type).  Unknown names are kept (the server graph may have them)."""
        k = self.norm_name(name).lower()
        r = self.by_key.get(k)
        if r:
            return r.get("name_norm") or k, r["name"], r.get("type") or ""
        return k, self.norm_name(name), ""


def occurs(name, text_lower):
    """Case-insensitive occurrence; plain ASCII names must not match inside a longer word."""
    n = name.lower()
    if n not in text_lower:
        return False
    if re.fullmatch(r"[a-z0-9_ .\-]+", n):
        return re.search(r"(?<![a-z0-9_])" + re.escape(n) + r"(?![a-z0-9_])", text_lower) is not None
    return True


# ───────────────────────── collecting ─────────────────────────

EMBED_WIKI = re.compile(r"!\[\[([^\]|#]+?)(?:[#|][^\]]*)?\]\]")
EMBED_MD = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
HEADING = re.compile(r"^#{1,6}[ \t]+\S", re.M)


def is_media(path):
    return os.path.splitext(path)[1].lower() in IMG_EXT | VID_EXT


def walk(root, skip=()):
    """Every non-hidden file under root.  A folder whose REAL path is a folder in `skip` (the private media dirs) or
    inside one is not entered at all.  Directory symlinks are never followed (os.walk's default)."""
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if not d.startswith(".") and not under(os.path.join(dp, d), skip)]
        for f in fn:
            if not f.startswith("."):
                yield os.path.join(dp, f)


def inside(path, root):
    root = os.path.realpath(root)
    return os.path.realpath(path).startswith(root + os.sep)


def under(path, roots):
    """Is the REAL path of `path` one of `roots` (compared by real path too) or inside one?  A symlink is judged by
    where it points; "/pics/secret2" is not under "/pics/secret"."""
    rp = os.path.realpath(path)
    return any(rp == os.path.realpath(r) or inside(rp, r) for r in roots)


def section_of(text, pos):
    """The section holding `pos`: from the previous heading line to the next heading line of any level."""
    start, end = 0, len(text)
    for m in HEADING.finditer(text):
        if m.start() <= pos:
            start = m.start()
        else:
            end = m.start()
            break
    return text[start:end]


def parse_fm(text):
    m = FM_RE.match(text)
    if not m:
        return {}
    try:
        import yaml
        d = yaml.safe_load(m.group(1))
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def as_list(v):
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v if isinstance(x, (str, int))] if isinstance(v, list) else []


def collect(vault, dirs, docs):
    """→ {abs path: {"embeds": [...], "notes": [...], "dir": alias|None}}.

    embeds: (note rel path, section text, ref as written)   notes: (note rel path, [entity names])
    """
    from schema_v3 import doc_meta, _blocked_by_path
    found = {}
    private = [d["path"] for d in dirs if d["private"]]

    def is_private(p):          # inside ANY private dir by real path, however deep, whichever public dir holds it
        return under(p, private)

    by_name = {}
    all_files = list(walk(vault, private)) if vault and os.path.isdir(vault) else []
    for p in all_files:
        if is_media(p):
            by_name.setdefault(os.path.basename(p).lower(), []).append(p)

    def resolve(ref, note_abs):
        ref = urllib.parse.unquote(ref).strip()
        if not ref or re.match(r"[a-z][a-z0-9+.\-]*:", ref, re.I) or not is_media(ref):
            return None            # URLs, data:, non-media
        cands = [os.path.join(os.path.dirname(note_abs), ref), os.path.join(vault, ref)]
        for c in cands:            # a path, relative to the note or to the vault root
            if os.path.isfile(c) and inside(c, vault):
                return os.path.abspath(c)
        hits = by_name.get(os.path.basename(ref).lower(), [])   # Obsidian: by file name, anywhere
        if "/" in ref:
            hits = [h for h in hits if h.lower().endswith("/" + ref.lower())] or hits
        if not hits:
            return None
        here = os.path.dirname(note_abs)
        return sorted(hits, key=lambda h: (os.path.dirname(h) != here, len(h)))[0]

    for note in all_files:
        if not note.lower().endswith(".md") or is_private(note):
            continue               # a note symlinked into a private dir is not read
        rel = os.path.relpath(note, vault)
        try:
            with open(note, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        flag = docs.get(rel)
        if (flag and flag[1]) or doc_meta(text)[2] or _blocked_by_path(rel):
            continue               # no_llm: contributes nothing
        fm = parse_fm(text)
        for m in list(EMBED_WIKI.finditer(text)) + list(EMBED_MD.finditer(text)):
            p = resolve(m.group(1), note)
            if p and not is_private(p):
                found.setdefault(p, {"embeds": [], "notes": [], "dir": None})["embeds"].append(
                    (rel, section_of(text, m.start()), m.group(1)))
        for name in as_list(fm.get("media")):
            p = resolve(name, note)
            if p and not is_private(p):
                found.setdefault(p, {"embeds": [], "notes": [], "dir": None})["notes"].append(
                    (rel, as_list(fm.get("entities"))))

    for d in dirs:
        if d["private"]:
            continue
        if not os.path.isdir(d["path"]):
            warn(f"media dir {d['path']} does not exist")
            continue
        for p in walk(d["path"], private):            # a private subfolder of a public dir is pruned
            if is_media(p) and not is_private(p):
                rec = found.setdefault(os.path.abspath(p), {"embeds": [], "notes": [], "dir": None})
                rec["dir"] = rec["dir"] or (d["alias"], os.path.relpath(p, d["path"]))
    return found


def sidecar_entities(path, private=()):
    for ext in (".md", ".json"):
        sc = path + ext
        if not os.path.isfile(sc) or under(sc, private):     # a sidecar symlinked into a private dir is not read
            continue
        try:
            with open(sc, encoding="utf-8") as fh:
                raw = fh.read()
            d = json.loads(raw) if ext == ".json" else parse_fm(raw)
        except (OSError, ValueError):
            continue
        if isinstance(d, dict):
            return as_list(d.get("entities"))
    return []


# ───────────────────────── deriving ─────────────────────────

def have(tool):
    return shutil.which(tool) is not None


#  Seconds a tool may take on one file.  There was no limit: one pathological file hung the whole scan
#  (review 2026-10-09).  ffmpeg gets the most —— a long clip's re-encode is legitimately slow.
TIMEOUT = {"tesseract": 120, "ffprobe": 120, "ffmpeg": 1800}
RETRIES = 2                     # scans that retry a file whose derivation timed out
_WARNED = set()                 # one "tool missing" warning per scan, not one per file (cleared by scan())
_TIMED_OUT = []                 # set by run(); process() marks that file's cache for a retry on the next scan


def run(cmd, **kw):
    """A timed-out tool is that file's failure (returncode -1, no output), never a hung scan."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT.get(cmd[0], 1800), **kw)
    except subprocess.TimeoutExpired:
        #  the input file: ffmpeg's follows -i, tesseract's is first, ffprobe's is last
        src = cmd[cmd.index("-i") + 1] if "-i" in cmd else cmd[1] if cmd[0] == "tesseract" else cmd[-1]
        warn(f"{cmd[0]} gave up after {TIMEOUT.get(cmd[0], 1800)}s on {os.path.basename(src)}")
        _TIMED_OUT.append(cmd[0])
        return subprocess.CompletedProcess(cmd, -1, "", "")


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def exif_time(im):
    ex = im.getexif()
    raw = ex.get_ifd(0x8769).get(0x9003) or ex.get(0x9003) or ex.get(0x0132)
    try:
        return datetime.datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S").isoformat()
    except ValueError:
        return None


def flat_rgb(im):
    from PIL import Image
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im, mask=im.getchannel("A"))
        return bg
    return im.convert("RGB")


def write_thumb(im, out, icc=None):
    """256px longest side, WebP.  Saved without exif=, so no EXIF survives; the ICC profile is kept
    (a Display-P3 photo read as sRGB looks washed out)."""
    im = im.copy()
    im.thumbnail((THUMB_PX, THUMB_PX))
    im = im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im.convert("RGB")
    im.save(out, "WEBP", quality=80, icc_profile=icc)


def ocr_text(path):
    """First 2,000 chars of tesseract output; "" when tesseract is missing or finds nothing."""
    if not have("tesseract"):
        #  Used to be silent: a scan without tesseract produced 0 OCR links and said nothing (2026-10-09).
        if "tesseract" not in _WARNED:
            _WARNED.add("tesseract")
            warn("tesseract missing —— no OCR text, no ocr links (cached items are re-read once it is installed)")
        return ""
    for lang in ("kor+eng", "eng"):
        r = run(["tesseract", path, "-", "-l", lang])
        if r.returncode == 0:
            return re.sub(r"\s+", " ", r.stdout).strip()[:OCR_CHARS]
    return ""


def probe_video(path):
    if not have("ffprobe"):
        return {}
    r = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path])
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {}


def derive_image(path, out_dir, ext):
    from PIL import Image, ImageOps
    try:
        im = Image.open(path)
        im.load()
    except Exception as e:      # heic without a plugin, truncated files …
        warn(f"{os.path.basename(path)}: cannot open ({type(e).__name__}) —— skipped")
        return None
    meta = {"kind": "image", "taken_at": exif_time(im)}
    gif, icc = im.format == "GIF", im.info.get("icc_profile")
    im = ImageOps.exif_transpose(im)
    meta["width"], meta["height"], meta["duration_s"] = im.width, im.height, None
    write_thumb(im, os.path.join(out_dir, "thumb.webp"), icc)
    if gif:
        shutil.copyfile(path, os.path.join(out_dir, "view.gif"))       # animation kept as is
    else:
        v = flat_rgb(im)
        if max(v.size) > VIEW_PX:
            v.thumbnail((VIEW_PX, VIEW_PX))
        v.save(os.path.join(out_dir, "view.jpg"), "JPEG", quality=85, icc_profile=icc)
    #  OCR reads the derived JPEG when there is one: tesseract cannot open HEIC (rc 1, measured 2026-10-09), so
    #  every iPhone photo silently got no OCR links.  The view is also upright (EXIF rotation applied).
    #  Only for HEIC/HEIF —— the view is ≤2048 px, and a 5K screenshot scaled to 0.4× drops UI text below what
    #  tesseract reads (review round 2).  Every other format is read at full resolution as before.
    view = os.path.join(out_dir, "view.jpg")
    use_view = ext in (".heic", ".heif") and os.path.isfile(view)
    meta["ocr"] = ocr_text(view if use_view else path)
    return meta


def derive_video(path, out_dir):
    pr = probe_video(path)
    vs = next((s for s in pr.get("streams", []) if s.get("codec_type") == "video"), {})
    aus = [s for s in pr.get("streams", []) if s.get("codec_type") == "audio"]
    fmt = pr.get("format", {})
    meta = {"kind": "video", "width": vs.get("width"), "height": vs.get("height"), "ocr": "",
            "duration_s": round(float(fmt["duration"]), 2) if fmt.get("duration") else None,
            "taken_at": (fmt.get("tags", {}).get("creation_time") or "")[:19] or None}
    if not have("ffmpeg"):
        warn(f"{os.path.basename(path)}: ffmpeg missing —— original only")
        return meta
    from PIL import Image
    with tempfile.TemporaryDirectory() as td:
        poster = os.path.join(td, "poster.png")
        for ss in ("1", "0"):          # a clip shorter than 1s has no frame there
            if run(["ffmpeg", "-v", "error", "-y", "-ss", ss, "-i", path, "-frames:v", "1", poster]).returncode:
                with contextlib.suppress(OSError):
                    os.remove(poster)          # a timed-out run may leave a half-written frame
            if os.path.isfile(poster):
                break
        if os.path.isfile(poster):
            write_thumb(Image.open(poster), os.path.join(out_dir, "thumb.webp"))
            meta["ocr"] = ocr_text(poster)
        tmp = os.path.join(td, "view.mp4")
        # -map_metadata -1: location tags do not ride along.  A ≤720p 8-bit 4:2:0 H.264 mp4 whose audio (if any) is
        # AAC or MP3 is only re-muxed (fast); anything taller, 4:4:4 / 10-bit, PCM / Opus / other audio (browsers
        # cannot play those in mp4), or a re-mux over the server's view cap, is re-encoded to ≤720p H.264 + AAC ——
        # a 4K clip must not stay 4K.  10-bit / HDR goes to 8-bit without tone mapping: colours may look flat.
        ok = False
        if (vs.get("codec_name") == "h264" and vs.get("pix_fmt") == "yuv420p"
                and all(a.get("codec_name") in ("aac", "mp3") for a in aus)
                and os.path.splitext(path)[1].lower() in (".mp4", ".m4v") and (vs.get("height") or 1 << 30) <= 720):
            ok = (run(["ffmpeg", "-v", "error", "-y", "-i", path, "-c", "copy", "-map_metadata", "-1",
                       "-movflags", "+faststart", tmp]).returncode == 0
                  and os.path.isfile(tmp) and os.path.getsize(tmp) <= CAP["view"])
        if not ok:      # even height: yuv420p cannot encode 479 lines
            ok = run(["ffmpeg", "-v", "error", "-y", "-i", path, "-vf", "scale=-2:'2*trunc(min(720,ih)/2)'",
                      "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
                      "-c:a", "aac", "-map_metadata", "-1", "-movflags", "+faststart", tmp]).returncode == 0
        if ok and os.path.isfile(tmp):
            shutil.copyfile(tmp, os.path.join(out_dir, "view.mp4"))
        else:
            warn(f"{os.path.basename(path)}: no view copy (ffmpeg failed)")
    return meta


TOOLS = {"image": ("tesseract",), "video": ("ffprobe", "ffmpeg", "tesseract")}    # what a derivation can use


def process(path):
    """Derive (or reuse) the variants of one file → its meta dict (sha256, mime, bytes, variants, …) or None."""
    ext = os.path.splitext(path)[1].lower()
    sha = sha256_of(path)       # ponytail: re-hashes every file each run; cache by (path, size, mtime) if slow
    d = os.path.join(media_dir(), sha)
    mj = os.path.join(d, "meta.json")
    prev_retries = 0
    if os.path.isfile(mj):
        try:
            with open(mj, encoding="utf-8") as fh:
                meta = json.load(fh)
            prev_retries = int(meta.get("retries", 0))
            #  A file derived while a tool was missing (no OCR without tesseract, no view without ffmpeg) was cached
            #  as complete forever, even after the tool was installed (2026-10-09).  `tools` records what was there.
            if "tools" in meta:
                later = [t for t in TOOLS[meta.get("kind")] if t not in meta["tools"] and have(t)]
            else:
                #  Caches from before `tools` existed: assume the tools were there, except where the cache itself shows
                #  one missing (a video with no view copy).  Treating every old item as "missing everything" re-ran OCR on
                #  every image and re-encoded every video on the first scan after upgrading (review round 2).
                later = ["ffmpeg"] if (meta.get("kind") == "video" and "view" not in meta.get("files", {})
                                       and have("ffmpeg")) else []
            #  A timeout is not a verdict: the tool was present, so `tools` alone would cache the gap forever (round 2).
            if (meta.get("derive_version") == DERIVE_VERSION and not later and not meta.get("retry")
                    and all(os.path.isfile(os.path.join(d, f)) for f in meta["files"].values())):
                return meta
        except (OSError, ValueError, KeyError):
            pass
    shutil.rmtree(d, ignore_errors=True)        # no stale copy from an older derivation may be picked up below
    os.makedirs(d, exist_ok=True)
    _TIMED_OUT.clear()
    meta = derive_image(path, d, ext) if ext in IMG_EXT else derive_video(path, d)
    if meta is None:
        return None
    #  Retried on the next scan, at most RETRIES times: a clip that always needs more than the limit must not
    #  re-encode for 30 minutes on every scan forever (round 3).
    if _TIMED_OUT and prev_retries < RETRIES:
        meta["retry"], meta["retries"] = True, prev_retries + 1
    meta["tools"] = [t for t in TOOLS[meta["kind"]] if have(t)]
    shutil.copyfile(path, os.path.join(d, "orig" + ext))
    files = {}
    for v, names in (("thumb", ["thumb.webp"]), ("view", ["view.jpg", "view.gif", "view.mp4"]), ("orig", ["orig" + ext])):
        for n in names:
            if os.path.isfile(os.path.join(d, n)):
                files[v] = n
    meta.update(derive_version=DERIVE_VERSION, sha256=sha, bytes=os.path.getsize(path), files=files, variants=[v for v in VARIANTS if v in files],
                mime=MIME_FIX.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream")
    tmp = mj + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False)
    os.replace(tmp, mj)        # meta.json last: its presence means every variant is complete
    return meta


# ───────────────────────── linking ─────────────────────────

def build_links(rec, meta, path, ents, private=()):
    """→ (links keyed by entity key, docs).  The strongest basis per entity wins."""
    links, docs = {}, []

    def add(key, name, typ, basis, doc):
        #  A cut key would name another entity; a cut or blanked doc would name another note or none —— and a link
        #  with doc "" is ungated in MCP.  Either way the link is dropped.
        if nbytes(key) > CAPS["entity"] or nbytes(doc) > CAPS["doc"]:
            return
        old = links.get(key)
        if old is None or BASIS_RANK[basis] > BASIS_RANK[old["basis"]]:
            links[key] = {"entity": key, "name": name[:CAPS["name"]], "type": typ, "basis": basis,
                          "score": BASIS_SCORE[basis], "doc": doc}

    for name in sidecar_entities(path, private):
        k, nm, ty = ents.lookup(name)
        if k:
            add(k, nm, ty, "manual", "")
    for note, names in rec["notes"]:
        docs.append(note)
        for name in names:
            k, nm, ty = ents.lookup(name)
            if k:
                add(k, nm, ty, "manual", note)
    for note, section, _ref in rec["embeds"]:
        docs.append(note)
        #  Match against the prose only: the embed markup itself carries file names and paths, and on a real graph the
        #  extractor had made entities of exactly those ("storage-whiteboard.png", "raw/media/…/"), so every picture
        #  linked to its own file name (2026-10-08, real-vault run).
        low = EMBED_MD.sub(" ", EMBED_WIKI.sub(" ", section)).lower()
        did = ents.docs.get(note, (None, False))[0]
        for r in ents.by_doc.get(did, []) if did is not None else []:
            if occurs(r["name"], low):
                add(r.get("name_norm") or r["name"].lower(), r["name"], r.get("type") or "", "embed", note)
    text = (meta.get("ocr") or "").lower()
    if text:
        for r in ents.ocr_pool:
            if occurs(r["name"], text) and not ents.generic(r):
                add(r.get("name_norm") or r["name"].lower(), r["name"], r.get("type") or "", "ocr",
                    next((d for d in docs if nbytes(d) <= CAPS["doc"]), docs[0] if docs else ""))
    return links, list(dict.fromkeys(docs))


def nbytes(s):
    return len((s or "").encode("utf-8"))


def source_of(rec, path, vault):
    if rec["embeds"]:
        n, _s, ref = rec["embeds"][0]
        return f"vault:{n}#{ref}"
    if rec["notes"]:
        return f"vault:{rec['notes'][0][0]}#{os.path.basename(path)}"
    return f"dir:{rec['dir'][0]}/{rec['dir'][1]}"


def scan():
    _WARNED.clear()
    md = media_dir()
    if os.path.isdir(md) and not os.access(md, os.W_OK):
        #  The plugin mounts this folder read-only into its container; on Linux docker creates a missing bind-mount
        #  folder as root, and every derive below would then die on a PermissionError (2026-10-09).
        sys.exit(f"{md} is not writable (docker creates a missing mounted folder as root on Linux) —— "
                 f"sudo chown -R $(id -u):$(id -g) {md}")
    vault = vault_path.vault()
    ents_rows, docs = load_graph()
    ents = Entities(ents_rows, docs, load_texts)
    dirs = media_dirs()
    found = collect(vault, dirs, docs)
    private = [d["path"] for d in dirs if d["private"]]
    out, n_new = {}, 0
    for path in sorted(found):
        rec = found[path]
        meta = process(path)
        if meta is None:
            continue
        links, dcs = build_links(rec, meta, path, ents, private)
        m = out.get(meta["sha256"])
        if m is None:
            m = out[meta["sha256"]] = {
                "sha256": meta["sha256"], "kind": meta["kind"], "mime": meta["mime"], "bytes": meta["bytes"],
                "width": meta.get("width"), "height": meta.get("height"), "duration_s": meta.get("duration_s"),
                "taken_at": meta.get("taken_at") if nbytes(meta.get("taken_at")) <= CAPS["taken_at"] else None,
                "source": source_of(rec, path, vault)[:CAPS["source"]],
                "variants": meta["variants"], "ocr": (meta.get("ocr") or "")[:OCR_CHARS], "links": {}, "docs": [], "_dir": False}
        m["_dir"] = m["_dir"] or bool(rec.get("dir"))     # chosen through a public media folder
        for k, l in links.items():
            if k not in m["links"] or BASIS_RANK[l["basis"]] > BASIS_RANK[m["links"][k]["basis"]]:
                m["links"][k] = l
        m["docs"] = list(dict.fromkeys(m["docs"] + dcs))
    media = []
    for m in out.values():
        m["links"] = sorted(m["links"].values(), key=lambda l: (-l["score"], l["entity"]))
        docs = sorted(d for d in m["docs"] if nbytes(d) <= CAPS["doc"])     # a cut path would name another note
        if m["docs"] and not docs and not m["_dir"]:
            #  docs [] reads as "not from a note" → ungated in MCP, even after the note is blocked.  Leave it out —— unless
            #  the file also sits in a public media folder: that alone puts it in (its note links are already gone).
            #  Dropping those too hid a folder photo the moment one long-path note embedded it (adversarial review 3).
            warn(f"{m['source']}: every note it is in has a path over {CAPS['doc']} bytes —— left out")
            continue
        if len(docs) < len(m["docs"]):
            warn(f"{m['source']}: {len(m['docs']) - len(docs)} note path(s) over {CAPS['doc']} bytes —— not listed")
        if len(docs) > MAX_DOCS:
            warn(f"{m['source']}: in {len(docs)} notes —— only the first {MAX_DOCS} (by path) are listed")
        m["docs"] = docs[:MAX_DOCS]
        del m["_dir"]
        media.append(m)
    media.sort(key=lambda m: (m["taken_at"] or "", m["sha256"]))
    if len(media) > MAX_MEDIA:
        warn(f"{len(media)} media —— the server accepts {MAX_MEDIA}; the rest are left out")
        media = media[:MAX_MEDIA]
    manifest = {"version": 1, "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "media": media}
    os.makedirs(media_dir(), exist_ok=True)
    mp = os.path.join(media_dir(), "manifest.json")
    with open(mp + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False)
    os.replace(mp + ".tmp", mp)
    n_links = sum(len(m["links"]) for m in media)
    print(f"media: {len(media)} files · {n_links} links → {mp}")
    return manifest


# ───────────────────────── push ─────────────────────────

def variant_ct(m, v, fname):
    if v == "orig":
        return m["mime"]
    if v == "thumb":
        return "image/webp"
    return {".gif": "image/gif", ".mp4": "video/mp4"}.get(os.path.splitext(fname)[1], "image/jpeg")


def manifest_errors(manifest):
    """Every rule of the server's validateManifest (cloud/api/media.go) → a list of problems; [] = it will be accepted.
    Checked before any request, so blobs are never uploaded (or pruned) for a manifest the server then refuses."""
    errs = [] if type(manifest.get("version")) is int and manifest["version"] == 1 else ["version must be 1"]
    try:   # Python writes NaN/Infinity, which is not JSON —— the server would refuse the whole body (adversarial review 3)
        json.dumps(manifest, allow_nan=False)
    except ValueError:
        errs.append("a number is NaN or infinite —— not valid JSON")
    items = manifest.get("media")
    items = items if isinstance(items, list) else []
    if len(items) > MAX_MEDIA:
        errs.append(f"{len(items)} media —— the server takes {MAX_MEDIA}")
    for i, m in enumerate(items):
        #  Types first, as Go's json.Unmarshal into typed fields does (a wrong type refuses the whole manifest).
        #  null is what Go reads as the zero value, so it passes here too.  bool is not an int in JSON.
        if not isinstance(m, dict):
            errs.append(f"media[{i}]: not an object")
            continue
        links = m.get("links")
        #  Go reads bytes as int64: out of range refuses the whole manifest (adversarial review 3).
        bad = [f for f in ("bytes", "width", "height") if m.get(f) is not None and not (type(m[f]) is int and -(1 << 63) <= m[f] < (1 << 63))]
        bad += [f for f in ("duration_s",) if m.get(f) is not None and not _num(m[f])]
        bad += [f for f in ("sha256", "kind", "source", "ocr", "taken_at", "mime") if not isinstance(m.get(f), (str, type(None)))]
        bad += [f for f in ("variants", "docs") if not (m.get(f) is None or isinstance(m[f], list) and all(isinstance(x, str) for x in m[f]))]
        if not (links is None or isinstance(links, list) and all(isinstance(l, dict) for l in links)):
            bad.append("links")
        for l in links if "links" not in bad and links else []:
            bad += [f"links.{f}" for f in ("entity", "name", "basis", "doc") if not isinstance(l.get(f), (str, type(None)))]
        if bad:
            errs.append(f"media[{i}] {m.get('source', '') if isinstance(m.get('source'), str) else ''}: wrong type: "
                        + ", ".join(dict.fromkeys(bad)))
            continue
        docs, variants = m.get("docs") or [], m.get("variants")
        checks = [
            (re.fullmatch(r"[0-9a-f]{64}", str(m.get("sha256"))), "sha256 must be 64 lowercase hex"),
            (m.get("kind") in ("image", "video"), "kind must be image or video"),
            (isinstance(links, list), "links must be a list"),
            (isinstance(variants, list) and variants and set(variants) <= set(VARIANTS), "variants: thumb · view · orig, at least one"),
            (m.get("mime") and nbytes(m["mime"]) <= CAPS["mime"], f"mime is required, ≤ {CAPS['mime']} bytes"),
            (m.get("bytes") is None or m["bytes"] >= 0, "bytes is negative"),
            (len(m.get("source") or "") <= CAPS["source"], f"source over {CAPS['source']} characters"),
            (len(m.get("ocr") or "") <= OCR_CHARS, f"ocr over {OCR_CHARS} characters"),
            (nbytes(m.get("taken_at")) <= CAPS["taken_at"], f"taken_at over {CAPS['taken_at']} bytes"),
            (len(docs) <= MAX_DOCS, f"{len(docs)} docs —— the server takes {MAX_DOCS}"),
            (all(nbytes(d) <= CAPS["doc"] for d in docs), f"a docs entry over {CAPS['doc']} bytes"),
        ]
        for l in links if isinstance(links, list) else []:
            checks += [
                (l.get("entity") and l.get("name"), "a link without entity or name"),
                (len(l.get("name") or "") <= CAPS["name"], f"a link name over {CAPS['name']} characters"),
                (nbytes(l.get("entity")) <= CAPS["entity"] and nbytes(l.get("doc")) <= CAPS["doc"],
                 f"a link entity or doc over {CAPS['entity']} bytes"),
                (l.get("basis") in BASIS_RANK, "a link basis other than manual · embed · ocr"),
                (_num(l.get("score")) and 0 <= l["score"] <= 1, "a link score outside 0..1"),
            ]
        errs += [f"media[{i}] {m.get('source', '')}: {msg}" for ok, msg in checks if not ok]
    return errs


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


SOFT = (400, 413, 415)          # the server refused this one file —— skip it, the run goes on; anything else stops
KEY_RE = re.compile(r"[0-9a-f]{64}/(thumb|view|orig)")
FREE_SPACE = ("To make room: `push --prune` deletes server files this device no longer lists, and "
              "`\"media_orig\": false` in config.json (or KAL_MEDIA_ORIG=0) keeps originals on this device.")


def push(prune=False):
    url = (os.environ.get("KAL_CLOUD_URL") or "").rstrip("/")
    token = os.environ.get("KAL_CLOUD_TOKEN")
    if not url or not token:
        sys.exit("KAL_CLOUD_URL and KAL_CLOUD_TOKEN are required (same as `just push`)")
    mp = os.path.join(media_dir(), "manifest.json")
    if not os.path.isfile(mp):
        sys.exit("no manifest —— run `just media` first")
    with open(mp, encoding="utf-8") as fh:
        manifest = json.load(fh)
    auth = {"Authorization": f"Bearer {token}"}

    def call(method, u, data=None, ct=None, length=None, soft=()):
        """→ (status, body).  An HTTP code in `soft` is returned; any other failure stops the run."""
        h = dict(auth)
        if ct == "application/json":
            h["Content-Type"] = ct
        elif ct:
            #  The server's request guard only takes json · gzip · octet-stream bodies, so a blob goes
            #  as octet-stream and its real type rides in X-Kal-Content-Type (cloud/api/media.go).
            h["Content-Type"] = "application/octet-stream"
            h["X-Kal-Content-Type"] = ct
        if length is not None:
            h["Content-Length"] = str(length)   # a file object as `data` streams; urllib needs the length
        req = urllib.request.Request(u, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if method == "HEAD" and e.code == 404:
                return 404, b""
            detail = e.read(300).decode("utf-8", "replace") if method != "HEAD" else ""
            if e.code in soft:
                warn(f"{method} {u.replace(url, '')} → {e.code} {detail}".rstrip())
                return e.code, b""
            if e.code == 507:       # not this file's fault: nothing after it would fit either
                sys.exit("push stopped: the server's storage quota is full (507).  The manifest was not replaced.  "
                         + FREE_SPACE)
            sys.exit(f"push failed: {method} {u.replace(url, '')} → {e.code} {detail}")
        except urllib.error.URLError as e:
            sys.exit(f"push failed: {method} {u.replace(url, '')} → {e.reason}")

    def body_of(media):
        return json.dumps(dict(manifest, media=media), ensure_ascii=False).encode("utf-8")

    # 1. what to send: caps and the originals opt-out, decided before anything leaves the device
    keep_orig = want_orig()
    media, plan = [], []
    for m in manifest["media"]:
        with open(os.path.join(media_dir(), m["sha256"], "meta.json"), encoding="utf-8") as fh:
            files = json.load(fh)["files"]
        paths = {}
        for v in m["variants"]:
            if v == "orig" and not keep_orig:
                continue
            p = os.path.join(media_dir(), m["sha256"], files[v])
            if os.path.getsize(p) > CAP[v]:
                warn(f"{m['source']}: {v} is {os.path.getsize(p) / (1 << 20):.1f} MiB, over the server's "
                     f"{CAP[v] >> 20} MiB —— not uploaded")
                continue
            paths[v] = p
        if "view" in paths or "orig" in paths:
            media.append(dict(m, variants=[v for v in VARIANTS if v in paths]))
            plan.append(paths)
        else:
            warn(f"{m['source']}: nothing viewable left to upload —— left out")

    # 2. the manifest must be one the server accepts, and fit, before any request
    errs = manifest_errors(dict(manifest, media=media))
    if errs:
        sys.exit("the manifest breaks the server's rules —— nothing was sent.  Run `just media` again; if it "
                 "persists, report it.\n  " + "\n  ".join(errs[:20]) + (f"\n  … and {len(errs) - 20} more" if len(errs) > 20 else ""))
    body = body_of(media)
    if len(body) > MANIFEST_CAP:
        over = len(body) - MANIFEST_CAP
        ocr = sum(len((m.get("ocr") or "").encode("utf-8")) for m in media)
        sys.exit(f"manifest is {len(body) / (1 << 20):.1f} MiB; the server takes {MANIFEST_CAP >> 20} MiB —— cut about "
                 f"{-(-over * len(media) // len(body))} of {len(media)} items (narrow media_dirs) or "
                 f"{over / (1 << 20):.1f} MiB of the {ocr / (1 << 20):.1f} MiB of OCR text.  Nothing was uploaded.")

    # 3. what the server holds.  An older server has no usage endpoint: no precheck, no warning, no prune.
    st, raw = call("GET", f"{url}/api/media/usage", soft=(404, 405))
    usage = None if st in (404, 405) else json.loads(raw)
    if usage is None and prune:
        sys.exit("this server cannot list its media (no /api/media/usage) —— nothing to prune.  Nothing was uploaded.")
    server = (usage or {}).get("bytes") or {}
    local = {m["sha256"] for m in manifest["media"]}
    foreign = {k.split("/")[0] for k in server if k.split("/")[0] not in local}
    if foreign and not prune:
        warn(f"the server holds {len(foreign)} item(s) this device does not list.  The manifest is replaced (last "
             "push wins), so the web view will now show only this device's items.  If that is intended, "
             "`push --prune` also deletes their files.")

    def stale(k, v, size):
        """A derived copy the server records at another size (re-derived since) —— replaced, never orig."""
        return v != "orig" and server.get(k, size) != size

    def prune_now(keep):
        gone = [k for k in server if k not in keep and KEY_RE.fullmatch(k)]
        n_orig = sum(k.endswith("/orig") for k in gone)
        if n_orig and not keep_orig:
            print(f"media prune: media_orig is off —— {n_orig} original(s) uploaded earlier will be deleted from "
                  "the server")
        freed = 0
        for k in gone:
            call("DELETE", f"{url}/api/media/blob/{k}")
            size = server.pop(k)
            freed += size if isinstance(size, int) else 0
        print(f"media prune: deleted {len(gone)} blob(s) · {freed / (1 << 20):.1f} MiB freed")
        return freed

    # 4. the quota must fit at the peak (--prune may free room first).  Two orders:
    #    in place      each stale copy is deleted right before its own re-upload (a stop leaves at most one item
    #                  without a preview); usage moves step by step, so its peak is the running maximum (`rise`).
    #    delete-first  every stale copy is deleted before the first upload; usage only falls, then only rises, so
    #                  the peak is the end state, total − freed + needed.  Used only when in place would not fit.
    keep = {f"{m['sha256']}/{v}" for m, paths in zip(media, plan) for v in paths}
    replace, freed, needed, level, rise = set(), 0, 0, 0, 0
    for m, paths in zip(media, plan):
        for v, p in paths.items():
            k, size = f"{m['sha256']}/{v}", os.path.getsize(p)
            if stale(k, v, size):
                replace.add(k)
                old = server[k] if isinstance(server[k], int) else 0
                freed += old
                level -= old
            if k not in server or k in replace:
                needed += size
                level += size
                rise = max(rise, level)
    need = needed - freed
    quota, total = (usage or {}).get("quota") or 0, (usage or {}).get("total") or 0
    over = total + need - quota
    if quota and over > 0:
        freeable = sum(s for k, s in server.items() if k not in keep and KEY_RE.fullmatch(k) and isinstance(s, int))
        if not (prune and over <= freeable):
            sys.exit(f"push stopped before uploading: this push adds {need / (1 << 20):.1f} MiB, the server holds "
                     f"{total / (1 << 20):.1f} of {quota / (1 << 20):.1f} MiB —— {over / (1 << 20):.1f} MiB over the storage quota"
                     + (f" ({freeable / (1 << 20):.1f} MiB prunable)" if prune else "") + ".  Nothing was uploaded.  "
                     + FREE_SPACE)
        print(f"media prune: {over / (1 << 20):.1f} MiB over the quota —— pruning before the upload")
        total -= prune_now(keep)
    delete_first = bool(quota) and total + rise > quota
    if delete_first and replace:
        print(f"media push: replacing in place would peak {(total + rise - quota) / (1 << 20):.1f} MiB over the quota "
              f"—— deleting all {len(replace)} stale preview(s) first; if the run stops, those items have no preview "
              "until the next push")

    # 5. stale derived copies are deleted (the server would also answer HEAD 200 for the old copy) —— all up front or
    #    each right before its own upload, as step 4 chose.  Then blobs; one refused file does not stop the run.
    for k in sorted(replace) if delete_first else ():
        call("DELETE", f"{url}/api/media/blob/{k}")
    up = skipped = refused = sent = 0
    for m, paths in zip(media, plan):
        for v, p in list(paths.items()):
            k, u = f"{m['sha256']}/{v}", f"{url}/api/media/blob/{m['sha256']}/{v}"
            size = os.path.getsize(p)
            if k in replace and not delete_first:
                call("DELETE", u)
            if k not in replace and call("HEAD", u)[0] != 404:
                skipped += 1
                continue
            with open(p, "rb") as fh:      # streamed, not read whole —— an original can be up to 1 GiB
                st = call("PUT", u, fh, variant_ct(m, v, p), size, soft=SOFT)[0]
            if st in SOFT:
                del paths[v]
                refused += 1
                continue
            up += 1
            sent += size
        m["variants"] = [v for v in VARIANTS if v in paths]
    media = [m for m in media if "view" in m["variants"] or "orig" in m["variants"]]
    call("PUT", f"{url}/api/media/manifest", body_of(media), "application/json")
    print(f"media push: uploaded {up} · already there {skipped} · refused {refused} · {sent / (1 << 20):.1f} MiB · "
          f"manifest {len(media)} files")

    # 6. --prune: delete what the uploaded manifest does not list (only after it is safely in place)
    if prune:
        prune_now({f"{m['sha256']}/{v}" for m in media for v in m["variants"]})


def main(argv):
    if len(argv) < 2 or argv[1] not in ("scan", "push") or set(argv[2:]) - ({"--prune"} if argv[1] == "push" else set()):
        sys.exit(__doc__)
    scan() if argv[1] == "scan" else push(prune="--prune" in argv[2:])


if __name__ == "__main__":
    main(sys.argv)
