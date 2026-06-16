"""File delete (session trash), restore, move, and mark-for-deletion."""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import media_index as mi
from jobs import BaseJobManager, now_iso

TRASH_ROOT = Path(__file__).parent / ".trash"


def long_path(path: str) -> str:
    """Windows extended-length form (\\\\?\\...) so >260-char paths work.

    Returns the path unchanged on non-Windows or if already prefixed."""
    if os.name != "nt" or not path:
        return path
    full = os.path.abspath(path)
    return full if full.startswith("\\\\?\\") else "\\\\?\\" + full


def exists_on_disk(path: str) -> bool:
    """Robust file-existence check that tolerates Windows >260-char paths,
    which plain os.path.isfile reports as missing without the \\\\?\\ prefix."""
    try:
        if os.path.isfile(path):
            return True
    except OSError:
        pass
    if os.name == "nt":
        try:
            return os.path.isfile(long_path(path))
        except OSError:
            return False
    return False


# Windows attributes set on OneDrive "Files On-Demand" cloud-only placeholders.
_FILE_ATTRIBUTE_OFFLINE = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_CLOUD_ONLY_ATTRS = (_FILE_ATTRIBUTE_OFFLINE
                     | _FILE_ATTRIBUTE_RECALL_ON_OPEN
                     | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)


def is_cloud_placeholder(path: str) -> bool:
    """True if the path is a OneDrive Files-On-Demand cloud-only placeholder —
    it exists in the namespace but isn't downloaded locally. These must never
    be pruned. Stats the entry itself (no symlink follow) so it isn't recalled."""
    if os.name != "nt":
        return False
    try:
        attrs = os.stat(long_path(path), follow_symlinks=False).st_file_attributes
    except OSError:
        return False
    return bool(attrs & _CLOUD_ONLY_ATTRS)


# media-org bucket per index `kind`. Files of other kinds are not moved.
# Top-level Media Org buckets are derived from kind + category (see _top_bucket).
ORGANIZE_BUCKET_NAMES = ("Audio", "Video", "Photos", "Graphics", "Documents")
# Image extensions treated as camera photos when the index category is missing
# (cheap ext guess only — no file read).
_PHOTO_EXTS = set(mi.CAMERA_EXTS) | {"jpg", "jpeg", "tif", "tiff"}


def _top_bucket(kind: str, category, ext, skip_documents: bool):
    """Big bucket for a file, or None if it should be skipped. Cheap signals
    only (kind, stored category, extension) — never reads the file."""
    if kind == "audio":
        return "Audio"
    if kind == "video":
        return "Video"
    if kind == "image":
        if category == "photo":
            return "Photos"
        if category == "graphic":
            return "Graphics"
        return "Photos" if (ext or "").lower() in _PHOTO_EXTS else "Graphics"
    if kind == "document":
        return None if skip_documents else "Documents"
    return None


def _is_remote(source: str) -> bool:
    """True when rows are stored via rclone (gdrive, qnap) rather than on the
    local filesystem. Delegates to the source registry so this concept lives in
    exactly one place (see media_index.Source.is_remote)."""
    return mi.get_source(source).is_remote


def source_root(source: str) -> str:
    """Root under which media-org/ is created. local/onedrive -> absolute path;
    gdrive -> '' (the remote root). QNAP is deferred."""
    if source == "local":
        return str(mi.USER)
    if source == "onedrive":
        return str(mi.ONEDRIVE_ROOT)
    if source == "gdrive":
        return ""
    raise ValueError(f"Media Org is not available for source: {source}")


def _media_org_root(source: str) -> str:
    if _is_remote(source):
        return "media-org"
    return os.path.join(source_root(source), "media-org")


def _already_sorted(source: str, path: str) -> bool:
    base = _media_org_root(source)
    if _is_remote(source):
        return path.replace("\\", "/").lstrip("/").startswith(base + "/")
    return os.path.normcase(os.path.abspath(path)).startswith(
        os.path.normcase(os.path.abspath(base)) + os.sep)


def _sanitize_seg(seg: str) -> str:
    """Make a path segment safe to use as a folder name."""
    seg = (seg or "").replace("/", "_").replace("\\", "_").strip().strip(".")
    return seg or "_other"


def _prev_location(source: str, original_path: str) -> str:
    """The file's original parent-folder name (its 'previous location')."""
    if _is_remote(source):
        parts = original_path.replace("\\", "/").strip("/").split("/")
        parent = parts[-2] if len(parts) >= 2 else ""
    else:
        parent = os.path.basename(os.path.dirname(original_path))
    return _sanitize_seg(parent)


def _year_of(modified) -> str:
    """Year from the stored ISO modified date, or 'unknown'."""
    s = str(modified or "")
    return s[:4] if (len(s) >= 4 and s[:4].isdigit()) else "unknown"


def _dest_path(source: str, bucket: str, subdirs: list, name: str) -> str:
    parts = [bucket] + list(subdirs) + [name]
    if _is_remote(source):
        return "media-org/" + "/".join(parts)
    return os.path.join(_media_org_root(source), *parts)


def _dest_exists(source: str, dest: str) -> bool:
    if _is_remote(source):
        return mi.rclone.exists(source, dest)
    return exists_on_disk(dest)


def _split_ext(source: str, dest: str):
    if _is_remote(source):
        dot = dest.rfind(".")
        return (dest[:dot], dest[dot:]) if dot > dest.rfind("/") else (dest, "")
    return os.path.splitext(dest)


def _uniquify(source: str, dest: str) -> str:
    """Append ' (1)', ' (2)', ... before the extension if dest is taken.
    The single worker moves files one at a time, so each move lands before the
    next check — sequential existence checks also resolve same-run collisions."""
    if not _dest_exists(source, dest):
        return dest
    base, ext = _split_ext(source, dest)
    n = 1
    while _dest_exists(source, f"{base} ({n}){ext}"):
        n += 1
    return f"{base} ({n}){ext}"


def _move_path(source: str, src: str, dest: str) -> None:
    if _is_remote(source):
        try:
            mi.rclone.move(source, src, dest)
        except mi.RcloneError as exc:
            raise ValueError(f"Move failed: {exc.stderr[:300]}")
        return
    os.makedirs(long_path(os.path.dirname(dest)), exist_ok=True)
    shutil.move(long_path(src), long_path(dest))


def _base_name(source: str, path: str) -> str:
    return path.rsplit("/", 1)[-1] if _is_remote(source) else os.path.basename(path)


def organize_plan(db: sqlite3.Connection, source: str,
                  skip_documents: bool = True) -> dict:
    """Preview: top-bucket counts for a source. Pure index query, no disk access."""
    rows = db.execute(
        "SELECT path, kind, category, ext FROM files WHERE source = ?",
        (source,)).fetchall()
    counts = {b: 0 for b in ORGANIZE_BUCKET_NAMES}
    skipped = 0
    for r in rows:
        bucket = _top_bucket(r["kind"], r["category"], r["ext"], skip_documents)
        if not bucket:
            continue
        if _already_sorted(source, r["path"]):
            skipped += 1
        else:
            counts[bucket] += 1
    return {"ok": True, "source": source, "total": sum(counts.values()),
            "skipped": skipped, "buckets": counts}


def list_batches(db: sqlite3.Connection) -> list:
    rows = db.execute(
        "SELECT batch_id, source, COUNT(*) AS total, SUM(undone) AS undone, "
        "MIN(moved_at) AS started FROM media_org_moves "
        "GROUP BY batch_id ORDER BY started DESC").fetchall()
    return [{"batch_id": r["batch_id"], "source": r["source"],
             "total": r["total"], "undone": r["undone"] or 0,
             "started": r["started"]} for r in rows]


class FileGoneError(Exception):
    """The file is already gone from disk, so there's nothing to trash —
    the stale index row should just be pruned."""


_now = now_iso   # kept as a local alias; many call sites use _now()


def _row_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


class FileSessionManager:
    """Per-browser-session trash bin for restore-before-end-of-session."""

    def __init__(self):
        self._lock = threading.Lock()
        self._trash: dict[str, list[dict]] = {}

    def list_trash(self, session_id: str) -> list[dict]:
        with self._lock:
            return list(self._trash.get(session_id, []))

    def empty_trash(self, session_id: str) -> dict:
        """Permanently delete every trashed file for this session and clear the
        restore list. Irreversible — the index rows are already gone, so this
        only frees the disk/remote space the trash bin was holding."""
        with self._lock:
            entries = self._trash.get(session_id, [])
            self._trash[session_id] = []   # we own these files now; drop them
        removed, errors = 0, []
        remote_sources = set()
        for entry in entries:
            source = entry["source"]
            if source in ("local", "onedrive"):
                try:
                    tp = entry["trash_path"]
                    if exists_on_disk(tp):
                        os.remove(long_path(tp))
                    removed += 1
                except OSError as exc:
                    errors.append(f"{entry.get('name', '?')}: {exc}")
            else:
                # Remote entries are purged per-source in one shot below.
                remote_sources.add(source)
                removed += 1
        # Best-effort: drop the now-empty local session trash tree.
        shutil.rmtree(TRASH_ROOT / session_id, ignore_errors=True)
        # Purge each remote's per-session trash folder in a single rclone call.
        for source in remote_sources:
            try:
                mi.rclone.purge(source, f".indexhub-trash/{session_id}")
            except mi.RcloneError as exc:
                errors.append(f"{source}: {exc.stderr[:200]}")
            except OSError as exc:
                errors.append(f"{source}: {exc}")
        return {"ok": True, "removed": removed, "errors": errors}

    def _add_entry(self, session_id: str, entry: dict):
        with self._lock:
            self._trash.setdefault(session_id, []).append(entry)

    def _pop_entry(self, session_id: str, entry_id: str) -> dict | None:
        with self._lock:
            items = self._trash.get(session_id, [])
            for i, item in enumerate(items):
                if item["entry_id"] == entry_id:
                    return items.pop(i)
            return None

    def mark_for_deletion(self, db: sqlite3.Connection, file_id: str, marked: bool) -> dict:
        row = db.execute("SELECT id, marked_delete FROM files WHERE id = ?", (file_id,)).fetchone()
        if not row:
            raise ValueError("File not found in index")
        flag = 1 if marked else 0
        db.execute("UPDATE files SET marked_delete = ? WHERE id = ?", (flag, file_id))
        db.commit()
        return {"ok": True, "marked_delete": bool(flag)}

    def delete_file(self, db: sqlite3.Connection, session_id: str, file_id: str) -> dict:
        row = db.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        if not row:
            raise ValueError("File not found in index")
        entry_id = uuid.uuid4().hex[:12]
        snapshot = _row_dict(row)
        # Delete the index row first (uncommitted), move the file second:
        # if the move fails we roll back and nothing changed; the move is the
        # step that can't be rolled back, so it must come last.
        # Move the file to trash BEFORE touching the DB: a remote (rclone) move
        # can take minutes, and holding a write lock across it starves other
        # writers (the hash pass, other deletes) into "database is locked".
        try:
            trash_path = self._move_to_trash(session_id, entry_id, row)
        except FileGoneError:
            # Already gone from disk — just prune the stale index row.
            db.execute("DELETE FROM files WHERE id = ?", (file_id,))
            db.commit()
            return {"ok": True, "pruned": True, "name": row["name"],
                    "source": row["source"], "original_path": row["path"]}
        # File is safely in trash; now remove the index row (a brief write).
        db.execute("DELETE FROM files WHERE id = ?", (file_id,))
        db.commit()
        entry = {
            "entry_id": entry_id,
            "deleted_at": _now(),
            "original_path": row["path"],
            "trash_path": trash_path,
            "name": row["name"],
            "source": row["source"],
            "snapshot": snapshot,
        }
        self._add_entry(session_id, entry)
        return {"ok": True, "entry": {
            "entry_id": entry_id,
            "name": row["name"],
            "source": row["source"],
            "original_path": row["path"],
            "deleted_at": entry["deleted_at"],
        }}

    def restore_file(self, db: sqlite3.Connection, session_id: str, entry_id: str) -> dict:
        entry = self._pop_entry(session_id, entry_id)
        if not entry:
            raise ValueError("Trash entry not found or already restored")
        snap = entry["snapshot"]
        cols = [k for k in snap if k != "id"]
        placeholders = ", ".join("?" * len(cols))
        col_names = ", ".join(cols)
        # Same ordering as delete_file: stage the row insert, move the file,
        # commit only once the move succeeded.
        db.execute(
            f"INSERT OR REPLACE INTO files ({col_names}) VALUES ({placeholders})",
            [snap[c] for c in cols],
        )
        try:
            self._restore_from_trash(entry)
        except BaseException:
            db.rollback()
            self._add_entry(session_id, entry)
            raise
        db.commit()
        return {"ok": True, "file_id": snap["id"], "path": snap["path"], "name": snap["name"]}

    def move_file(self, db: sqlite3.Connection, file_id: str, dest_dir: str) -> dict:
        row = db.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        if not row:
            raise ValueError("File not found in index")
        dest_dir = (dest_dir or "").strip()
        if not dest_dir:
            raise ValueError("Destination folder is required")
        new_path = self._dest_path(row, dest_dir)
        new_name = Path(new_path).name if row["source"] in ("local", "onedrive") else PurePosixPath(new_path).name
        new_ext = new_name.rsplit(".", 1)[-1].lower() if "." in new_name else row["ext"]
        # Stage the index update, move on disk, commit only after the move
        # succeeds — a locked/failed step can no longer leave the file moved
        # but the index stale (or vice versa).
        db.execute(
            "UPDATE files SET path = ?, name = ?, ext = ?, marked_delete = 0 WHERE id = ?",
            (new_path, new_name, new_ext, file_id),
        )
        try:
            self._move_on_disk(row, dest_dir)
        except BaseException:
            db.rollback()
            raise
        db.commit()
        return {"ok": True, "path": new_path, "name": new_name, "ext": new_ext}

    def _session_trash_dir(self, session_id: str) -> Path:
        path = TRASH_ROOT / session_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _move_to_trash(self, session_id: str, entry_id: str, row: sqlite3.Row) -> str:
        source = row["source"]
        name = row["name"]
        if not _is_remote(source):
            if not exists_on_disk(row["path"]):
                raise FileGoneError("File missing on disk")
            dest = self._session_trash_dir(session_id) / source / f"{entry_id}_{name}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(long_path(row["path"]), str(dest))
            return str(dest)
        trash_rel = f".indexhub-trash/{session_id}/{entry_id}_{name}"
        try:
            mi.rclone.move(source, row["path"], trash_rel)
        except mi.RcloneError as exc:
            if mi.rclone.is_missing(exc.stderr):
                raise FileGoneError("File missing on remote")
            raise ValueError(f"Delete failed: {exc.stderr[:300]}")
        return trash_rel

    def _restore_from_trash(self, entry: dict):
        source = entry["source"]
        if not _is_remote(source):
            src = Path(entry["trash_path"])
            dest = Path(entry["original_path"])
            if not src.is_file():
                raise ValueError("Trashed file missing; cannot restore")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if exists_on_disk(entry["original_path"]):
                raise ValueError("Cannot restore: original path already occupied")
            shutil.move(str(src), long_path(entry["original_path"]))
            return
        try:
            mi.rclone.move(source, entry["trash_path"], entry["original_path"])
        except mi.RcloneError as exc:
            raise ValueError(f"Restore failed: {exc.stderr[:300]}")

    @staticmethod
    def _dest_path(row: sqlite3.Row, dest_dir: str) -> str:
        if not _is_remote(row["source"]):
            return str(Path(dest_dir) / Path(row["path"]).name)
        dest_posix = dest_dir.replace("\\", "/").strip("/")
        name = PurePosixPath(row["path"]).name
        return f"{dest_posix}/{name}" if dest_posix else name

    def _move_on_disk(self, row: sqlite3.Row, dest_dir: str) -> str:
        source = row["source"]
        if not _is_remote(source):
            src = Path(row["path"])
            if not Path(dest_dir).is_dir():
                raise ValueError("Destination folder does not exist")
            dest = Path(self._dest_path(row, dest_dir))
            if dest.exists():
                raise ValueError("A file with that name already exists in the destination")
            shutil.move(str(src), str(dest))
            return str(dest)
        new_rel = self._dest_path(row, dest_dir)
        try:
            mi.rclone.move(source, row["path"], new_rel)
        except mi.RcloneError as exc:
            label = "Google Drive" if source == "gdrive" else "QNAP"
            raise ValueError(f"{label} move failed: {exc.stderr[:300]}")
        return new_rel


file_sessions = FileSessionManager()


class DeleteJobManager(BaseJobManager):
    """Runs file deletions on a single background worker so the UI returns
    immediately and a slow remote (rclone) move doesn't block the request.
    A status snapshot drives the progress bar in the web UI."""

    def __init__(self):
        super().__init__()
        self._queue: list[tuple[str, str]] = []   # (session_id, file_id)

    @staticmethod
    def _idle() -> dict:
        return {"running": False, "total": 0, "deleted": 0, "pruned": 0,
                "failed": 0, "current": "", "started_at": None,
                "finished_at": None, "errors": [], "cancelled": False}

    def status(self) -> dict:
        with self._lock:
            s = dict(self._state)
            s["queued"] = len(self._queue)
            return s

    def cancel(self) -> bool:
        """Drop everything still queued. The file currently being moved is
        allowed to finish (a remote move can't be safely interrupted)."""
        with self._lock:
            if not self._state["running"]:
                return False
            self._queue = []
            self._state["cancelled"] = True
            return True

    def enqueue(self, session_id: str, ids: list) -> int:
        ids = [str(i) for i in ids]
        with self._lock:
            if not self._state["running"]:
                # Fresh batch: reset the counters so the bar starts at 0.
                self._state = self._idle()
                self._state.update(running=True, total=len(ids), started_at=_now())
                self._queue = [(session_id, i) for i in ids]
                self._launch(self._run)
            else:
                self._queue.extend((session_id, i) for i in ids)
                self._state["total"] += len(ids)
            return self._state["total"]

    def _run(self):
        db = mi.get_db()
        db.row_factory = sqlite3.Row  # delete_file reads rows by column name
        try:
            while True:
                with self._lock:
                    if not self._queue:
                        break
                    session_id, fid = self._queue.pop(0)
                try:
                    res = file_sessions.delete_file(db, session_id, fid)
                    name = res.get("name") or res.get("entry", {}).get("name", "")
                    self._inc("pruned" if res.get("pruned") else "deleted")
                    self._set(current=name)
                except Exception as exc:
                    self._err(str(exc))
        finally:
            db.close()
            self._finish(current="")


delete_jobs = DeleteJobManager()


class OrganizeJobManager(BaseJobManager):
    """Runs a Media Org organize-or-undo pass on a single background worker.
    Mirrors DeleteJobManager: one job at a time, a status snapshot for the
    progress bar, and a cancel that stops the queue (the in-flight move
    finishes — a remote move can't be safely interrupted)."""

    @staticmethod
    def _idle() -> dict:
        return {"running": False, "mode": "", "source": "", "batch_id": "",
                "total": 0, "moved": 0, "skipped": 0, "failed": 0,
                "current": "", "started_at": None, "finished_at": None,
                "errors": [], "cancelled": False}

    def cancel(self) -> bool:
        with self._lock:
            if not self._state["running"]:
                return False
            self._state["cancelled"] = True
            return True

    def enqueue_organize(self, source: str, skip_documents: bool = True) -> str:
        batch_id = f"{source}-{_now()}-{uuid.uuid4().hex[:8]}"
        with self._lock:
            if self._state["running"]:
                raise ValueError("A Media Org job is already running")
            self._state = self._idle()
            self._state.update(running=True, mode="organize", source=source,
                               batch_id=batch_id, started_at=_now())
            self._launch(self._run_organize, (source, batch_id, skip_documents))
        return batch_id

    def enqueue_undo(self, batch_id: str) -> None:
        with self._lock:
            if self._state["running"]:
                raise ValueError("A Media Org job is already running")
            self._state = self._idle()
            self._state.update(running=True, mode="undo", batch_id=batch_id,
                               started_at=_now())
            self._launch(self._run_undo, (batch_id,))

    def _run_organize(self, source: str, batch_id: str, skip_documents: bool = True):
        db = mi.get_db()
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                "SELECT id, path, name, kind, category, ext, modified "
                "FROM files WHERE source = ?", (source,)).fetchall()
            todo = [r for r in rows
                    if _top_bucket(r["kind"], r["category"], r["ext"], skip_documents)
                    and not _already_sorted(source, r["path"])]
            self._set(total=len(todo))
            for r in todo:
                if self._cancelled():
                    break
                self._set(current=r["name"])
                src = r["path"]
                if not _is_remote(source):
                    if not exists_on_disk(src) or is_cloud_placeholder(src):
                        self._inc("skipped")
                        continue
                try:
                    bucket = _top_bucket(r["kind"], r["category"], r["ext"], skip_documents)
                    subdirs = [_prev_location(source, src), _year_of(r["modified"])]
                    dest = _uniquify(source, _dest_path(source, bucket, subdirs, r["name"]))
                    _move_path(source, src, dest)
                    name = _base_name(source, dest)
                    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                    # Record the move first so undo always has the new_path, then
                    # point the index at the new location. If the log write can't
                    # land (db locked past every retry) the move would be
                    # unrecoverable — so put the file back and count it as failed.
                    if not mi._write_with_retry(db,
                            "INSERT INTO media_org_moves (batch_id, source, file_id, "
                            "original_path, new_path, moved_at, undone) VALUES (?,?,?,?,?,?,0)",
                            (batch_id, source, str(r["id"]), src, dest, _now())):
                        try:
                            _move_path(source, dest, src)
                            self._err(f"{r['name']}: could not record move (db locked); left in place")
                        except Exception as exc:
                            self._err(f"{r['name']}: move not recorded and revert failed: {exc}")
                        continue
                    if not mi._write_with_retry(db,
                            "UPDATE files SET path=?, name=?, ext=? WHERE id=?",
                            (dest, name, ext, r["id"])):
                        self._err(f"{r['name']}: moved and logged, but index update failed (db locked)")
                        continue
                    self._inc("moved")
                except Exception as exc:
                    self._err(f"{r['name']}: {exc}")
        finally:
            db.close()
            self._finish(current="")

    def _run_undo(self, batch_id: str):
        db = mi.get_db()
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                "SELECT id, source, file_id, original_path, new_path "
                "FROM media_org_moves WHERE batch_id=? AND undone=0", (batch_id,)).fetchall()
            self._set(total=len(rows), source=(rows[0]["source"] if rows else ""))
            for r in rows:
                if self._cancelled():
                    break
                source = r["source"]
                self._set(current=_base_name(source, r["new_path"]))
                try:
                    if not _is_remote(source) and not exists_on_disk(r["new_path"]):
                        # Already gone from the sorted location; just clear the log.
                        mi._write_with_retry(db,
                            "UPDATE media_org_moves SET undone=1 WHERE id=?", (r["id"],))
                        self._inc("skipped")
                        continue
                    _move_path(source, r["new_path"], r["original_path"])
                    name = _base_name(source, r["original_path"])
                    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                    mi._write_with_retry(db,
                        "UPDATE files SET path=?, name=?, ext=? WHERE id=?",
                        (r["original_path"], name, ext, int(r["file_id"])))
                    if not mi._write_with_retry(db,
                            "UPDATE media_org_moves SET undone=1 WHERE id=?", (r["id"],)):
                        self._err(f"undo {r['new_path']}: restored but undo-flag not saved (db locked)")
                        continue
                    self._inc("moved")
                except Exception as exc:
                    self._err(f"undo {r['new_path']}: {exc}")
        finally:
            db.close()
            self._finish(current="")


organize_jobs = OrganizeJobManager()


def parse_session_id(cookie_header: str | None) -> tuple[str, bool]:
    """Return (session_id, is_new)."""
    if cookie_header:
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith("indexhub_session="):
                sid = part.split("=", 1)[1].strip()
                if sid:
                    return sid, False
    return uuid.uuid4().hex, True
