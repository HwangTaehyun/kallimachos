# Media: photos and videos linked to entities

kal can attach the photos and videos in your vault to the entities they show. The code is
`src/media.py`. Scanning, deriving and linking all happen on your machine. The server only
receives the files you upload, and it never calls an LLM.

```bash
just media                                # scan → ~/.kal/media/manifest.json
just media-push                           # upload what the server lacks, then the manifest
just media-push --prune                   # the same, then delete server files the manifest no longer lists
just push                                 # the graph, then (once you opted in) `just media` + `just media-push`
```

`just push` runs the media step after the graph upload has succeeded, but only when both hold:

- media is in use: a manifest exists (you ran `just media` once), `media_dirs` is set, or this
  device has a push record for the same server and account (so `just reset` does not switch it
  off). Embeds in notes alone do not count.
- you opted in to uploading: this device already ran `just media-push` to the same server and
  account (there is a `pushed.json` record for them), or `~/.kal/config.json` has
  `"media_push": true` (env `KAL_MEDIA_PUSH=1`).

Originals go up with their EXIF and GPS, so running `just media` locally never starts uploads by
itself. When either condition fails, the step prints one line saying how to turn it on and
sends nothing.

Only one media push runs at a time on a device: a second one stops at once with "another media
push is running". (An older push that pruned after a newer one had published could otherwise
delete the newer one's files.)

**A source that cannot be read never causes a deletion.** If the vault or a folder in
`media_dirs` is missing, unreadable or not a directory, or a subfolder, photo, video or note inside
them cannot be read, when the scan runs (an unplugged photo
drive, a moved vault), the scan warns and notes it in `manifest.json` (that note is never sent).
The media step of `just push` then sends nothing at all, neither the manifest nor any deletion.
An explicit `just media-push` still replaces the server's manifest, so the web view stops
showing those files, but it deletes nothing from the server, and `--prune` refuses. Reconnect
the source (or remove it from `media_dirs`) and scan again. A folder that exists but is empty,
such as an unmounted mount point on Linux, cannot be told apart from an empty one. A file that no longer derives (a HEIC after its decoder was removed, a read error) counts the same way, and a `config.json` or `KAL_MEDIA_DIRS` that cannot be parsed stops the scan before anything changes.

If the media step fails, `just push` exits
non-zero and says that the graph upload already went through; fix the cause and run
`just media-push`.

`just status` shows a `media` section (and `media` in `--json`): how many items there are, how
many derivations wait for a retry, how many items have no preview, and whether the items changed
since the last successful media push. It reads only local files and makes no network request.
`just reset extract` (and `all`) deletes the media cache but keeps the push record.

`kal_entity` lists an entity's media and `kal_media` returns one thumbnail with its links
(MCP tools, see `src/kal_mcp.py`).

## What gets scanned

1. **Embeds in notes.** `![[file.png]]` and `![alt](<relative/path.png>)`, resolved the way
   Obsidian does it: relative to the note, then the vault root, then by file name anywhere in
   the vault. URLs and `data:` links are ignored.
2. **Folders you opt in**, with the config key `media_dirs` in `~/.kal/config.json`. The env var
   `KAL_MEDIA_DIRS` (same JSON) overrides it.

   ```json
   {"media_dirs": [{"path": "~/Pictures/whiteboards", "alias": "wb", "private": false}]}
   ```

   A folder marked `"private": true` is skipped entirely, wherever it sits. If it is inside a
   public folder (public `~/Pictures`, private `~/Pictures/secret`), the scan of the public
   folder does not enter it. Embeds and frontmatter `media:` entries that point into it are
   ignored too.

   Paths are compared after resolving symlinks. A note, a media file or a sidecar that is a
   symlink into a private folder is not read, wherever the link itself sits, and a directory
   symlink is never followed. Only the folder itself and what is inside it count as private:
   with `~/Pictures/secret` private, `~/Pictures/secret2` is still scanned.

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
basis. The server takes at most 50 notes per item, so when a file appears in more notes, only
the first 50 by path are listed, with a warning. A note path longer than 512 bytes is never
shortened, because a shortened path would name a different note. Instead it is left out of
`docs`, and every link that would name it as its `doc` is dropped. An item whose notes **all**
have such paths is left out of the manifest with a warning, unless the file also sits in a public
media folder: that alone puts it in, with no note listed. An empty `docs` list or `doc` means
"not from a note" (a media folder or a sidecar), and the MCP tools show those without a note
check, so a file from a note must never end up with one. Items from media folders and sidecars
are not affected.

## Optional tools

| tool | what it adds | without it |
|------|--------------|------------|
| `ffmpeg` + `ffprobe` | video thumbnail and a ≤ 720p H.264 view copy | the video goes up as the original only |
| `tesseract` with `kor` and `eng` data | OCR text and `ocr` links | no OCR links, with one warning per scan (falls back to `eng` when `kor` is missing) |
| `pillow-heif` (a declared dependency) | HEIC/HEIF thumbnails and view copies | `.heic` files are skipped with a warning |

An H.264 `.mp4` or `.m4v` that is already ≤ 720p and 8-bit 4:2:0 (`yuv420p`), with AAC or MP3
audio or no audio, and not rotated, is only re-muxed, which is fast. Anything taller, 4:4:4 or 10-bit H.264, audio
in any other codec such as PCM or Opus (browsers cannot play those), and any re-mux that would
exceed the view cap, is re-encoded to ≤ 720p H.264 with AAC audio. Odd heights are rounded down
to an even number. 10-bit and HDR video is converted to 8-bit without tone mapping, so its
colours may look flat.

Phones store a portrait clip as landscape frames plus a rotation. The manifest's `width` and
`height` are the displayed size, the thumbnail is upright, and a rotated clip is always
re-encoded so that the view copy's pixels are upright too. A re-mux would keep the sideways
frames, and a player that ignores the rotation would show them sideways. Videos cached before
this was recorded are derived once more on the next scan. Images are not affected.

OCR reads the original at full resolution, except HEIC/HEIF: tesseract cannot open those, so they
are read from the derived JPEG view copy (≤ 2048 px, upright). Each tool run has a time limit
(tesseract and ffprobe 120 s per attempt, ffmpeg 30 min); a run that hits it counts as that file's
failure, the scan goes on, and the next scan tries that file again. Every cached item records
which of these tools were installed when it was derived, and is derived again once a missing one
appears. Items cached before that record existed are kept as they are, except a video with no view
copy, which is derived again when ffmpeg is now installed.

Derived copies (thumbnails and view copies) are cached in `~/.kal/media/<sha256>/` and stamped
with `DERIVE_VERSION`. When an upgrade changes how they are made, the old copies are derived
again automatically on the next scan, and the next push replaces them on the server.

## What `push` sends

Every file gets up to three variants: `thumb` (256 px WebP), `view` (2048 px JPEG, the GIF as it
is, or the 720p mp4) and `orig`. The server limits them to 1 MiB, 300 MiB and 1 GiB. A variant
over its limit is skipped with a warning. When a file is left with neither `view` nor `orig`, it
is dropped from the uploaded manifest.

- The manifest is checked against every rule the server applies to it (field types, field
  lengths, the 50-note `docs` limit, required fields) **before any request**. Types follow the
  server's decoder: `bytes`, `width` and `height` must be integers (not `true`, not `"10"`),
  `duration_s` and a link's `score` numbers, text fields strings, and `docs` and `variants` lists
  of strings. If anything breaks a rule, push
  stops with the list and sends nothing, so no file is uploaded or pruned for a manifest the
  server would then refuse.
- The manifest's size (server limit 16 MiB) is checked **before** any file is uploaded. If it is
  too big, push stops and tells you how many items or how much OCR text to cut.
- Your storage quota is checked **before** any file is uploaded, too. Push asks the server what it
  already holds and works out the highest usage this push will reach. It stops if that goes over
  the quota, and tells you how much is over and how to make room: `just media-push --prune`, or
  `media_orig` off. Without `--prune`, push never deletes anything to make room.
  If the server answers 507 (quota full) during the upload anyway, push stops the same way and
  does not replace the manifest.
- **Per-file responses:** 400, 413 (the file is over its limit) and 415. Push skips that one file
  with a warning and continues. **Any other error response stops the run**, including 401, 402,
  409, 410, 411, 507 and 5xx, as do network errors. A 409 means the server's media was wiped
  while this push ran, another upload of the same file is in progress, or the manifest named
  files the server no longer has: push stops with "run push again". After a wipe the next push uploads everything again, and its record
  forgets the files the server no longer has. Push uploads one file at a time, so a 409 that is
  not a wipe most likely means another push to the same account is running.
- Files the server already has are not sent again. The exception is a thumbnail or view copy that
  the server holds at a different size (it was derived again after an upgrade). Push replaces
  it. Normally each old copy is deleted right before its own replacement is uploaded, so a run
  that stops leaves at most one item without a preview. The quota check walks that order and
  takes its highest point. Only when that point is over the quota (while the end state fits),
  push says so and **deletes all the old copies before the first upload** instead: usage then
  never rises above the final total, but if the run stops, those items show no preview until the
  next push. An original is identified by its content and is never replaced.
- An older server without the usage endpoint gets no quota check, no multi-device warning and no
  automatic cleanup, and `--prune` stops with a message.
- The server allows 30 minutes per upload. A 1 GiB original needs a steady upload speed of about
  0.6 MiB/s to finish in time. On a slow connection, set `media_orig` off.

### One device per account

The uploaded manifest **replaces** the server's copy, so the last push wins. If two devices push
to the same account, the web view shows only the items from the device that pushed last. The
other device's files stay on the server and keep counting toward your storage. When the server
holds items that this device does not list, push warns you before it replaces the manifest.

**Automatic cleanup of this device's own files.** Push keeps a record in
`~/.kal/media/pushed.json` (readable only by you) of the server files this device uploaded, or
found already on the server for an item it listed, and for which server and account (a SHA-256
hash of the push token, never the token itself). Every push deletes those
of them that the new manifest no longer lists, so a photo you delete or stop embedding also leaves
the server. Files this device has no record of are never deleted automatically: another device's
files, and anything uploaded before the record existed (the first push with the record deletes
nothing). Push only warns about them. The record is written even when a run stops, so a file
uploaded before the stop can still be cleaned up later. A record made for another server address,
or with another token, counts as no record: switching accounts on the same server deletes
nothing from either account.

`--prune` deletes every server file that the server has a record of and that the newly uploaded
manifest does not list, whoever uploaded it. Both kinds of deletion run after the manifest upload
succeeds. The one exception is `--prune` when the quota check needs the room: it then deletes
before the upload, and if the run stops after that, the server's previous manifest can point at
files that were already deleted until the next successful push.
With `media_orig` off, the next push deletes the originals this device uploaded earlier, and
prints a line saying so.

If the server loses its records but keeps the stored files, the next push uploads and records
this device's files again. Stored files that this device no longer lists stay unrecorded, so
`--prune` cannot see them; they are removed only when the account is deleted.

**Treat the push token like a password.** Anyone who has it can upload to your account, and with
`--prune` or a direct DELETE they can delete your media.

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
  never listed. Originals that this device already uploaded are deleted by the next push; other
  originals on the server only by `push --prune`.

## The cloud copy is for viewing, not a backup

Your device holds the source. The hosted service keeps the uploaded files so you can see them from
anywhere, but it does **not** back up originals or view copies: if the storage is lost, it is
refilled by running `just media-push` again from the device. Only the small records (thumbnails,
the manifest and the usage ledger) are kept in the service's daily backup. Keep your own copy of
every photo and video — do not delete a file from your device because it is in the cloud.
