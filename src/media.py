#!/usr/bin/env python3
"""Photos and videos linked to entities —— the device side.

    python src/media.py scan     collect → derive (thumb / view / OCR) → link → $KAL_HOME/media/manifest.json
    python src/media.py push     upload what the cloud does not have yet, then the manifest

Sources
  (a) local media embedded in vault notes:  ![[file.png]]  and  ![alt](relative/path.png)
  (b) opt-in folders, config key `media_dirs` (env KAL_MEDIA_DIRS as a JSON list wins over config.json):
        [{"path": "~/Pictures/whiteboards", "alias": "wb", "private": false}]
      A folder with "private": true is skipped entirely.

Linking (every link carries its basis; the same (media, entity) keeps only the strongest basis):
  manual 1.0   sidecar `<file>.md` / `<file>.json` with `entities: [...]`, or the note's frontmatter
               `media: [file, ...]` (those files get the note's `entities:` list)
  embed  0.9   the embed sits in a section (previous heading → next heading) that names an entity
               which was extracted from that very note
  ocr    0.7   an entity name (≥ 4 chars, degree ≥ 2) occurs in the picture's text
  doc          the note path is recorded in `docs` (a link to the document, not to an entity)
`no_llm` notes never contribute a reference, a link or a doc; a file referenced only by them is not listed.

The server never calls an LLM and never sees a file the device did not choose to upload: everything
interpretive happens here.  EXIF is stripped from the thumbnail and the view copy; the original is
uploaded as it is.
"""
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

IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic"}
VID_EXT = {".mp4", ".mov", ".m4v", ".webm"}
MIME_FIX = {".heic": "image/heic", ".m4v": "video/x-m4v", ".mov": "video/quicktime"}
THUMB_PX, VIEW_PX, OCR_CHARS = 256, 2048, 2000
CAPS = {"name": 120, "source": 512, "doc": 512}
BASIS_RANK = {"manual": 3, "embed": 2, "ocr": 1}
BASIS_SCORE = {"manual": 1.0, "embed": 0.9, "ocr": 0.7}
VARIANTS = ("thumb", "view", "orig")
MAX_MEDIA = 50_000


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


class Entities:
    """Name lookups over the entity rows.  Keys are the table's `name_norm` (what the server graph shares)."""

    def __init__(self, rows, docs=None):
        self.docs = docs or {}
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


def walk(root):
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if not d.startswith(".")]
        for f in fn:
            if not f.startswith("."):
                yield os.path.join(dp, f)


def inside(path, root):
    root = os.path.realpath(root)
    return os.path.realpath(path).startswith(root + os.sep)


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
    by_name = {}
    all_files = list(walk(vault)) if vault and os.path.isdir(vault) else []
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
        if not note.lower().endswith(".md"):
            continue
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
            if p and not any(inside(p, q) for q in private):
                found.setdefault(p, {"embeds": [], "notes": [], "dir": None})["embeds"].append(
                    (rel, section_of(text, m.start()), m.group(1)))
        for name in as_list(fm.get("media")):
            p = resolve(name, note)
            if p and not any(inside(p, q) for q in private):
                found.setdefault(p, {"embeds": [], "notes": [], "dir": None})["notes"].append(
                    (rel, as_list(fm.get("entities"))))

    for d in dirs:
        if d["private"]:
            continue
        if not os.path.isdir(d["path"]):
            warn(f"media dir {d['path']} does not exist")
            continue
        for p in walk(d["path"]):
            if is_media(p):
                rec = found.setdefault(os.path.abspath(p), {"embeds": [], "notes": [], "dir": None})
                rec["dir"] = rec["dir"] or (d["alias"], os.path.relpath(p, d["path"]))
    return found


def sidecar_entities(path):
    for ext in (".md", ".json"):
        sc = path + ext
        if not os.path.isfile(sc):
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


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


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


def write_thumb(im, out):
    """256px longest side, WebP.  Saved without exif=, so no metadata survives."""
    im = im.copy()
    im.thumbnail((THUMB_PX, THUMB_PX))
    im = im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im.convert("RGB")
    im.save(out, "WEBP", quality=80)


def ocr_text(path):
    """First 2,000 chars of tesseract output; "" when tesseract is missing or finds nothing."""
    if not have("tesseract"):
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
    gif = im.format == "GIF"
    im = ImageOps.exif_transpose(im)
    meta["width"], meta["height"], meta["duration_s"] = im.width, im.height, None
    write_thumb(im, os.path.join(out_dir, "thumb.webp"))
    if gif:
        shutil.copyfile(path, os.path.join(out_dir, "view.gif"))       # animation kept as is
    else:
        v = flat_rgb(im)
        if max(v.size) > VIEW_PX:
            v.thumbnail((VIEW_PX, VIEW_PX))
        v.save(os.path.join(out_dir, "view.jpg"), "JPEG", quality=85)
    meta["ocr"] = ocr_text(path)
    return meta


def derive_video(path, out_dir):
    pr = probe_video(path)
    vs = next((s for s in pr.get("streams", []) if s.get("codec_type") == "video"), {})
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
            run(["ffmpeg", "-v", "error", "-y", "-ss", ss, "-i", path, "-frames:v", "1", poster])
            if os.path.isfile(poster):
                break
        if os.path.isfile(poster):
            write_thumb(Image.open(poster), os.path.join(out_dir, "thumb.webp"))
            meta["ocr"] = ocr_text(poster)
        tmp = os.path.join(td, "view.mp4")
        # -map_metadata -1: location tags do not ride along.  An H.264 mp4 is only re-muxed, never re-encoded.
        if vs.get("codec_name") == "h264" and os.path.splitext(path)[1].lower() in (".mp4", ".m4v"):
            cmd = ["ffmpeg", "-v", "error", "-y", "-i", path, "-c", "copy", "-map_metadata", "-1",
                   "-movflags", "+faststart", tmp]
        else:
            cmd = ["ffmpeg", "-v", "error", "-y", "-i", path, "-vf", "scale=-2:'min(720,ih)'",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-map_metadata", "-1", "-movflags", "+faststart", tmp]
        if run(cmd).returncode == 0 and os.path.isfile(tmp):
            shutil.copyfile(tmp, os.path.join(out_dir, "view.mp4"))
        else:
            warn(f"{os.path.basename(path)}: no view copy (ffmpeg failed)")
    return meta


def process(path):
    """Derive (or reuse) the variants of one file → its meta dict (sha256, mime, bytes, variants, …) or None."""
    ext = os.path.splitext(path)[1].lower()
    sha = sha256_of(path)       # ponytail: re-hashes every file each run; cache by (path, size, mtime) if slow
    d = os.path.join(media_dir(), sha)
    mj = os.path.join(d, "meta.json")
    if os.path.isfile(mj):
        try:
            with open(mj, encoding="utf-8") as fh:
                meta = json.load(fh)
            if all(os.path.isfile(os.path.join(d, f)) for f in meta["files"].values()):
                return meta
        except (OSError, ValueError, KeyError):
            pass
    os.makedirs(d, exist_ok=True)
    meta = derive_image(path, d, ext) if ext in IMG_EXT else derive_video(path, d)
    if meta is None:
        return None
    shutil.copyfile(path, os.path.join(d, "orig" + ext))
    files = {}
    for v, names in (("thumb", ["thumb.webp"]), ("view", ["view.jpg", "view.gif", "view.mp4"]), ("orig", ["orig" + ext])):
        for n in names:
            if os.path.isfile(os.path.join(d, n)):
                files[v] = n
    meta.update(sha256=sha, bytes=os.path.getsize(path), files=files, variants=[v for v in VARIANTS if v in files],
                mime=MIME_FIX.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream")
    tmp = mj + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False)
    os.replace(tmp, mj)        # meta.json last: its presence means every variant is complete
    return meta


# ───────────────────────── linking ─────────────────────────

def build_links(rec, meta, path, ents):
    """→ (links keyed by entity key, docs).  The strongest basis per entity wins."""
    links, docs = {}, []

    def add(key, name, typ, basis, doc):
        old = links.get(key)
        if old is None or BASIS_RANK[basis] > BASIS_RANK[old["basis"]]:
            links[key] = {"entity": key[:200], "name": name[:CAPS["name"]], "type": typ, "basis": basis,
                          "score": BASIS_SCORE[basis], "doc": doc[:CAPS["doc"]]}

    for name in sidecar_entities(path):
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
        low = section.lower()
        did = ents.docs.get(note, (None, False))[0]
        for r in ents.by_doc.get(did, []) if did is not None else []:
            if occurs(r["name"], low):
                add(r.get("name_norm") or r["name"].lower(), r["name"], r.get("type") or "", "embed", note)
    text = (meta.get("ocr") or "").lower()
    if text:
        for r in ents.ocr_pool:
            if occurs(r["name"], text):
                add(r.get("name_norm") or r["name"].lower(), r["name"], r.get("type") or "", "ocr",
                    docs[0] if docs else "")
    return links, list(dict.fromkeys(docs))


def source_of(rec, path, vault):
    if rec["embeds"]:
        n, _s, ref = rec["embeds"][0]
        return f"vault:{n}#{ref}"
    if rec["notes"]:
        return f"vault:{rec['notes'][0][0]}#{os.path.basename(path)}"
    return f"dir:{rec['dir'][0]}/{rec['dir'][1]}"


def scan():
    vault = vault_path.vault()
    ents_rows, docs = load_graph()
    ents = Entities(ents_rows, docs)
    found = collect(vault, media_dirs(), docs)
    out, n_new = {}, 0
    for path in sorted(found):
        rec = found[path]
        meta = process(path)
        if meta is None:
            continue
        links, dcs = build_links(rec, meta, path, ents)
        m = out.get(meta["sha256"])
        if m is None:
            m = out[meta["sha256"]] = {
                "sha256": meta["sha256"], "kind": meta["kind"], "mime": meta["mime"], "bytes": meta["bytes"],
                "width": meta.get("width"), "height": meta.get("height"), "duration_s": meta.get("duration_s"),
                "taken_at": meta.get("taken_at"), "source": source_of(rec, path, vault)[:CAPS["source"]],
                "variants": meta["variants"], "ocr": (meta.get("ocr") or "")[:OCR_CHARS], "links": {}, "docs": []}
        for k, l in links.items():
            if k not in m["links"] or BASIS_RANK[l["basis"]] > BASIS_RANK[m["links"][k]["basis"]]:
                m["links"][k] = l
        m["docs"] = list(dict.fromkeys(m["docs"] + dcs))
    media = []
    for m in out.values():
        m["links"] = sorted(m["links"].values(), key=lambda l: (-l["score"], l["entity"]))
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


def push():
    url = (os.environ.get("KAL_CLOUD_URL") or "").rstrip("/")
    token = os.environ.get("KAL_CLOUD_TOKEN")
    if not url or not token:
        sys.exit("KAL_CLOUD_URL and KAL_CLOUD_TOKEN are required (same as `just push`)")
    mp = os.path.join(media_dir(), "manifest.json")
    if not os.path.isfile(mp):
        sys.exit("no manifest —— run `just media` first")
    with open(mp, "rb") as fh:
        body = fh.read()
    manifest = json.loads(body)
    auth = {"Authorization": f"Bearer {token}"}

    def call(method, u, data=None, ct=None, length=None):
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
                return r.status
        except urllib.error.HTTPError as e:
            if method == "HEAD" and e.code == 404:
                return 404
            detail = e.read(300).decode("utf-8", "replace") if method != "HEAD" else ""
            sys.exit(f"push failed: {method} {u.replace(url, '')} → {e.code} {detail}")
        except urllib.error.URLError as e:
            sys.exit(f"push failed: {method} {u.replace(url, '')} → {e.reason}")

    up = skipped = nbytes = 0
    for m in manifest["media"]:
        with open(os.path.join(media_dir(), m["sha256"], "meta.json"), encoding="utf-8") as fh:
            files = json.load(fh)["files"]
        for v in m["variants"]:
            u = f"{url}/api/media/blob/{m['sha256']}/{v}"
            if call("HEAD", u) != 404:
                skipped += 1
                continue
            p = os.path.join(media_dir(), m["sha256"], files[v])
            size = os.path.getsize(p)
            with open(p, "rb") as fh:      # streamed, not read whole —— an original can be up to 1 GB
                call("PUT", u, fh, variant_ct(m, v, files[v]), size)
            up += 1
            nbytes += size
    call("PUT", f"{url}/api/media/manifest", body, "application/json")
    print(f"media push: uploaded {up} · already there {skipped} · {nbytes / 1e6:.1f} MB · manifest {len(manifest['media'])} files")


def main(argv):
    if len(argv) < 2 or argv[1] not in ("scan", "push"):
        sys.exit(__doc__)
    scan() if argv[1] == "scan" else push()


if __name__ == "__main__":
    main(sys.argv)
