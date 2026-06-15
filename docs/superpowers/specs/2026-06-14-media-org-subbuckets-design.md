# Media Org — Sub-buckets + Skip-documents Design

**Date:** 2026-06-14
**Status:** Approved
**Extends:** 2026-06-14-media-org-design.md

## Goal

Enhance the Media Org organizer: split images into Photos/Graphics big
buckets, auto-create nested sub-buckets `<BigBucket>/<previous-location>/<year>/`
from cheap signals, and add a "Skip documents" option (default on).

## 1. Top buckets (kind + category, no file read)

| File | Bucket |
|------|--------|
| `kind=audio` | `Audio` |
| `kind=video` | `Video` |
| `kind=image`, `category=photo` | `Photos` |
| `kind=image`, `category=graphic` | `Graphics` |
| `kind=image`, category missing | ext guess: camera ext / jpg / jpeg / tif / tiff → `Photos`, else `Graphics` |
| `kind=document` | `Documents` (skipped when "Skip documents" is on) |
| other / unknown kind | skipped |

## 2. Sub-buckets: `<BigBucket>/<previous-location>/<year>/<name>`

- **previous-location** — the original parent-folder name.
  - local/onedrive: `os.path.basename(os.path.dirname(original_path))`
  - gdrive: POSIX parent of the remote-relative path
  - sanitized (trim, strip path separators); empty → `_other`
- **year** — `modified[:4]` when those are 4 digits, else `unknown`.

Example:
```
media-org/
  Photos/Camera Roll/2024/IMG_1234.jpg
  Graphics/Screenshots/2025/shot.png
  Video/Movies/2022/film.mp4
  Audio/Albums/2019/track.mp3
  Documents/Invoices/2023/...      (only if Skip-documents is off)
```

## 3. Skip documents

A "Skip documents" checkbox on the page, **checked (on) by default**. When on,
`kind=document` files are counted as skipped and not moved. The flag flows
through `/api/organize/preview`, `/api/organize/start`, and the worker.

## 4. Changes

- `file_ops.py`:
  - Replace flat `ORGANIZE_BUCKETS` map with
    `_top_bucket(kind, category, ext, skip_documents) -> str | None`.
  - Add `_prev_location(source, original_path)` and `_year_of(modified)`.
  - `_dest_path(source, bucket, subdirs, name)` gains the `subdirs` list
    (`[location, year]`).
  - `organize_plan(db, source, skip_documents)` and
    `enqueue_organize(source, skip_documents)` / `_run_organize(..., skip_documents)`
    take the flag. Preview `counts` keys become
    `Audio, Video, Photos, Graphics, Documents`.
- `webui.py`:
  - `api_organize_preview` / `api_organize_start` read `skip_documents`
    (default True).
  - Page adds the "Skip documents" checkbox (default checked) and shows the
    5-bucket preview line.

Camera-photo ext set reuses `media_index.CAMERA_EXTS` plus `jpg/jpeg/tif/tiff`.

## 5. Backward compatibility

- **Undo unchanged** — `media_org_moves` stores each file's `original_path`,
  so undo restores correctly regardless of the nested layout.
- `_already_sorted` matches anything under `media-org/` at any depth, so
  re-runs remain idempotent.

## 6. Testing

- Logic test (temp DB + temp files): files from different parent folders /
  years, a photo (camera ext) and a graphic, an audio and a video, plus a
  document. Assert nested paths `Photos/<folder>/<year>/…`, `Graphics/…`,
  `Audio/…`, `Video/…`; document skipped by default and included when the flag
  is off; undo restores everything.
- E2E through the page (Playwright): Skip-documents checkbox + nested layout on
  a small synthetic local set.

## Deferred

Deep metadata extraction (EXIF date-taken/camera, ID3 artist/album) — too slow
over the network; cheap signals only for now.
