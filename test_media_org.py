"""Media Org logic test: temp DB + temp files, no network.

Covers top buckets (Audio/Video/Photos/Graphics/Documents), nested
sub-buckets <bucket>/<previous-location>/<year>/, the photo/graphic split
incl. the ext-guess fallback when category is missing, skip-documents
(default on + opt-in), idempotency, and undo.
"""
import os, sqlite3, tempfile, time
from pathlib import Path

import media_index as mi

tmp = Path(tempfile.mkdtemp())
mi.DB_PATH = tmp / "test.db"
mi.USER = tmp                      # source_root('local') -> tmp
mi._schema_ready = False

import file_ops                    # imported after patching DB_PATH

# (relpath under tmp, kind, category, modified, expected dest relative to media-org)
SPECS = [
    ("Pictures/Camera Roll/IMG_1.jpg", "image", "photo",   "2024-03-02", "Photos/Camera Roll/2024/IMG_1.jpg"),
    ("Phone/DCIM/IMG_2.jpg",           "image", None,       "2024-07-01", "Photos/DCIM/2024/IMG_2.jpg"),       # ext-guess -> Photos
    ("Downloads/Screens/shot.png",     "image", "graphic",  "2025-01-09", "Graphics/Screens/2025/shot.png"),
    ("Music/Albums/song.mp3",          "audio", None,       "2019-11-20", "Audio/Albums/2019/song.mp3"),
    ("Videos/Movies/clip.mp4",         "video", None,       "2022-05-05", "Video/Movies/2022/clip.mp4"),
]
DOC = ("Documents/Invoices/doc.pdf", "document", "pdf", "2023-08-08", "Documents/Invoices/2023/doc.pdf")

db = mi.get_db(); db.row_factory = sqlite3.Row
def insert(rel, kind, cat, mod):
    p = tmp / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b"x")
    db.execute("INSERT INTO files (source,path,name,ext,kind,size,modified,category,"
               "device_id,device_label,scanned_at) VALUES "
               "('local',?,?,?,?,1,?,?,'dev','DEV','2026-01-01')",
               (str(p), p.name, p.suffix.lstrip(".").lower(), kind, mod, cat))
for rel, kind, cat, mod, _ in SPECS: insert(rel, kind, cat, mod)
insert(*DOC[:4])
db.commit()

def wait_idle():
    for _ in range(60):
        s = file_ops.organize_jobs.status()
        if not s["running"]: return s
        time.sleep(0.2)
    raise TimeoutError("job stuck")

org = tmp / "media-org"

# 1. Preview, skip-documents on (default): 5 media files, doc excluded
plan = file_ops.organize_plan(db, "local", skip_documents=True)
assert plan["total"] == 5, plan
assert plan["buckets"] == {"Audio":1,"Video":1,"Photos":2,"Graphics":1,"Documents":0}, plan
print("PASS: preview (skip docs) counts", plan["buckets"])

# 2. Organize, skip-documents on
batch = file_ops.organize_jobs.enqueue_organize("local", skip_documents=True)
s = wait_idle()
assert s["moved"] == 5 and s["failed"] == 0, s
for rel, kind, cat, mod, dest in SPECS:
    assert (org / dest).is_file(), "missing " + dest
    assert not (tmp / rel).exists(), "original still there: " + rel
assert (tmp / DOC[0]).is_file(), "document should NOT have moved"
print("PASS: organize moved 5 media files into nested buckets; document skipped")

# 3. Index updated + move log
chk = mi.get_db(); chk.row_factory = sqlite3.Row
paths = [r["path"] for r in chk.execute("SELECT path FROM files WHERE kind!='document'")]
assert all("media-org" in p for p in paths) and len(paths) == 5, paths
assert chk.execute("SELECT COUNT(*) FROM media_org_moves WHERE batch_id=?", (batch,)).fetchone()[0] == 5
print("PASS: index updated and 5 move-log rows recorded")

# 4. Idempotent re-run
assert file_ops.organize_plan(chk, "local", skip_documents=True)["total"] == 0
print("PASS: re-run skips already-sorted files")

# 5. Undo restores the 5 media files
file_ops.organize_jobs.enqueue_undo(batch); s = wait_idle()
assert s["moved"] == 5, s
for rel, *_ in SPECS:
    assert (tmp / rel).is_file(), "not restored: " + rel
print("PASS: undo restored all 5 files to original paths")

# 6. Organize with skip-documents OFF -> the document moves too
plan2 = file_ops.organize_plan(chk, "local", skip_documents=False)
assert plan2["total"] == 6 and plan2["buckets"]["Documents"] == 1, plan2
batch2 = file_ops.organize_jobs.enqueue_organize("local", skip_documents=False); s = wait_idle()
assert s["moved"] == 6, s
assert (org / DOC[4]).is_file(), "document not in Documents/Invoices/2023/"
print("PASS: skip-documents OFF moves the document to", DOC[4])
print("ALL PASS")
