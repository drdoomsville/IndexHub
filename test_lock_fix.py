"""Regression tests for the database-lock fix. Uses a temp DB; no real files."""
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import media_index as mi
import file_ops

tmp = Path(tempfile.mkdtemp())
mi.DB_PATH = tmp / "test.db"

ROW = ("local", str(tmp / "a.txt"), "a.txt", "txt", "doc", 1, "2026-01-01",
       None, "dev", "dev", "2026-01-01")


def seed():
    db = mi.get_db()
    db.execute("DELETE FROM files")
    db.execute(
        "INSERT INTO files (source, path, name, ext, kind, size, modified,"
        " category, device_id, device_label, scanned_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ROW)
    db.commit()
    fid = db.execute("SELECT id FROM files").fetchone()[0]
    db.close()
    return fid


def webui_db():
    conn = mi.get_db()
    conn.row_factory = sqlite3.Row
    return conn


# Test 1: writer holding a transaction no longer kills a concurrent delete.
fid = seed()
holder = sqlite3.connect(mi.DB_PATH, check_same_thread=False)
holder.execute("UPDATE files SET category = 'x' WHERE id = ?", (fid,))  # open write txn


def release():
    time.sleep(2)
    holder.commit()


threading.Thread(target=release).start()
mgr = file_ops.FileSessionManager()
mgr._move_to_trash = lambda sid, eid, row: "fake/trash/path"  # no real file involved
t0 = time.time()
res = mgr.delete_file(webui_db(), "test-session", str(fid))
elapsed = time.time() - t0
assert res["ok"], res
check = mi.get_db()
assert check.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
print(f"PASS: delete waited out a held write lock ({elapsed:.1f}s) and succeeded")
holder.close()

# Test 2: failed trash move rolls the index row back (no orphaned trash state).
fid = seed()


def boom(sid, eid, row):
    raise ValueError("simulated rclone failure")


mgr2 = file_ops.FileSessionManager()
mgr2._move_to_trash = boom
conn = webui_db()
try:
    mgr2.delete_file(conn, "test-session", str(fid))
    raise AssertionError("delete should have raised")
except ValueError:
    pass
assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
assert mgr2.list_trash("test-session") == []
print("PASS: failed move leaves index row intact and trash empty")
conn.close()

# Test 3: WAL is active on new connections.
db = mi.get_db()
mode = db.execute("PRAGMA journal_mode").fetchone()[0]
assert mode == "wal", mode
print("PASS: journal_mode=wal")
db.close()
