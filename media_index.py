#!/usr/bin/env python3
"""Media Index: catalog media files across local disk, OneDrive, Google Drive, and QNAP NAS.

Usage:
  python media_index.py scan [local] [onedrive] [gdrive] [qnap]   # scan sources (default: all configured)
  python media_index.py qnap-setup --user NAME --share Public   # save NAS credentials locally
  python media_index.py search <term> [--kind video|audio|image] [--source ...] [--limit N]
  python media_index.py stats
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
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
QNAP_CONFIG_PATH = Path(__file__).parent / "qnap_config.json"
QNAP_REMOTE = "qnap:"


def find_rclone() -> str:
    found = shutil.which("rclone")
    if found:
        return found
    winget_packages = Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "WinGet" / "Packages"
    matches = list(winget_packages.glob("Rclone.Rclone_*/**/rclone.exe"))
    if matches:
        return str(matches[0])
    raise RuntimeError("rclone not found; install it or add it to PATH")


def load_qnap_config() -> dict | None:
    if not QNAP_CONFIG_PATH.is_file():
        return None
    cfg = json.loads(QNAP_CONFIG_PATH.read_text(encoding="utf-8"))
    required = ("host", "user", "pass")
    if not all(cfg.get(k) for k in required):
        return None
    cfg.setdefault("share", "Public")
    cfg.setdefault("web_url", f"http://{cfg['host']}:8080/")
    cfg.setdefault("label", "QNAP NAS")
    return cfg


def qnap_configured() -> bool:
    return load_qnap_config() is not None


def qnap_device_identity(cfg: dict) -> tuple[str, str]:
    host = cfg["host"]
    digest = hashlib.sha256(host.encode("utf-8")).hexdigest()[:12]
    label = cfg.get("label") or f"QNAP ({host})"
    return f"qnap-{digest}", label


def ensure_qnap_rclone_remote(cfg: dict):
    """Create/update the rclone SMB remote used for QNAP scans."""
    args = [
        find_rclone(), "config", "create", "qnap", "smb",
        f"host={cfg['host']}",
        f"user={cfg['user']}",
        f"pass={cfg['pass']}",
        "port=445",
        "config_is_local=true",
    ]
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0 and "already exists" not in proc.stderr.lower():
        # Remote may exist with different settings — recreate it.
        subprocess.run(
            [find_rclone(), "config", "delete", "qnap", "config_is_local=true"],
            capture_output=True, text=True, encoding="utf-8")
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
        if proc.returncode != 0:
            raise RuntimeError(f"rclone QNAP setup failed: {proc.stderr.strip()[:500]}")


def qnap_scan_root(cfg: dict) -> str:
    share = (cfg.get("share") or "").strip("/\\")
    return f"{QNAP_REMOTE}{share}" if share else QNAP_REMOTE


def rclone_full_path(source: str, rel_path: str) -> str:
    """Build a full rclone remote path for gdrive/qnap rows."""
    if source == "gdrive":
        return f"{GDRIVE_REMOTE}{rel_path}"
    if source == "qnap":
        cfg = load_qnap_config()
        if not cfg:
            raise RuntimeError("QNAP not configured")
        root = qnap_scan_root(cfg)
        rel = rel_path.lstrip("/\\")
        return f"{root}/{rel}" if rel else root
    raise ValueError(f"not a remote source: {source}")


def reveal_path(source: str, rel_path: str) -> str | None:
    """Return a Windows filesystem path that Explorer can select for this row,
    or None if the source can't be browsed locally (e.g. Google Drive).

    local/onedrive rows are already absolute paths; qnap rows are SMB-relative
    and become a UNC path \\\\host\\share\\rel."""
    if source in ("local", "onedrive"):
        return os.path.normpath(rel_path)
    if source == "qnap":
        cfg = load_qnap_config()
        if not cfg:
            return None
        rel = rel_path.replace("/", "\\").lstrip("\\")
        # Prefer a mapped network drive (e.g. Z:) if the share root is mounted
        # locally — Explorer opens it instantly with no credential prompt.
        drive = (cfg.get("mapped_drive") or "").strip().rstrip("\\")
        if drive:
            if not drive.endswith(":"):
                drive += ":"
            return f"{drive}\\{rel}"
        share = (cfg.get("share") or "Public").strip("/\\")
        return f"\\\\{cfg['host']}\\{share}\\{rel}"
    return None


def gdrive_web_url(rel_path: str) -> str | None:
    """Resolve a gdrive file's Google Drive web URL via its Drive ID.

    Uses `rclone lsjson` (a read-only call, so it works regardless of the
    remote's write scope). Returns None if the file or its ID can't be found."""
    try:
        proc = subprocess.run(
            [find_rclone(), "lsjson", rclone_full_path("gdrive", rel_path)],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout or "[]")
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    for entry in data:
        if not entry.get("IsDir") and entry.get("ID"):
            return f"https://drive.google.com/file/d/{entry['ID']}/view"
    return None


def scan_qnap(cancel_event=None, path_prefix: str = ""):
    """Yield media file rows from QNAP via rclone SMB lsf (streaming)."""
    cfg = load_qnap_config()
    if not cfg:
        raise RuntimeError(
            "QNAP not configured. Copy qnap_config.example.json to qnap_config.json "
            "or run: python media_index.py qnap-setup --user YOUR_USER"
        )
    ensure_qnap_rclone_remote(cfg)
    root = qnap_scan_root(cfg)
    proc = subprocess.Popen(
        [find_rclone(), "lsf", "-R", "--files-only", "--fast-list",
         "--format", "tsp", "--separator", "|",
         "--filter", "- @Recently-Snapshot/**", root],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            if cancel_event is not None and cancel_event.is_set():
                break
            line = line.rstrip("\r\n")
            if not line or "|" not in line:
                continue
            modified, size_str, path = line.split("|", 2)
            if not _path_matches_prefix(path, path_prefix):
                continue
            name = path.rsplit("/", 1)[-1]
            kind = classify(name)
            if kind is None:
                continue
            ext = name.rsplit(".", 1)[-1].lower()
            try:
                size = int(size_str)
            except ValueError:
                size = -1
            category = categorize(path, name, ext, kind, readable=False)
            yield path, name, ext, kind, size, modified, category
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    rc = proc.returncode
    if rc not in (0, None, -15, 1) and not (cancel_event and cancel_event.is_set()):
        print(f"  [qnap] warning: rclone exited with code {rc}", file=sys.stderr)


HASH_CHUNK = 1024 * 1024
# Files at/over this size are skipped by the hash pass (downloading multi-GB
# files over SMB to hash them is too slow); metadata-duplicate candidates are
# flagged possible_dupe=1 instead, for manual review.
HASH_MAX_BYTES = 1_000_000_000


def compute_meta_fingerprint(size: int, modified: str, name: str, ext: str, kind: str) -> str:
    payload = f"{size}|{modified or ''}|{name.lower()}|{ext}|{kind}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def hash_local_file(path: str, cancel_event=None) -> str | None:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    return None
                chunk = handle.read(HASH_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def hash_remote_file(source: str, rel_path: str, cancel_event=None) -> str | None:
    if cancel_event is not None and cancel_event.is_set():
        return None
    remote = rclone_full_path(source, rel_path)
    proc = subprocess.run(
        [find_rclone(), "hashsum", "SHA256", "--download", remote],
        capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2 and len(parts[0]) == 64:
            return parts[0].lower()
    return None


def hash_file_row(source: str, path: str, cancel_event=None) -> str | None:
    if source in ("local", "onedrive"):
        return hash_local_file(path, cancel_event)
    if source in ("gdrive", "qnap"):
        return hash_remote_file(source, path, cancel_event)
    return None


def default_sources() -> list[str]:
    return ["local", "onedrive", "gdrive"] + (["qnap"] if qnap_configured() else [])


def _path_matches_prefix(path: str, prefix: str) -> bool:
    if not prefix:
        return True
    norm_path = path.replace("\\", "/")
    norm_prefix = prefix.replace("\\", "/").strip("/")
    if not norm_prefix:
        return True
    return norm_path == norm_prefix or norm_path.startswith(norm_prefix + "/") or norm_path.startswith(norm_prefix + "\\")


def _filter_rows(row_iter, path_prefix: str = "", cancel_event=None):
    for row in row_iter:
        if cancel_event is not None and cancel_event.is_set():
            break
        if _path_matches_prefix(row[0], path_prefix):
            yield row


def cmd_qnap_setup(args):
    password = args.password
    if not password:
        import getpass
        password = getpass.getpass("QNAP password: ")
    if not password:
        raise SystemExit("Password is required.")
    cfg = {
        "host": args.host,
        "web_url": args.web_url,
        "share": args.share,
        "user": args.user,
        "pass": password,
        "label": args.label,
    }
    QNAP_CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    ensure_qnap_rclone_remote(cfg)
    print(f"Saved {QNAP_CONFIG_PATH}")
    print(f"Scan with: python media_index.py scan qnap")


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


def get_machine_identity() -> tuple[str, str]:
    """Stable ID + human label for this machine's local C: scan context."""
    host = socket.gethostname()
    machine_guid = ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            machine_guid = winreg.QueryValueEx(key, "MachineGuid")[0]
    except Exception:
        machine_guid = ""

    serial = "unknown"
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            vol_name = ctypes.create_unicode_buffer(261)
            fs_name = ctypes.create_unicode_buffer(261)
            serial_num = ctypes.c_ulong(0)
            max_comp = ctypes.c_ulong(0)
            flags = ctypes.c_ulong(0)
            ok = kernel32.GetVolumeInformationW(
                ctypes.c_wchar_p("C:\\"),
                vol_name,
                261,
                ctypes.byref(serial_num),
                ctypes.byref(max_comp),
                ctypes.byref(flags),
                fs_name,
                261,
            )
            if ok:
                serial = f"{serial_num.value:08X}"
        except Exception:
            pass

    raw = f"{host}|{machine_guid}|{serial}"
    digest = hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:12]
    machine_id = f"pc-{digest}"
    machine_label = f"{host} (C:{serial})"
    return machine_id, machine_label


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
    source TEXT NOT NULL,            -- 'local' | 'onedrive' | 'gdrive' | 'qnap'
    path TEXT NOT NULL,              -- absolute local path or remote-relative path
    name TEXT NOT NULL,
    ext TEXT NOT NULL,
    kind TEXT NOT NULL,              -- 'video' | 'audio' | 'image'
    size INTEGER,
    modified TEXT,                   -- ISO 8601
    category TEXT,                   -- images/documents sub-category
    device_id TEXT,                  -- identifies scanning machine context
    device_label TEXT,               -- readable machine label
    scanned_at TEXT NOT NULL,
    UNIQUE (source, device_id, path)
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


def _is_legacy_unique(db: sqlite3.Connection) -> bool:
    for idx in db.execute("PRAGMA index_list(files)"):
        if not idx[2]:  # unique flag
            continue
        cols = [c[2] for c in db.execute(f"PRAGMA index_info({idx[1]!r})")]
        if cols == ["source", "path"]:
            return True
    return False


def _migrate_files_table(db: sqlite3.Connection):
    # Rebuild table so uniqueness includes machine identity.
    db.executescript("""
    CREATE TABLE IF NOT EXISTS files_new (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        path TEXT NOT NULL,
        name TEXT NOT NULL,
        ext TEXT NOT NULL,
        kind TEXT NOT NULL,
        size INTEGER,
        modified TEXT,
        category TEXT,
        device_id TEXT,
        device_label TEXT,
        scanned_at TEXT NOT NULL,
        UNIQUE (source, device_id, path)
    );
    INSERT INTO files_new (
        id, source, path, name, ext, kind, size, modified, category, device_id, device_label, scanned_at
    )
    SELECT
        id, source, path, name, ext, kind, size, modified, category,
        CASE WHEN source = 'gdrive' THEN 'gdrive-shared' ELSE 'legacy-machine' END,
        CASE WHEN source = 'gdrive' THEN 'Google Drive remote' ELSE 'Legacy machine (migrated)' END,
        scanned_at
    FROM files;
    DROP TABLE files;
    ALTER TABLE files_new RENAME TO files;
    CREATE INDEX IF NOT EXISTS idx_files_name ON files (name);
    CREATE INDEX IF NOT EXISTS idx_files_kind ON files (kind);
    CREATE INDEX IF NOT EXISTS idx_files_source ON files (source);
    CREATE INDEX IF NOT EXISTS idx_files_device ON files (device_id);
    """)


_schema_lock = threading.Lock()
_schema_ready = False


def _init_schema(db: sqlite3.Connection) -> None:
    """Create/migrate the schema and indexes. Runs once per process — doing it
    on every connection makes every request (even reads) take a write lock,
    which collides with a running scan and yields 'database is locked'."""
    db.executescript(SCHEMA)
    cols = {row[1] for row in db.execute("PRAGMA table_info(files)")}
    if "category" not in cols:
        db.execute("ALTER TABLE files ADD COLUMN category TEXT")
    if "device_id" not in cols:
        db.execute("ALTER TABLE files ADD COLUMN device_id TEXT")
    if "device_label" not in cols:
        db.execute("ALTER TABLE files ADD COLUMN device_label TEXT")
    if "meta_fingerprint" not in cols:
        db.execute("ALTER TABLE files ADD COLUMN meta_fingerprint TEXT")
    if "content_hash" not in cols:
        db.execute("ALTER TABLE files ADD COLUMN content_hash TEXT")
    if "marked_delete" not in cols:
        db.execute("ALTER TABLE files ADD COLUMN marked_delete INTEGER NOT NULL DEFAULT 0")
    if "possible_dupe" not in cols:
        db.execute("ALTER TABLE files ADD COLUMN possible_dupe INTEGER NOT NULL DEFAULT 0")
    if _is_legacy_unique(db):
        _migrate_files_table(db)
    db.execute("CREATE INDEX IF NOT EXISTS idx_files_device ON files (device_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_files_meta_fp ON files (meta_fingerprint)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_files_content_hash ON files (content_hash)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_files_name_lower ON files (name COLLATE NOCASE)")
    db.commit()


def get_db() -> sqlite3.Connection:
    # 30s busy timeout: a delete/rename must wait out a scan job's commit
    # instead of failing with "database is locked". WAL keeps readers from
    # ever blocking on writers — but only if connections don't themselves
    # write, so the schema/migration DDL runs once (below), not per connection.
    global _schema_ready
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    if not _schema_ready:
        with _schema_lock:
            if not _schema_ready:
                _init_schema(db)
                _schema_ready = True
    return db


def classify(name: str) -> str | None:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return EXT_TO_KIND.get(ext)


def walk_filesystem(roots: list[Path], exclude: Path | None = None,
                    path_prefix: str = "", cancel_event=None):
    """Yield file rows for media files under roots."""
    if path_prefix:
        prefix_path = Path(path_prefix)
        if prefix_path.is_dir():
            stack = [prefix_path]
        else:
            return
    else:
        stack = [r for r in roots if r.is_dir()]
    while stack:
        if cancel_event is not None and cancel_event.is_set():
            break
        current = stack.pop()
        try:
            entries = os.scandir(current)
        except OSError:
            continue
        with entries:
            for entry in entries:
                if cancel_event is not None and cancel_event.is_set():
                    break
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


def scan_gdrive(path_prefix: str = ""):
    """Yield media file rows from Google Drive via rclone lsjson."""
    proc = subprocess.run(
        [find_rclone(), "lsjson", "-R", "--files-only", "--fast-list",
         "--no-mimetype", GDRIVE_REMOTE],
        capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"rclone failed: {proc.stderr.strip()[:500]}")
    for item in json.loads(proc.stdout):
        if not _path_matches_prefix(item["Path"], path_prefix):
            continue
        name = item["Name"]
        kind = classify(name)
        if kind is None:
            continue
        ext = name.rsplit(".", 1)[-1].lower()
        modified = item.get("ModTime", "")
        category = categorize(item["Path"], name, ext, kind, readable=False)
        yield item["Path"], name, ext, kind, item.get("Size", -1), modified, category


def fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:,.1f} {unit}"
        size /= 1024
    return f"{n} B"


def backfill_meta_fingerprints(db: sqlite3.Connection) -> int:
    rows = db.execute(
        "SELECT id, size, modified, name, ext, kind FROM files "
        "WHERE meta_fingerprint IS NULL OR meta_fingerprint = ''"
    ).fetchall()
    updated = 0
    batch = []
    for row_id, size, modified, name, ext, kind in rows:
        fp = compute_meta_fingerprint(size or -1, modified or "", name, ext, kind)
        batch.append((fp, row_id))
        if len(batch) >= 5000:
            db.executemany("UPDATE files SET meta_fingerprint = ? WHERE id = ?", batch)
            updated += len(batch)
            batch.clear()
    if batch:
        db.executemany("UPDATE files SET meta_fingerprint = ? WHERE id = ?", batch)
        updated += len(batch)
    if updated:
        db.commit()
    return updated


def run_scan(db: sqlite3.Connection, source: str, row_iter,
             device_id: str, device_label: str,
             scope_path: str | None = None, cancel_event=None,
             progress_cb=None) -> tuple[int, int]:
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    scanned_at = started
    rows = []
    count = 0
    total = 0
    t0 = time.time()
    insert_sql = (
        "INSERT OR REPLACE INTO files "
        "(source, path, name, ext, kind, size, modified, category, "
        "device_id, device_label, scanned_at, meta_fingerprint, content_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "(SELECT content_hash FROM files WHERE source=? AND device_id=? AND path=?))")
    if scope_path:
        like = scope_path.replace("\\", "/").rstrip("/") + "%"
        db.execute(
            "DELETE FROM files WHERE source = ? AND "
            "(device_id = ? OR device_id IS NULL OR device_id = 'legacy-machine') "
            "AND REPLACE(path, '\\\\', '/') LIKE REPLACE(?, '\\\\', '/')",
            (source, device_id, like),
        )
    elif source in ("local", "onedrive"):
        db.execute(
            "DELETE FROM files WHERE source = ? "
            "AND (device_id = ? OR device_id = 'legacy-machine' OR device_id IS NULL)",
            (source, device_id),
        )
    else:
        db.execute("DELETE FROM files WHERE source = ?", (source,))
    cancelled = False
    for path, name, ext, kind, size, modified, category in row_iter:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        meta_fp = compute_meta_fingerprint(size, modified, name, ext, kind)
        rows.append((source, path, name, ext, kind, size, modified, category,
                     device_id, device_label, scanned_at, meta_fp,
                     source, device_id, path))
        count += 1
        total += max(size, 0)
        if len(rows) >= 5000:
            db.executemany(insert_sql, rows)
            rows.clear()
            db.commit()
            msg = f"  [{source}] {count:,} files so far..."
            print(msg, flush=True)
            if progress_cb:
                progress_cb(source=source, phase="scan", files=count, message=msg)
    if rows:
        db.executemany(insert_sql, rows)
    finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if count:
        db.execute(
            "INSERT INTO scans (source, started_at, finished_at, file_count, total_bytes) "
            "VALUES (?, ?, ?, ?, ?)", (source, started, finished, count, total))
    db.commit()
    status = "cancelled" if cancelled else "done"
    print(f"  [{source}] {status}: {count:,} media files, {fmt_size(total)} in {time.time() - t0:.1f}s")
    if progress_cb:
        progress_cb(source=source, phase="scan", files=count,
                    message=f"{source} scan {status}", cancelled=cancelled)
    return count, total


def ingest_rows(db: sqlite3.Connection, source: str, device_id: str,
                device_label: str, files: list) -> int:
    """Upsert a remote machine's file inventory for `source` into the shared
    index, tagged with that machine's identity. Existing content hashes are
    preserved unless the incoming row supplies one; rows for this device that
    are absent from the new inventory are removed (so deletions propagate).

    Each item in `files` is a dict with keys: path, name, ext, kind, size,
    modified, category, and optionally content_hash."""
    scanned_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    insert_sql = (
        "INSERT OR REPLACE INTO files "
        "(source, path, name, ext, kind, size, modified, category, "
        "device_id, device_label, scanned_at, meta_fingerprint, content_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "COALESCE(?, (SELECT content_hash FROM files "
        "WHERE source=? AND device_id=? AND path=?)))")
    batch = []
    count = 0
    total = 0
    for f in files:
        path = f["path"]
        name = f["name"]
        ext = f.get("ext", "")
        kind = f["kind"]
        size = int(f.get("size") or 0)
        modified = f.get("modified") or ""
        category = f.get("category")
        chash = f.get("content_hash") or None
        meta = compute_meta_fingerprint(size, modified, name, ext, kind)
        batch.append((source, path, name, ext, kind, size, modified, category,
                      device_id, device_label, scanned_at, meta,
                      chash, source, device_id, path))
        count += 1
        total += max(size, 0)
        if len(batch) >= 5000:
            db.executemany(insert_sql, batch)
            batch.clear()
            db.commit()
    if batch:
        db.executemany(insert_sql, batch)
    # Drop rows for this device+source not present in this push (deletions).
    db.execute(
        "DELETE FROM files WHERE source = ? AND device_id = ? AND scanned_at < ?",
        (source, device_id, scanned_at))
    db.execute(
        "INSERT INTO scans (source, started_at, finished_at, file_count, total_bytes) "
        "VALUES (?, ?, ?, ?, ?)",
        (source, scanned_at,
         datetime.now(timezone.utc).isoformat(timespec="seconds"), count, total))
    db.commit()
    return count


def flag_possible_dupes(db: sqlite3.Connection) -> int:
    """Recompute possible_dupe: unhashed files at/over HASH_MAX_BYTES whose
    metadata fingerprint collides with another file. These are excluded from
    the hash pass and left for manual review."""
    db.execute("UPDATE files SET possible_dupe = 0 WHERE possible_dupe != 0")
    cur = db.execute(
        """
        UPDATE files SET possible_dupe = 1
        WHERE size >= ?
          AND (content_hash IS NULL OR content_hash = '')
          AND meta_fingerprint IN (
            SELECT meta_fingerprint FROM files
            WHERE meta_fingerprint IS NOT NULL AND meta_fingerprint != ''
            GROUP BY meta_fingerprint HAVING COUNT(*) > 1)
        """, (HASH_MAX_BYTES,))
    db.commit()
    return cur.rowcount


def _write_with_retry(db: sqlite3.Connection, sql: str, params, attempts: int = 6) -> bool:
    """Execute + commit a write, retrying transient 'database is locked' errors.

    Each attempt already waits out the busy timeout, so this tolerates a lock
    held for minutes (e.g. another writer mid remote move) without letting a
    single failure abort a long-running pass. Returns False if still locked
    after all attempts so the caller can skip that row and carry on."""
    for attempt in range(attempts):
        try:
            db.execute(sql, params)
            db.commit()
            return True
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            try:
                db.rollback()
            except sqlite3.Error:
                pass
            if attempt < attempts - 1:
                time.sleep(min(2 ** attempt, 15))
    return False


def run_hash_pass(db: sqlite3.Connection, sources: list[str] | None = None,
                  path_prefix: str = "", missing_only: bool = True,
                  cancel_event=None, progress_cb=None) -> int:
    flagged = flag_possible_dupes(db)
    print(f"  [hash] {flagged:,} files >= {HASH_MAX_BYTES / 1e9:.0f} GB flagged "
          "as possible duplicates (skipped from hashing)", flush=True)
    where = ["(size IS NULL OR size < ?)"]
    args: list = [HASH_MAX_BYTES]
    if missing_only:
        where.append("(content_hash IS NULL OR content_hash = '')")
    if sources:
        where.append(f"source IN ({','.join('?' * len(sources))})")
        args.extend(sources)
    if path_prefix:
        where.append("REPLACE(path, '\\\\', '/') LIKE ?")
        args.append(path_prefix.replace("\\", "/").rstrip("/") + "%")
    sql = (
        "SELECT id, source, path FROM files WHERE "
        + " AND ".join(where)
        + " ORDER BY source, path"
    )
    rows = db.execute(sql, args).fetchall()
    done = 0
    t0 = time.time()
    for row_id, source, path in rows:
        if cancel_event is not None and cancel_event.is_set():
            break
        digest = hash_file_row(source, path, cancel_event)
        if digest is None:
            if cancel_event is not None and cancel_event.is_set():
                break
            continue
        # Commit per file: hashing can take seconds (remote downloads), and
        # holding a write transaction across iterations starves other
        # connections (UI deletes/renames) into "database is locked". Retry a
        # transient lock instead of aborting the whole pass; skip on giving up.
        if not _write_with_retry(
                db, "UPDATE files SET content_hash = ? WHERE id = ?", (digest, row_id)):
            print(f"  [hash] skipped id={row_id}: database stayed locked", flush=True)
            continue
        done += 1
        if done % 100 == 0:
            msg = f"  [hash] {done:,}/{len(rows):,} files hashed..."
            print(msg, flush=True)
            if progress_cb:
                progress_cb(source="hash", phase="hash", files=done, total=len(rows), message=msg)
    db.commit()
    status = "cancelled" if cancel_event is not None and cancel_event.is_set() else "done"
    print(f"  [hash] {status}: {done:,} content hashes in {time.time() - t0:.1f}s")
    if progress_cb:
        progress_cb(source="hash", phase="hash", files=done, total=len(rows),
                    message=f"hash {status}", cancelled=status == "cancelled")
    return done


def iter_source_rows(source: str, path_prefix: str = "", cancel_event=None):
    local_device_id, local_device_label = get_machine_identity()
    qnap_cfg = load_qnap_config()
    if source == "local":
        return _filter_rows(
            walk_filesystem(LOCAL_ROOTS, exclude=ONEDRIVE_ROOT,
                            path_prefix=path_prefix, cancel_event=cancel_event),
            path_prefix, cancel_event)
    if source == "onedrive":
        roots = [Path(path_prefix)] if path_prefix else [ONEDRIVE_ROOT]
        return _filter_rows(
            walk_filesystem(roots, path_prefix=path_prefix, cancel_event=cancel_event),
            path_prefix, cancel_event)
    if source == "gdrive":
        return _filter_rows(scan_gdrive(path_prefix), path_prefix, cancel_event)
    if source == "qnap":
        return _filter_rows(scan_qnap(cancel_event, path_prefix), path_prefix, cancel_event)
    raise ValueError(f"unknown source: {source}")


def device_for_source(source: str) -> tuple[str, str]:
    if source in ("local", "onedrive"):
        return get_machine_identity()
    if source == "gdrive":
        return "gdrive-shared", "Google Drive remote"
    qnap_cfg = load_qnap_config()
    if source == "qnap" and qnap_cfg:
        return qnap_device_identity(qnap_cfg)
    return "unknown", source


def scan_sources(sources: list[str] | None = None, path_prefix: str = "",
                 hash_missing: bool = True, cancel_event=None, progress_cb=None) -> dict:
    sources = sources or default_sources()
    db = get_db()
    backfill_meta_fingerprints(db)
    totals = {"sources": {}, "hashed": 0, "cancelled": False}
    try:
        for source in sources:
            if cancel_event is not None and cancel_event.is_set():
                totals["cancelled"] = True
                break
            if progress_cb:
                progress_cb(source=source, phase="scan", files=0, message=f"Scanning {source}...")
            device_id, device_label = device_for_source(source)
            try:
                count, size = run_scan(
                    db, source,
                    iter_source_rows(source, path_prefix, cancel_event),
                    device_id, device_label,
                    scope_path=path_prefix or None,
                    cancel_event=cancel_event,
                    progress_cb=progress_cb,
                )
                totals["sources"][source] = {"files": count, "bytes": size}
            except Exception as exc:
                totals["sources"][source] = {"error": str(exc)}
            if cancel_event is not None and cancel_event.is_set():
                totals["cancelled"] = True
                break
        if hash_missing and not totals.get("cancelled"):
            totals["hashed"] = run_hash_pass(
                db, sources=sources, path_prefix=path_prefix,
                missing_only=True, cancel_event=cancel_event, progress_cb=progress_cb)
            if cancel_event is not None and cancel_event.is_set():
                totals["cancelled"] = True
    finally:
        db.close()
    return totals


def cmd_scan(args):
    cancel_event = None
    totals = scan_sources(
        sources=args.sources or None,
        path_prefix=getattr(args, "path_prefix", "") or "",
        hash_missing=not getattr(args, "no_hash", False),
    )
    for source, info in totals.get("sources", {}).items():
        if "error" in info:
            print(f"  [{source}] FAILED: {info['error']}", file=sys.stderr)
    if totals.get("hashed"):
        print(f"  Hashed {totals['hashed']:,} files")


def cmd_push(args):
    """Walk this machine's local files and push them to a remote IndexHub
    server's shared index (this machine keeps no database of its own)."""
    server = args.server.rstrip("/")
    source = args.source
    device_id, device_label = get_machine_identity()
    print(f"Collecting {source} files on this machine ({device_label})...", flush=True)
    files = []
    for path, name, ext, kind, size, modified, category in iter_source_rows(source):
        rec = {"path": path, "name": name, "ext": ext, "kind": kind,
               "size": size, "modified": modified, "category": category}
        if args.hash:
            digest = hash_local_file(path)
            if digest:
                rec["content_hash"] = digest
        files.append(rec)
        if len(files) % 1000 == 0:
            print(f"  {len(files):,} files{' (hashed)' if args.hash else ''}...", flush=True)
    print(f"Collected {len(files):,} files. Pushing to {server} ...", flush=True)
    payload = json.dumps({
        "device_id": device_id, "device_label": device_label,
        "source": source, "files": files,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{server}/api/ingest", data=payload,
        headers={"Content-Type": "application/json"})
    token = os.environ.get("INDEXHUB_TOKEN")
    if token:
        req.add_header("X-IndexHub-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        print(f"Push failed: HTTP {exc.code} {exc.read().decode()[:200]}", file=sys.stderr)
        return
    except urllib.error.URLError as exc:
        print(f"Push failed: cannot reach {server} ({exc.reason})", file=sys.stderr)
        return
    if result.get("ok"):
        print(f"Pushed {result['count']:,} {source} files to the shared index "
              f"as '{device_label}'.")
    else:
        print(f"Push rejected: {result.get('error')}", file=sys.stderr)


def cmd_search(args):
    db = get_db()
    sql = ("SELECT source, device_label, kind, category, size, modified, path "
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
    for source, device_label, kind, category, size, modified, path in results:
        label = category or kind
        who = (device_label or "-")[:24]
        print(f"{source:<9} {who:<24} {label:<8} {fmt_size(size or 0):>10}  {modified or '':<20}  {path}")
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
    devices = db.execute(
        "SELECT source, device_label, COUNT(*) FROM files "
        "GROUP BY source, device_label ORDER BY source, device_label").fetchall()
    if devices:
        print("\nIndexed machine contexts:")
        for source, label, count in devices:
            print(f"  {source}: {label or 'unknown'} ({count:,} files)")
    last = db.execute(
        "SELECT source, MAX(finished_at) FROM scans GROUP BY source").fetchall()
    print("\nLast scans:")
    for source, when in last:
        print(f"  {source}: {when}")


def main():
    parser = argparse.ArgumentParser(description="Media Index")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="scan sources and rebuild their index")
    p_scan.add_argument("sources", nargs="*",
                        choices=["local", "onedrive", "gdrive", "qnap"],
                        help="sources to scan (default: all configured)")
    p_scan.add_argument("--path", dest="path_prefix", default="",
                        help="optional folder path to limit scan scope")
    p_scan.add_argument("--no-hash", action="store_true",
                        help="skip content hash computation after scan")
    p_scan.set_defaults(func=cmd_scan)

    p_qnap = sub.add_parser("qnap-setup", help="save QNAP NAS credentials locally")
    p_qnap.add_argument("--host", default="192.168.50.168")
    p_qnap.add_argument("--web-url", default="http://192.168.50.168:8080/")
    p_qnap.add_argument("--share", default="Public",
                        help="SMB share name to scan (default: Public)")
    p_qnap.add_argument("--user", required=True)
    p_qnap.add_argument("--password", help="QNAP password (prompted if omitted)")
    p_qnap.add_argument("--label", default="QNAP NAS")
    p_qnap.set_defaults(func=cmd_qnap_setup)

    p_search = sub.add_parser("search", help="search the index by filename")
    p_search.add_argument("term")
    p_search.add_argument("--kind", choices=["video", "audio", "image", "document"])
    p_search.add_argument("--source", choices=["local", "onedrive", "gdrive", "qnap"])
    p_search.add_argument("--category",
                          choices=["photo", "graphic", "text", "data", "word",
                                   "spreadsheet", "presentation", "pdf"],
                          help="image or document sub-category")
    p_search.add_argument("--limit", type=int, default=50)
    p_search.set_defaults(func=cmd_search)

    p_stats = sub.add_parser("stats", help="show index statistics")
    p_stats.set_defaults(func=cmd_stats)

    p_push = sub.add_parser(
        "push", help="send this machine's local files to a remote IndexHub "
        "server (use on a laptop that shares the desktop's index)")
    p_push.add_argument("--server", required=True,
                        help="IndexHub server URL, e.g. http://192.168.50.50:8765")
    p_push.add_argument("--source", default="local", choices=["local", "onedrive"],
                        help="which local source to push (default: local)")
    p_push.add_argument("--hash", action="store_true",
                        help="compute content hashes locally first "
                        "(enables exact-duplicate detection for these files)")
    p_push.set_defaults(func=cmd_push)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
