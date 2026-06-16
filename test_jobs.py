"""Runtime test of the BaseJobManager subclasses: a real ScanJobManager pass and
a real DeleteJobManager batch, both driven to completion through status polling.
Temp DB + temp files, no network.
"""
import sqlite3, tempfile, time
from pathlib import Path

import media_index as mi

tmp = Path(tempfile.mkdtemp())
mi.DB_PATH = tmp / "jobs.db"
mi.USER = tmp
mi.LOCAL_ROOTS = [tmp]
mi.ONEDRIVE_ROOT = tmp / "OneDrive"
mi._schema_ready = False

import scan_jobs, file_ops


def wait(mgr, timeout=20):
    for _ in range(int(timeout / 0.1)):
        s = mgr.status()
        if not s["running"]:
            return s
        time.sleep(0.1)
    raise TimeoutError("job stuck: " + str(mgr.status()))


# --- ScanJobManager: a real local scan to completion ---
(tmp / "Music").mkdir()
(tmp / "Music" / "a.mp3").write_bytes(b"x")
(tmp / "b.mp4").write_bytes(b"x")

sj = scan_jobs.ScanJobManager()
assert sj.status()["phase"] == "idle"
assert sj.start(["local"], rescan=True, hash_missing=False) is True
assert sj.start(["local"]) is False, "second start while running must be refused"
s = wait(sj)
assert s["running"] is False and s["phase"] == "done", s
assert s["message"] == "Complete" and s["error"] is None, s
assert s["results"]["sources"]["local"]["files"] == 2, s
print("PASS: ScanJobManager ran a real scan to 'done'", s["results"]["sources"]["local"])

# --- DeleteJobManager: a real delete batch through the queue worker ---
db = mi.get_db(); db.row_factory = sqlite3.Row
rows = db.execute("SELECT id FROM files WHERE source='local'").fetchall()
ids = [r["id"] for r in rows]
assert len(ids) == 2
db.close()

dj = file_ops.DeleteJobManager()
total = dj.enqueue("sess-test", ids)
assert total == 2, total
s = wait(dj)
assert s["running"] is False and s["deleted"] + s["pruned"] == 2 and s["failed"] == 0, s
assert s["queued"] == 0, s
# the index rows are gone; the files now sit in the session trash bin
left = mi.get_db().execute("SELECT COUNT(*) FROM files WHERE source='local'").fetchone()[0]
assert left == 0, left
trash = file_ops.file_sessions.list_trash("sess-test")
assert len(trash) == 2, trash
print("PASS: DeleteJobManager deleted batch via worker; trash holds", len(trash))

print("ALL PASS")
