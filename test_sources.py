"""Source-adapter tests: registry behaviour + a real end-to-end local scan.

Verifies that the deepened Source seam preserves the behaviour the old scattered
`source ==` branches had, and that a full scan_sources('local') run still indexes
files through LocalSource.scan -> run_scan. Temp DB + temp home, no network.
"""
import sqlite3, tempfile
from pathlib import Path

import media_index as mi

# 1. Registry: names + is_remote (gdrive AND qnap are remote)
assert mi.get_source("local").is_remote is False
assert mi.get_source("onedrive").is_remote is False
assert mi.get_source("gdrive").is_remote is True
assert mi.get_source("qnap").is_remote is True
try:
    mi.get_source("dropbox"); assert False, "unknown source should raise"
except ValueError:
    pass
print("PASS: registry names + is_remote")

# 2. reveal_path: local normpath, gdrive None (not browsable), unknown None
assert mi.reveal_path("local", "C:/a/b/../c") == __import__("os").path.normpath("C:/a/b/../c")
assert mi.reveal_path("gdrive", "x/y.mp4") is None
assert mi.reveal_path("nope", "x") is None
print("PASS: reveal_path delegation")

# 3. rclone_full_path: gdrive prefixes the remote; non-remote raises ValueError
assert mi.rclone_full_path("gdrive", "movies/a.mp4") == f"{mi.GDRIVE_REMOTE}movies/a.mp4"
for bad in ("local", "onedrive"):
    try:
        mi.rclone_full_path(bad, "x"); assert False
    except ValueError:
        pass
print("PASS: rclone_full_path delegation")

# 4. hash_file_row delegates to a real local hash; unknown source -> None
tmp = Path(tempfile.mkdtemp())
f = tmp / "h.bin"; f.write_bytes(b"hello indexhub")
assert mi.hash_file_row("local", str(f)) == mi.hash_local_file(str(f))
assert mi.hash_file_row("bogus", str(f)) is None
print("PASS: hash_file_row delegation")

# 5. device_for_source: local -> machine identity; unknown -> ('unknown', name)
assert mi.device_for_source("local") == mi.get_machine_identity()
assert mi.device_for_source("zzz") == ("unknown", "zzz")
print("PASS: device_for_source delegation")

# 6. REAL end-to-end local scan through scan_sources -> LocalSource.scan -> run_scan
home = Path(tempfile.mkdtemp())
mi.DB_PATH = home / "e2e.db"
mi.USER = home
mi.LOCAL_ROOTS = [home]
mi.ONEDRIVE_ROOT = home / "OneDrive"   # absent, so it just won't match
mi._schema_ready = False
(home / "Videos").mkdir()
(home / "Videos" / "clip.mp4").write_bytes(b"x")
(home / "song.mp3").write_bytes(b"x")
(home / "skip.xyz").write_bytes(b"x")   # unknown extension -> not classified

result = mi.scan_sources(["local"], hash_missing=False)
db = mi.get_db(); db.row_factory = sqlite3.Row
names = sorted(r["name"] for r in db.execute("SELECT name FROM files WHERE source='local'"))
assert names == ["clip.mp4", "song.mp3"], names
assert result["sources"]["local"]["files"] == 2, result
print("PASS: real local scan indexed", names)

print("ALL PASS")
