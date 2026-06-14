# Media Org — Design Spec

**Date:** 2026-06-14
**Status:** Approved (pending implementation plan)

## Goal

Add a web page called **Media Org** that lets the user pick one of the
indexed sources, creates a `media-org/` folder at that source's root, and
moves the source's indexed files into category buckets (Audio, Video,
Images, Documents). Every move is recorded so the entire run can be undone,
even after a server restart.

## Scope decisions

- **Drive scope:** indexed sources only (reuses the index + existing
  local/rclone move code). Picker offers **Local, OneDrive, Google Drive**.
  **QNAP is excluded for now** — it is running a multi-day background index
  job; it can be re-enabled later with no structural change.
- **Operation:** move (not copy), with persistent undo.
- **Buckets:** four top-level kinds — `Audio/`, `Video/`, `Images/`,
  `Documents/`. (Documents are included.)
- **Undo durability:** persistent DB log, survives restart.

## 1. Source roots & buckets

`media-org/` is created at each source's root:

| Source       | `media-org` root                              |
|--------------|-----------------------------------------------|
| Local        | `C:\Users\sonny\media-org` (added to scan roots) |
| OneDrive     | `…\OneDrive\media-org`                         |
| Google Drive | `gdrive:media-org`                             |
| QNAP         | *(deferred — not offered yet)*                 |

Local is scanned from six scattered folders (`~/Videos`, `~/Pictures`,
`~/Music`, `~/Downloads`, `~/Documents`, `~/Desktop`), so it has no single
natural root. We anchor its `media-org/` at the user home and **add that
folder to `LOCAL_ROOTS`** so reorganized files remain in scan scope and are
not pruned on the next rescan.

Each file moves to `<root>/media-org/<Bucket>/<name>`, where Bucket is
derived from the index `kind`:

| index `kind` | Bucket folder |
|--------------|---------------|
| `video`      | `Video`       |
| `audio`      | `Audio`       |
| `image`      | `Images`      |
| `document`   | `Documents`   |

- Files already located under `<root>/media-org/` are **skipped**
  (idempotent re-runs).
- Name collisions in the destination get ` (1)`, ` (2)`, … suffixes.

## 2. Schema — `media_org_moves`

Persistent table, survives restart. One row per moved file:

```
media_org_moves(
    id            INTEGER PRIMARY KEY,
    batch_id      TEXT NOT NULL,   -- groups one organize run (timestamp+source)
    source        TEXT NOT NULL,
    file_id       TEXT NOT NULL,   -- files.id at move time
    original_path TEXT NOT NULL,
    new_path      TEXT NOT NULL,
    moved_at      TEXT NOT NULL,
    undone        INTEGER NOT NULL DEFAULT 0
)
```

`batch_id` groups one organize run so the whole run can be undone as a unit.
Created once-per-process in `media_index._init_schema` alongside the other
DDL.

## 3. Backend (`file_ops.py`)

- `source_root(source) -> str` — the root table above; raises for QNAP /
  unknown sources while deferred.
- **Preview** — `organize_plan(db, source)`: a fast index query returning
  counts per bucket, total movable, and count already-sorted/skipped. No
  disk access. Drives the confirmation step before any move.
- **`OrganizeJobManager`** — mirrors the existing `DeleteJobManager`:
  single background worker, queue, `status()` snapshot, `cancel()`. Supports
  a `mode` of `organize` or `undo`. Per file (organize):
  1. Compute destination `<root>/media-org/<Bucket>/<name>` (+ collision
     suffix).
  2. Move: local/onedrive via `shutil.move` with `long_path`; gdrive via
     `rclone moveto` (server-side, no download).
  3. Update `files.path / name / ext` in the index (move-first, then commit;
     `_write_with_retry` for lock resilience — same pattern as deletes).
  4. Insert a `media_org_moves` row.
  - Missing files / OneDrive cloud-only placeholders are **skipped and
    counted**, never fatal.
- **Undo** runs through the same manager in `undo` mode: for a `batch_id`,
  move each not-yet-undone file back to `original_path`, restore the index
  path, set `undone = 1`.
- `delete_jobs`-style module-level singleton: `organize_jobs =
  OrganizeJobManager()`.

## 4. Frontend (`/media-org` page + nav link)

- Added to the site nav / landing alongside Media, Documents, Duplicates.
- **Drive picker** — radio buttons for the configured non-QNAP sources
  (Local, OneDrive, Google Drive).
- **Preview** button → shows e.g. *"1,240 files → Audio 310, Video 95,
  Images 700, Documents 135 (42 already sorted, skipped)"*.
- **Organize** button (enabled after a preview) → starts the background job.
  An on-page progress bar shows *"Moving X of Y — current file (N failed)"*
  with a **Cancel** button, reusing the existing delete-bar polling pattern
  (`IH` helper).
- **Past runs** list (from `media_org_moves` grouped by `batch_id`): date,
  source, file count, and an **Undo** button per batch (disabled once fully
  undone).

## 5. API

| Method + path                          | Purpose                                  |
|----------------------------------------|------------------------------------------|
| `GET  /api/organize/preview?source=`   | Bucket counts + total, no disk touch     |
| `POST /api/organize/start {source}`    | Enqueue an organize run, returns batch_id|
| `GET  /api/organize/status`            | Job snapshot for the progress bar        |
| `POST /api/organize/cancel`            | Drop the queue; in-flight move finishes  |
| `GET  /api/organize/batches`           | Past runs grouped by batch_id            |
| `POST /api/organize/undo {batch_id}`   | Enqueue an undo run for that batch        |

## 6. Error handling & safety

- Per-file errors counted, with a capped error list surfaced to the UI
  (like deletes).
- **Preview-before-execute** gate; Organize is disabled until a preview ran
  for the selected source.
- OneDrive cloud-only placeholders are skipped (cannot move a file that
  isn't downloaded locally).
- gdrive moves are server-side (no download/upload round trip).
- Cancel stops the queue; the in-flight move is allowed to finish (a remote
  move cannot be safely interrupted) — same contract as deletes.
- Local `media-org` is added to scan roots so reorganized files are not
  pruned by a later rescan.

## 7. Testing

End-to-end through the running app (the `verify` skill, driven through the
real page — not unit tests):

1. Preview a source → counts render and match an independent index query.
2. Run organize on a small synthetic set → files land in the correct
   buckets on disk, `files.path` updates, `media_org_moves` rows exist.
3. Cancel mid-run → queue stops, in-flight file completes, status reflects
   `cancelled`.
4. Undo the batch → every file returns to its `original_path`, index paths
   restored, rows marked `undone`.
5. Re-run organize → already-sorted files are skipped (idempotent).

## Deferred / out of scope

- QNAP source (re-enable after the index job completes).
- Fine-grained buckets (Photos/Graphics, Word/PDF/etc.) — top-level kinds
  only for now.
- Copy mode — move only.
