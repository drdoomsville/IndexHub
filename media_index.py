#!/usr/bin/env python3
"""Media Index: catalog media files across local disk, OneDrive, and Google Drive.

Usage:
  python media_index.py scan [local] [onedrive] [gdrive]   # scan sources (default: all)
  python media_index.py search <term> [--kind video|audio|image] [--source ...] [--limit N]
  python media_index.py stats
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "media_index.db"
USER = Path.home()
ONEDRIVE_ROOT = USER / "OneDrive"
LOCAL_ROOTS = [
    USER / "Videos",
    USER / "Pictures",
    USER / "Music",
    USER / "Downloads",
    USER / "Documents",
    USER / "Desktop",
]
GDRIVE_REMOTE = "gdrive:"


def find_rclone() -> str:
    found = shutil.which("rclone")
    if found:
        return found
    winget_packages = Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "WinGet" / "Packages"
    matches = list(winget_packages.glob("Rclone.Rclone_*/**/rclone.exe"))
    if matches:
        return str(matches[0])
    raise RuntimeError("rclone not found; install it or add it to PATH")

EXTENSIONS = {
    "video": {
        "mp4", "mkv", "mov", "avi", "wmv", "flv", "webm", "m4v",
        "mpg", "mpeg", "ts", "m2ts", "3gp", "vob",
    },
    "audio": {
        "mp3", "flac", "m4a", "wav", "aac", "ogg", "wma", "opus",
        "aiff", "aif", "alac", "mid", "midi",
    },
    "image": {
        "jpg", "jpeg", "png", "gif", "bmp", "heic", "heif", "webp",
        "tiff", "tif", "raw", "cr2", "cr3", "nef", "arw", "dng",
        "orf", "rw2", "svg", "psd",
    },
    "document": {
        "txt", "md", "markdown", "rtf", "log",
        "json", "xml", "yaml", "yml", "csv", "tsv",
        "doc", "docx", "odt",
        "xls", "xlsx", "xlsm", "ods",
        "ppt", "pptx", "odp",
        "pdf",
    },
}
EXT_TO_KIND = {ext: kind for kind, exts in EXTENSIONS.items() for ext in exts}

# Document sub-categories (stored in the same `category` column as photo/graphic).
DOC_CATEGORIES = {
    "text": {"txt", "md", "markdown", "rtf", "log"},
    "data": {"json", "xml", "yaml", "yml", "csv", "tsv"},
    "word": {"doc", "docx", "odt"},
    "spreadsheet": {"xls", "xlsx", "xlsm", "ods"},
    "presentation": {"ppt", "pptx", "odp"},
    "pdf": {"pdf"},
}
EXT_TO_DOC_CATEGORY = {ext: cat for cat, exts in DOC_CATEGORIES.items() for ext in exts}

# Directories that are all noise for a personal file index.
EXCLUDE_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv",
                "$recycle.bin", ".vs", "obj", "bin"}

# --- photo vs computer-image (graphic) detection -------------------------------

# Camera raw / phone formats are always real photos.
CAMERA_EXTS = {"heic", "heif", "cr2", "cr3", "nef", "arw", "dng", "orf", "rw2", "raw"}
# Formats that are essentially never camera output.
GRAPHIC_EXTS = {"svg", "psd", "gif", "bmp", "webp"}

# Camera/phone naming conventions (IMG_1234, DSC0001, PXL_, 20190728_165647,
# WhatsApp IMG-20190728-WA0001, FB_IMG_, burst/portrait shots, etc.)
PHOTO_NAME_RE = re.compile(
    r"^(IMG[_-]?\d|DSC[NF]?[_-]?\d|PXL_\d|\d{8}[_-]\d{6}|IMG-\d{8}-WA\d|"
    r"FB_IMG_\d|received_\d|signal-\d|GOPR\d|DJI[_-]\d|P\d{7}|100_\d{4})",
    re.IGNORECASE)
SCREENSHOT_NAME_RE = re.compile(
    r"screen[\s_-]?shot|screenshot|screen[\s_-]?cap|screencap|snip|clipboard|"
    r"untitled|^image[\s_-]?\(?\d*\)?\.|thumbnail|logo|icon|banner|wallpaper|"
    r"meme|diagram|chart|mockup|qr[\s_-]?code",
    re.IGNORECASE)
PHOTO_PATH_RE = re.compile(r"camera roll|dcim|camera|photos?[\\/]|takeout", re.IGNORECASE)
GRAPHIC_PATH_RE = re.compile(
    r"screenshots?|captures?|icons?|assets|wallpapers?|clipart|emoji|stickers?",
    re.IGNORECASE)

# Windows Files On-Demand placeholder: reading content would trigger a download.
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000


def _parse_tiff_exif(tiff: bytes) -> dict:
    """Minimal TIFF parse. Returns has_camera_tags, make, model, datetime_original."""
    out: dict = {}
    try:
        if tiff[:2] == b"II":
            bo = "little"
        elif tiff[:2] == b"MM":
            bo = "big"
        else:
            return out

        def u16(o):
            return int.from_bytes(tiff[o:o + 2], bo)

        def u32(o):
            return int.from_bytes(tiff[o:o + 4], bo)

        def read_ifd(off):
            entries = {}
            for i in range(u16(off)):
                pos = off + 2 + i * 12
                entries[u16(pos)] = (u16(pos + 2), u32(pos + 4), pos + 8)
            return entries

        def ascii_val(typ, cnt, vpos):
            if typ != 2 or cnt == 0:
                return None
            off = vpos if cnt <= 4 else u32(vpos)
            raw = tiff[off:off + cnt].split(b"\x00")[0]
            return raw.decode("ascii", "replace").strip() or None

        ifd0 = read_ifd(u32(4))
        out["has_camera_tags"] = any(t in ifd0 for t in (0x010F, 0x0110, 0x8769))
        if 0x010F in ifd0:
            out["make"] = ascii_val(*ifd0[0x010F])
        if 0x0110 in ifd0:
            out["model"] = ascii_val(*ifd0[0x0110])
        if 0x8769 in ifd0:  # pointer to the Exif sub-IFD
            exif_ifd = read_ifd(u32(ifd0[0x8769][2]))
            if 0x9003 in exif_ifd:  # DateTimeOriginal "YYYY:MM:DD HH:MM:SS"
                out["datetime_original"] = ascii_val(*exif_ifd[0x9003])
    except (IndexError, ValueError):
        pass
    return out


def read_jpeg_exif(path: str) -> dict | None:
    """EXIF dict for a JPEG ({} if none present), or None if unreadable/cloud-only."""
    try:
        if os.name == "nt":
            attrs = os.stat(path).st_file_attributes
            if attrs & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS:
                return None  # cloud-only placeholder; don't trigger a download
        with open(path, "rb") as f:
            if f.read(2) != b"\xff\xd8":
                return None
            while True:
                marker = f.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    return {}
                if marker[1] == 0xDA:  # start of image data; no EXIF found
                    return {}
                size = int.from_bytes(f.read(2), "big")
                if size < 2:
                    return {}
                segment = f.read(size - 2)
                if marker[1] == 0xE1 and segment[:6] == b"Exif\x00\x00":
                    return _parse_tiff_exif(segment[6:])
    except OSError:
        return None


def jpeg_has_camera_exif(path: str) -> bool | None:
    """True/False if the JPEG's EXIF could be checked, None if unreadable."""
    exif = read_jpeg_exif(path)
    if exif is None:
        return None
    return exif.get("has_camera_tags", False)


def categorize_image(path: str, name: str, ext: str, readable: bool) -> str:
    """Return 'photo' (camera shot) or 'graphic' (computer-generated image)."""
    if ext in CAMERA_EXTS:
        return "photo"
    if SCREENSHOT_NAME_RE.search(name):
        return "graphic"
    if PHOTO_NAME_RE.match(name):
        return "photo"
    if ext in GRAPHIC_EXTS:
        return "graphic"
    if GRAPHIC_PATH_RE.search(path):
        return "graphic"
    if ext == "png":
        return "graphic"  # PNGs are almost never camera output
    # Ambiguous JPEG/TIFF: check EXIF camera tags when the bytes are local.
    if readable and ext in ("jpg", "jpeg", "tif", "tiff"):
        exif = jpeg_has_camera_exif(path)
        if exif is not None:
            return "photo" if exif else "graphic"
    if PHOTO_PATH_RE.search(path):
        return "photo"
    # Unresolvable (e.g. cloud-only): JPEGs default to photo, the rest to graphic.
    return "photo" if ext in ("jpg", "jpeg", "tif", "tiff") else "graphic"


def categorize(path: str, name: str, ext: str, kind: str, readable: bool) -> str | None:
    if kind == "image":
        return categorize_image(path, name, ext, readable)
    if kind == "document":
        return EXT_TO_DOC_CATEGORY.get(ext)
    return None

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,            -- 'local' | 'onedrive' | 'gdrive'
    path TEXT NOT NULL,              -- absolute local path or gdrive-relative path
    name TEXT NOT NULL,
    ext TEXT NOT NULL,
    kind TEXT NOT NULL,              -- 'video' | 'audio' | 'image'
    size INTEGER,
    modified TEXT,                   -- ISO 8601
    category TEXT,                   -- images only: 'photo' | 'graphic'
    scanned_at TEXT NOT NULL,
    UNIQUE (source, path)
);
CREATE INDEX IF NOT EXISTS idx_files_name ON files (name);
CREATE INDEX IF NOT EXISTS idx_files_kind ON files (kind);
CREATE INDEX IF NOT EXISTS idx_files_source ON files (source);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    file_count INTEGER,
    total_bytes INTEGER
);
"""


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    cols = {row[1] for row in db.execute("PRAGMA table_info(files)")}
    if "category" not in cols:  # migrate pre-category databases
        db.execute("ALTER TABLE files ADD COLUMN category TEXT")
        db.commit()
    return db


def classify(name: str) -> str | None:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return EXT_TO_KIND.get(ext)


def walk_filesystem(roots: list[Path], exclude: Path | None = None):
    """Yield (path, name, ext, kind, size, modified_iso) for media files."""
    stack = [r for r in roots if r.is_dir()]
    while stack:
        current = stack.pop()
        try:
            entries = os.scandir(current)
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.lower() in EXCLUDE_DIRS:
                            continue
                        if exclude and Path(entry.path) == exclude:
                            continue
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        kind = classify(entry.name)
                        if kind is None:
                            continue
                        st = entry.stat(follow_symlinks=False)
                        mtime = datetime.fromtimestamp(
                            st.st_mtime, tz=timezone.utc
                        ).isoformat(timespec="seconds")
                        ext = entry.name.rsplit(".", 1)[-1].lower()
                        category = categorize(entry.path, entry.name, ext, kind, readable=True)
                        yield entry.path, entry.name, ext, kind, st.st_size, mtime, category
                except OSError:
                    continue


def scan_gdrive():
    """Yield media file rows from Google Drive via rclone lsjson."""
    proc = subprocess.run(
        [find_rclone(), "lsjson", "-R", "--files-only", "--fast-list",
         "--no-mimetype", GDRIVE_REMOTE],
        capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"rclone failed: {proc.stderr.strip()[:500]}")
    for item in json.loads(proc.stdout):
        name = item["Name"]
        kind = classify(name)
        if kind is None:
            continue
        ext = name.rsplit(".", 1)[-1].lower()
        modified = item.get("ModTime", "")
        category = categorize(item["Path"], name, ext, kind, readable=False)
        yield item["Path"], name, ext, kind, item.get("Size", -1), modified, category


def run_scan(db: sqlite3.Connection, source: str, row_iter) -> tuple[int, int]:
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    scanned_at = started
    rows = []
    count = 0
    total = 0
    t0 = time.time()
    insert_sql = (
        "INSERT OR REPLACE INTO files "
        "(source, path, name, ext, kind, size, modified, category, scanned_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")
    db.execute("DELETE FROM files WHERE source = ?", (source,))
    for path, name, ext, kind, size, modified, category in row_iter:
        rows.append((source, path, name, ext, kind, size, modified, category, scanned_at))
        count += 1
        total += max(size, 0)
        if len(rows) >= 5000:
            db.executemany(insert_sql, rows)
            rows.clear()
            print(f"  [{source}] {count:,} files so far...", flush=True)
    if rows:
        db.executemany(insert_sql, rows)
    finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO scans (source, started_at, finished_at, file_count, total_bytes) "
        "VALUES (?, ?, ?, ?, ?)", (source, started, finished, count, total))
    db.commit()
    print(f"  [{source}] done: {count:,} media files, {fmt_size(total)} in {time.time() - t0:.1f}s")
    return count, total


def fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:,.1f} {unit}"
        size /= 1024
    return f"{n} B"


def cmd_scan(args):
    sources = args.sources or ["local", "onedrive", "gdrive"]
    db = get_db()
    for source in sources:
        print(f"Scanning {source}...")
        try:
            if source == "local":
                run_scan(db, "local", walk_filesystem(LOCAL_ROOTS, exclude=ONEDRIVE_ROOT))
            elif source == "onedrive":
                run_scan(db, "onedrive", walk_filesystem([ONEDRIVE_ROOT]))
            elif source == "gdrive":
                run_scan(db, "gdrive", scan_gdrive())
            else:
                print(f"  unknown source: {source}", file=sys.stderr)
        except Exception as exc:
            print(f"  [{source}] FAILED: {exc}", file=sys.stderr)
    db.close()


def cmd_search(args):
    db = get_db()
    sql = ("SELECT source, kind, category, size, modified, path "
           "FROM files WHERE name LIKE ?")
    params: list = [f"%{args.term}%"]
    if args.kind:
        sql += " AND kind = ?"
        params.append(args.kind)
    if args.source:
        sql += " AND source = ?"
        params.append(args.source)
    if args.category:
        sql += " AND category = ?"
        params.append(args.category)
    sql += " ORDER BY name LIMIT ?"
    params.append(args.limit)
    results = db.execute(sql, params).fetchall()
    if not results:
        print("No matches.")
        return
    for source, kind, category, size, modified, path in results:
        label = category or kind
        print(f"{source:<9} {label:<8} {fmt_size(size or 0):>10}  {modified or '':<20}  {path}")
    print(f"\n{len(results)} result(s)")


def cmd_stats(args):
    db = get_db()
    rows = db.execute(
        "SELECT source, kind, COUNT(*), SUM(size) FROM files "
        "GROUP BY source, kind ORDER BY source, kind").fetchall()
    if not rows:
        print("Index is empty. Run: python media_index.py scan")
        return
    print(f"{'SOURCE':<10} {'KIND':<7} {'FILES':>10} {'SIZE':>12}")
    print("-" * 42)
    for source, kind, count, size in rows:
        print(f"{source:<10} {kind:<7} {count:>10,} {fmt_size(size or 0):>12}")
    total = db.execute("SELECT COUNT(*), SUM(size) FROM files").fetchone()
    print("-" * 42)
    print(f"{'TOTAL':<18} {total[0]:>10,} {fmt_size(total[1] or 0):>12}")
    cats = db.execute(
        "SELECT category, COUNT(*) FROM files WHERE kind='image' "
        "GROUP BY category ORDER BY category").fetchall()
    if cats:
        print("\nImage breakdown:")
        for category, count in cats:
            print(f"  {category or 'unclassified'}: {count:,}")
    last = db.execute(
        "SELECT source, MAX(finished_at) FROM scans GROUP BY source").fetchall()
    print("\nLast scans:")
    for source, when in last:
        print(f"  {source}: {when}")


def main():
    parser = argparse.ArgumentParser(description="Media Index")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="scan sources and rebuild their index")
    p_scan.add_argument("sources", nargs="*", choices=["local", "onedrive", "gdrive"],
                        help="sources to scan (default: all)")
    p_scan.set_defaults(func=cmd_scan)

    p_search = sub.add_parser("search", help="search the index by filename")
    p_search.add_argument("term")
    p_search.add_argument("--kind", choices=["video", "audio", "image", "document"])
    p_search.add_argument("--source", choices=["local", "onedrive", "gdrive"])
    p_search.add_argument("--category",
                          choices=["photo", "graphic", "text", "data", "word",
                                   "spreadsheet", "presentation", "pdf"],
                          help="image or document sub-category")
    p_search.add_argument("--limit", type=int, default=50)
    p_search.set_defaults(func=cmd_search)

    p_stats = sub.add_parser("stats", help="show index statistics")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
