"""File delete (session trash), restore, move, and mark-for-deletion."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import media_index as mi

TRASH_ROOT = Path(__file__).parent / ".trash"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        db.execute("DELETE FROM files WHERE id = ?", (file_id,))
        try:
            trash_path = self._move_to_trash(session_id, entry_id, row)
        except BaseException:
            db.rollback()
            raise
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
        if source in ("local", "onedrive"):
            src = Path(row["path"])
            if not src.is_file():
                raise ValueError("File missing on disk")
            dest = self._session_trash_dir(session_id) / source / f"{entry_id}_{name}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            return str(dest)
        trash_rel = f".indexhub-trash/{session_id}/{entry_id}_{name}"
        proc = subprocess.run(
            [mi.find_rclone(), "moveto",
             mi.rclone_full_path(source, row["path"]),
             mi.rclone_full_path(source, trash_rel)],
            capture_output=True, text=True, encoding="utf-8",
        )
        if proc.returncode != 0:
            raise ValueError(f"Delete failed: {proc.stderr.strip()[:300]}")
        return trash_rel

    def _restore_from_trash(self, entry: dict):
        source = entry["source"]
        if source in ("local", "onedrive"):
            src = Path(entry["trash_path"])
            dest = Path(entry["original_path"])
            if not src.is_file():
                raise ValueError("Trashed file missing; cannot restore")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                raise ValueError("Cannot restore: original path already occupied")
            shutil.move(str(src), str(dest))
            return
        proc = subprocess.run(
            [mi.find_rclone(), "moveto",
             mi.rclone_full_path(source, entry["trash_path"]),
             mi.rclone_full_path(source, entry["original_path"])],
            capture_output=True, text=True, encoding="utf-8",
        )
        if proc.returncode != 0:
            raise ValueError(f"Restore failed: {proc.stderr.strip()[:300]}")

    @staticmethod
    def _dest_path(row: sqlite3.Row, dest_dir: str) -> str:
        if row["source"] in ("local", "onedrive"):
            return str(Path(dest_dir) / Path(row["path"]).name)
        dest_posix = dest_dir.replace("\\", "/").strip("/")
        name = PurePosixPath(row["path"]).name
        return f"{dest_posix}/{name}" if dest_posix else name

    def _move_on_disk(self, row: sqlite3.Row, dest_dir: str) -> str:
        source = row["source"]
        if source in ("local", "onedrive"):
            src = Path(row["path"])
            if not Path(dest_dir).is_dir():
                raise ValueError("Destination folder does not exist")
            dest = Path(self._dest_path(row, dest_dir))
            if dest.exists():
                raise ValueError("A file with that name already exists in the destination")
            shutil.move(str(src), str(dest))
            return str(dest)
        new_rel = self._dest_path(row, dest_dir)
        proc = subprocess.run(
            [mi.find_rclone(), "moveto",
             mi.rclone_full_path(source, row["path"]),
             mi.rclone_full_path(source, new_rel)],
            capture_output=True, text=True, encoding="utf-8",
        )
        if proc.returncode != 0:
            label = "Google Drive" if source == "gdrive" else "QNAP"
            raise ValueError(f"{label} move failed: {proc.stderr.strip()[:300]}")
        return new_rel


file_sessions = FileSessionManager()


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
