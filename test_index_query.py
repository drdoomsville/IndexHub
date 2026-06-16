"""index_query tests — dedup analytics, search, and facets against a DB fixture,
with no HTTP and no web server. This is the payoff of pulling the SQL out of the
request handlers: the analytics are now exercisable directly.
"""
import sqlite3, tempfile
from pathlib import Path

import media_index as mi

tmp = Path(tempfile.mkdtemp())
mi.DB_PATH = tmp / "iq.db"
mi._schema_ready = False
db = mi.get_db(); db.row_factory = sqlite3.Row

import index_query as iq

GB = 1_000_000_000


def add(source, path, name, kind, size, modified="2024-01-01", category=None,
        chash=None, possible=0):
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    db.execute(
        "INSERT INTO files (source,path,name,ext,kind,size,modified,category,"
        "device_id,device_label,scanned_at,content_hash,possible_dupe) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (source, path, name, ext, kind, size, modified, category,
         "dev1", "PC One", "2026-01-01", chash, possible))


# Two identical-content videos on gdrive (a 200 MB exact-dup pair)
add("gdrive", "a/clip.mp4", "clip.mp4", "video", 200_000_000, "2024-05-01", chash="H1")
add("gdrive", "b/clip.mp4", "clip.mp4", "video", 200_000_000, "2024-05-02", chash="H1")
# A name-duplicate pair (same name, different content) on local
add("local", "x/song.mp3", "song.mp3", "audio", 5_000_000, "2023-02-02", chash="A")
add("local", "y/Song.MP3", "Song.MP3", "audio", 6_000_000, "2022-03-03", chash="B")
# A lone image + a document, for stats/domain checks
add("local", "p/pic.jpg", "pic.jpg", "image", 1_000_000, "2021-04-04", category="photo")
add("local", "d/notes.pdf", "notes.pdf", "document", 9000, "2020-06-06", category="pdf")
# A possible_dupe (large, unhashed) flag
add("qnap", "big.mkv", "big.mkv", "video", 3 * GB, "2024-07-07", possible=1)
db.commit()

# --- stats (media domain excludes the pdf) ---
s = iq.stats(db, "media")
assert s["total"] == 6, s            # 3 video + 2 audio + 1 image; pdf excluded
assert s["categories"].get("photo") == 1, s
docs = iq.stats(db, "documents")
assert docs["total"] == 1 and docs["categories"].get("pdf") == 1, docs
print("PASS: stats by domain")

# --- search (filters + paging) ---
r = iq.search(db, iq.SearchFilters(domain="media", kind="audio"))
assert r["total"] == 2 and all(row["kind"] == "audio" for row in r["rows"]), r
r2 = iq.search(db, iq.SearchFilters(domain="media", q="clip"))
assert r2["total"] == 2, r2
print("PASS: search filters")

# --- facets (each dimension computed with the others applied) ---
f = iq.facets(db, iq.SearchFilters(domain="media"))
assert f["kinds"].get("video") == 3 and f["kinds"].get("audio") == 2, f
assert f["sources"].get("gdrive") == 2, f
print("PASS: facets")

# --- duplicates summary ---
summ = iq.duplicates_summary(db)
assert summ["groups"] == 1, summ          # only H1 has >1 copy by content_hash
assert summ["possible"] == 1, summ
print("PASS: duplicates_summary")

# --- duplicates report (the analytics that were trapped behind HTTP) ---
rep = iq.duplicates_report(db)
assert rep["groups"] == 1 and rep["redundant"] == 1, rep
assert rep["reclaim"] == 200_000_000, rep
bucket = {b["label"]: b for b in rep["buckets"]}
assert bucket["100 MB – 1 GB"]["groups"] == 1, bucket
assert bucket["≥ 1 GB"]["groups"] == 0, bucket
assert rep["top"][0]["waste"] == 200_000_000, rep["top"]
per = {p["source"]: p for p in rep["per_source"]}
assert per["gdrive"]["groups"] == 1 and per["gdrive"]["reclaim"] == 200_000_000, per
# scoped to local -> no content-hash dup groups there
rep_local = iq.duplicates_report(db, "local")
assert rep_local["scope"] == "local" and rep_local["groups"] == 0, rep_local
print("PASS: duplicates_report analytics + scoping")

# --- duplicate_groups: name mode finds both name-identical pairs (song + clip),
# hash mode finds only the exact-content H1 group ---
name_groups = iq.duplicate_groups(db, "name", iq.DupFilters())
assert name_groups["total_groups"] == 2, name_groups   # song.mp3 pair + clip.mp4 pair
assert all(g["count"] == 2 for g in name_groups["groups"]), name_groups
# scoping name mode to local leaves only the song.mp3 pair
local_names = iq.duplicate_groups(db, "name", iq.DupFilters(source="local"))
assert local_names["total_groups"] == 1, local_names
hash_groups = iq.duplicate_groups(db, "hash", iq.DupFilters())
assert hash_groups["total_groups"] == 1, hash_groups
assert hash_groups["groups"][0]["key"] == "H1", hash_groups
# anchored on a specific file id
fid = db.execute("SELECT id FROM files WHERE content_hash='H1' LIMIT 1").fetchone()[0]
anchored = iq.duplicate_groups(db, "hash", iq.DupFilters(), file_id=str(fid))
assert anchored["total_groups"] == 1 and anchored["anchor"]["id"] == fid, anchored
print("PASS: duplicate_groups name/hash/anchored")

print("ALL PASS")
