"""Rclone adapter tests — all OFFLINE (no rclone binary, no remote).

Part A stubs Rclone.run to exercise the adapter's command handling, parsing, and
error semantics. Part B is the payoff: it drives a remote (gdrive) delete -> restore
through FileSessionManager with a fake in-memory remote, something that previously
required a live NAS to test at all.
"""
import subprocess, sqlite3, tempfile
from pathlib import Path

import media_index as mi


def proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def stub_run(handler):
    mi.rclone.run = lambda args, **kw: handler(args, **kw)


# --- Part A: adapter behaviour -------------------------------------------------
stub_run(lambda a, **k: proc(0))
mi.rclone.move("gdrive", "a.mp4", "b.mp4")          # success: no raise
print("PASS: move success")

stub_run(lambda a, **k: proc(1, stderr="  quota exceeded  "))
try:
    mi.rclone.move("gdrive", "a", "b"); assert False
except mi.RcloneError as e:
    assert e.stderr == "quota exceeded", repr(e.stderr)
print("PASS: move failure -> RcloneError(stderr trimmed)")

assert mi.rclone.is_missing("directory not found") is True
assert mi.rclone.is_missing("Permission denied") is False
print("PASS: is_missing detection")

stub_run(lambda a, **k: proc(0, stdout="clip.mp4\n"))
assert mi.rclone.exists("gdrive", "clip.mp4") is True
stub_run(lambda a, **k: proc(0, stdout="   \n"))
assert mi.rclone.exists("gdrive", "clip.mp4") is False
stub_run(lambda a, **k: proc(1))
assert mi.rclone.exists("gdrive", "clip.mp4") is False
print("PASS: exists")

stub_run(lambda a, **k: proc(1, stderr="path does not exist"))
mi.rclone.purge("gdrive", "x")                      # missing tolerated: no raise
stub_run(lambda a, **k: proc(1, stderr="i/o error"))
try:
    mi.rclone.purge("gdrive", "x"); assert False
except mi.RcloneError:
    pass
print("PASS: purge tolerates missing, raises real errors")

H = "a" * 64
stub_run(lambda a, **k: proc(0, stdout=f"{H}  movie.mp4\n"))
assert mi.rclone.hash_sha256("gdrive", "movie.mp4") == H
stub_run(lambda a, **k: proc(1))
assert mi.rclone.hash_sha256("gdrive", "x") is None
print("PASS: hash_sha256 parse + failure")

stub_run(lambda a, **k: proc(0, stdout='[{"Name":"a","ID":"XYZ","IsDir":false}]'))
assert mi.rclone.lsjson("gdrive", "a")[0]["ID"] == "XYZ"
stub_run(lambda a, **k: proc(1))
assert mi.rclone.lsjson("gdrive", "a") is None
assert mi.gdrive_web_url("a") is None               # non-zero exit -> no URL
stub_run(lambda a, **k: proc(0, stdout='[{"Name":"a","ID":"XYZ","IsDir":false}]'))
assert mi.gdrive_web_url("a") == "https://drive.google.com/file/d/XYZ/view"
print("PASS: lsjson + gdrive_web_url")

# --- Part B: remote delete -> restore through FileSessionManager, OFFLINE ------
tmp = Path(tempfile.mkdtemp()); mi.DB_PATH = tmp / "r.db"; mi._schema_ready = False
import file_ops

remote = {"movies/clip.mp4"}                          # fake gdrive contents

def fake_move(source, src, dst):
    if src not in remote:
        raise mi.RcloneError("directory not found")
    remote.discard(src); remote.add(dst)

mi.rclone.move = fake_move
mi.rclone.exists = lambda source, rel: rel in remote

db = mi.get_db(); db.row_factory = sqlite3.Row
db.execute("INSERT INTO files (source,path,name,ext,kind,size,modified,category,"
           "device_id,device_label,scanned_at) VALUES "
           "('gdrive','movies/clip.mp4','clip.mp4','mp4','video',10,'2024-01-01',"
           "'video','gdrive-shared','Google Drive remote','2026-01-01')")
db.commit()
fid = db.execute("SELECT id FROM files").fetchone()[0]

res = file_ops.file_sessions.delete_file(db, "sess", str(fid))
entry_id = res["entry"]["entry_id"]
assert db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0      # index row gone
assert "movies/clip.mp4" not in remote                                  # moved out of place
assert any(p.startswith(".indexhub-trash/sess/") for p in remote)       # now in remote trash
print("PASS: remote delete moved file to trash + dropped index row (offline)")

file_ops.file_sessions.restore_file(db, "sess", entry_id)
assert "movies/clip.mp4" in remote                                      # back where it was
assert db.execute("SELECT COUNT(*) FROM files WHERE path='movies/clip.mp4'"
                  ).fetchone()[0] == 1                                  # index row restored
print("PASS: remote restore moved file back + reinserted index row (offline)")

print("ALL PASS")
