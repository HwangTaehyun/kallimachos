# Media: photos and videos linked to entities

kal can attach the photos and videos in your vault to the entities they show. The code is
`src/media.py`. Scanning, deriving and linking all happen on your machine. The server only
receives the files you upload, and it never calls an LLM.

```bash
just media                                # scan → ~/.kal/media/manifest.json
just media-push                           # upload what the server lacks, then the manifest
just media-push --prune                   # the same, then delete server files the manifest no longer lists
```

`kal_entity` lists an entity's media and `kal_media` returns one thumbnail with its links
(MCP tools, see `src/kal_mcp.py`).

## What gets scanned

1. **Embeds in notes.** `![[file.png]]` and `![alt](relative/path.png)`, resolved the way
   Obsidian does it: relative to the note, then the vault root, then by file name anywhere in
   the vault. URLs and `data:` links are ignored.
2. **Folders you opt in**, with the config key `media_dirs` in `~/.kal/config.json`. The env var
   `KAL_MEDIA_DIRS` (same JSON) overrides it.

   ```json
   {"media_dirs": [{"path": "~/Pictures/whiteboards", "alias": "wb", "private": false}]}
   ```

   A folder marked `"private": true` is skipped entirely, including embeds that point into it.

Notes marked `no_llm` contribute nothing: no embed, no link and no doc. A file that only those
notes reference is not listed.

Formats: png, jpg, gif, webp, heic and heif for images, and mp4, mov, m4v and webm for video.

## How links are made

Each link records its basis. When one file links to the same entity in several ways, only the
strongest basis is kept.

| basis  | score | when |
|--------|-------|------|
| manual | 1.0   | A sidecar `<file>.md` or `<file>.json` with `entities: [...]`. Or a note whose frontmatter has `media: [file, ...]`: those files get the note's `entities:` list. |
| embed  | 0.9   | The embed sits in a section (from one heading to the next) that names an entity extracted from that same note. |
| ocr    | 0.7   | The picture's text (tesseract) contains an entity name that is at least 4 characters long and has degree ≥ 2. |

**The OCR filter for ordinary words.** Some entity names are also everyday words, such as
"index", "notes" or "rebuild". An OCR match is ignored when the name appears in more than
`OCR_GENERIC` (5) times as many notes as the entity was extracted from. On a real graph,
ordinary words measured at 10–54× and real names at ≤ 4×.

The notes a file is embedded in are recorded in the item's `docs` list. They are not a link
basis.

## Optional tools

| tool | what it adds | without it |
|------|--------------|------------|
| `ffmpeg` + `ffprobe` | video thumbnail and a ≤ 720p H.264 view copy | the video goes up as the original only |
| `tesseract` with `kor` and `eng` data | OCR text and `ocr` links | no OCR links (falls back to `eng` when `kor` is missing) |
| `pillow-heif` (a declared dependency) | HEIC/HEIF thumbnails and view copies | `.heic` files are skipped with a warning |

An H.264 mp4 that is already ≤ 720p is only re-muxed, which is fast. Anything taller, and any
re-mux that would exceed the view cap, is re-encoded to ≤ 720p.

## What `push` sends

Every file gets up to three variants: `thumb` (256 px WebP), `view` (2048 px JPEG, the GIF as it
is, or the 720p mp4) and `orig`. The server limits them to 1 MiB, 300 MiB and 1 GiB. A variant
over its limit is skipped with a warning. When a file is left with neither `view` nor `orig`, it
is dropped from the uploaded manifest.

- The manifest's size (server limit 16 MiB) is checked **before** any file is uploaded. If it is
  too big, push stops and tells you how many items or how much OCR text to cut.
- If the server refuses one file (400, 413 or 415), push skips it and continues. Network errors
  and 401, 402 and 5xx responses stop the run.
- Files the server already has are not sent again.

### One device per account

The uploaded manifest **replaces** the server's copy, so the last push wins. If two devices push
to the same account, the web view shows only the items from the device that pushed last. The
other device's files stay on the server and keep counting toward your storage. When the server
holds items that this device does not list, push warns you before it replaces the manifest.

`--prune` deletes every server file that the newly uploaded manifest does not list. It runs only
after the manifest upload succeeds. Without the flag, push never deletes anything.

## Privacy

- The thumbnail and the JPEG view copy are written without EXIF, so they carry no GPS, camera
  or time data. They keep the colour profile (ICC), so Display-P3 photos are not washed out.
- A GIF's view copy is the file unchanged, so any metadata the GIF has is kept.
- Video view copies are written with `-map_metadata -1`, so location tags are dropped.
- **Originals are uploaded byte for byte, including their EXIF and GPS.** To keep originals on
  your machine, set this in `~/.kal/config.json`:

  ```json
  {"media_orig": false}
  ```

  You can also set the env var `KAL_MEDIA_ORIG=0`. With either one, `orig` is never uploaded and
  never listed. Originals that are already on the server are deleted only when you run
  `push --prune`.
