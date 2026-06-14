"""Media Org end-to-end logic test: temp DB + temp files, no network."""
import os, sqlite3, tempfile, time
from pathlib import Path

import media_index as mi

tmp = Path(tempfile.mkdtemp())
mi.DB_PATH = tmp / "test.db"
mi.USER = tmp                      # source_root('local') -> tmp
mi._schema_ready = False           # force schema init on the temp DB

import file_ops                    # imported after patching DB_PATH

# Create three real local files of different kinds, outside media-org.
src_dir = tmp / "Downloads"; src_dir.mkdir()
specs = [("song.mp3", "audio"), ("clip.mp4", "video"), ("pic.jpg", "image")]
db = mi.get_db(); db.row_factory = sqlite3.Row
for fname, kind in specs:
    p = src_dir / fname
    p.write_bytes(b"x")
    db.execute("INSERT INTO files (source,path,name,ext,kind,size,modified,"
               "device_id,device_label,scanned_at) VALUES "
               "('local',?,?,?,?,1,'2026-01-01','dev','DEV','2026-01-01')",
               (str(p), fname, fname.rsplit(".",1)[-1], kind))
db.commit()

# Preview
plan = file_ops.organize_plan(db, "local")
assert plan["total"] == 3 and plan["skipped"] == 0, plan
assert plan["buckets"] == {"Audio":1,"Video":1,"Images":1,"Documents":0}, plan
print("PASS: preview counts", plan["buckets"])

# Organize (background worker; poll until done)
batch = file_ops.organize_jobs.enqueue_organize("local")
for _ in range(60):
    s = file_ops.organize_jobs.status()
    if not s["running"]: break
    time.sleep(0.2)
assert s["moved"] == 3 and s["failed"] == 0, s
assert (tmp/"media-org"/"Audio"/"song.mp3").is_file()
assert (tmp/"media-org"/"Video"/"clip.mp4").is_file()
assert (tmp/"media-org"/"Images"/"pic.jpg").is_file()
assert not (src_dir/"song.mp3").exists()
print("PASS: organize moved 3 files into buckets")

# Index paths updated, move log written
chk = mi.get_db(); chk.row_factory = sqlite3.Row
paths = [r["path"] for r in chk.execute("SELECT path FROM files")]
assert all("media-org" in p for p in paths), paths
logs = chk.execute("SELECT * FROM media_org_moves WHERE batch_id=?", (batch,)).fetchall()
assert len(logs) == 3, logs
print("PASS: index updated and 3 move-log rows recorded")

# Idempotent re-run: nothing left to move
assert file_ops.organize_plan(chk, "local")["total"] == 0
print("PASS: re-run skips already-sorted files")

# Undo
file_ops.organize_jobs.enqueue_undo(batch)
for _ in range(60):
    s = file_ops.organize_jobs.status()
    if not s["running"]: break
    time.sleep(0.2)
assert s["moved"] == 3, s
assert (src_dir/"song.mp3").is_file() and not (tmp/"media-org"/"Audio"/"song.mp3").exists()
restored = [r["path"] for r in chk.execute("SELECT path FROM files")]
assert all("media-org" not in p for p in restored), restored
undone = chk.execute("SELECT COUNT(*) FROM media_org_moves WHERE undone=1").fetchone()[0]
assert undone == 3
print("PASS: undo restored all 3 files to original paths")
print("ALL PASS")
