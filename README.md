# File Index (Media + Documents)

A SQLite-backed catalog of media files (video, audio, images) and documents
(text, data, Word, spreadsheets, presentations, PDFs) across:

- **Local disk** — `Videos`, `Pictures`, `Music`, `Downloads`, `Documents`, `Desktop`
- **OneDrive** — the locally synced folder at `~/OneDrive`
- **Google Drive** — listed remotely via [rclone](https://rclone.org/) (remote name: `gdrive`)
- **QNAP NAS** — listed via rclone SMB (remote name: `qnap`; configure with `qnap-setup`)

Everything is stored in `media_index.db` next to the script. No third-party Python
packages required (Python 3.10+ stdlib only). rclone must be installed for the
Google Drive and QNAP sources.

## Usage

```powershell
# Rebuild the index (all sources)
python media_index.py scan

# Scan only specific sources
python media_index.py scan local onedrive
python media_index.py scan gdrive
python media_index.py scan qnap

# Configure QNAP NAS (credentials saved locally in qnap_config.json, gitignored)
python media_index.py qnap-setup --user YOUR_QNAP_USER --share Public
python media_index.py qnap-setup --user YOUR_QNAP_USER --host 192.168.50.168 --web-url http://192.168.50.168:8080/

# Search by filename
python media_index.py search vacation
python media_index.py search ".heic" --kind image --source onedrive --limit 20
python media_index.py search "." --category photo     # camera photos only
python media_index.py search "." --category graphic   # computer images only

# Summary of what's indexed
python media_index.py stats

# Browse and query in your browser
python webui.py            # then open http://localhost:8765
python webui.py --port 9000
```

## Pages

- `/` — landing hub with live stats for both indexes
- `/media` — Media Index (video, audio, photos vs computer images)
- `/documents` — Documents Index (text: txt/md/rtf/log, data: json/xml/yaml/csv,
  Word: doc/docx/odt, spreadsheets: xls/xlsx/xlsm/ods, presentations: ppt/pptx/odp,
  and PDF). Same search, faceted filters, preview, and rename features as Media;
  text/data files preview inline and PDFs render in an embedded viewer.

Noise directories (`node_modules`, `.git`, `venv`, build output, ...) are skipped
during scans.

## Web UI

`webui.py` serves a local, dependency-free web interface on top of the same
database: live filename/path search, filters by type (including Photos vs
Computer images), source, machine, and year, sortable results, and per-source
stats. Filters are faceted: every dropdown shows live match counts given the
other active filters, and options with no matches are greyed out.

Click any row to open the detail panel:

- **Preview** — images, videos, and audio render inline (local files stream with
  HTTP range support so videos are seekable; Google Drive and QNAP files stream through
  rclone). Formats the browser can't render (HEIC, RAW, MKV, ...) show details only.
  Note: previewing a cloud-only OneDrive file makes OneDrive download it.
- **Rename** — works on all sources (local/OneDrive renames on disk; Google Drive
  and QNAP rename via rclone) and updates the index immediately. Suggested
  names are generated from the EXIF date taken and camera model (when available),
  the file's photo/graphic/video/audio type, and its folder context.

The server binds to `127.0.0.1` only, so nothing is exposed to your network.
Keep the terminal running while you use it; Ctrl+C stops it.

## How it works

- Local/OneDrive scans walk the filesystem with `os.scandir` (attribute-only, so
  OneDrive cloud-only placeholder files are indexed without downloading them).
- Google Drive is listed with `rclone lsjson -R --files-only --fast-list gdrive:`
  using a read-only OAuth scope.
- QNAP is listed with `rclone lsjson` over SMB (`qnap:SHARE`). Run
  `python media_index.py qnap-setup` once to save credentials in
  `qnap_config.json` (copy from `qnap_config.example.json`). The web UI at
  `http://192.168.50.168:8080/` is stored as metadata only; scanning uses SMB.
- Every image is auto-classified as a **photo** (camera shot) or **graphic**
  (computer-generated image) using camera raw/HEIC extensions, camera filename
  conventions (`IMG_`, `DSC`, `PXL_`, timestamps, WhatsApp names), screenshot and
  asset folder/name patterns, and - for ambiguous local JPEGs - a built-in EXIF
  check for camera Make/Model tags. Cloud-only OneDrive placeholders are never
  downloaded for the EXIF check; classification is heuristic, so stripped-EXIF
  photos may land in "graphic".
- Each `scan` fully replaces that source's rows for the scanning machine, so the
  index always reflects the latest scan. Scan history is kept in the `scans` table.

## Machine identity

Every local/OneDrive row records which machine it was scanned on, so `C:\...`
paths from different PCs never collide and remain distinguishable:

- `device_id` — stable fingerprint hashed from the hostname, the Windows
  `MachineGuid`, and the `C:` volume serial number
- `device_label` — readable form, e.g. `DESKTOP-H13TS5U (C:CB50F0E0)`

Run the same scripts (sharing the same `media_index.db`) on a second machine and
its files are indexed alongside this one's; the web UI's **Machine** filter and
column tell them apart. Google Drive rows use a shared `gdrive-shared` identity
since the remote is machine-independent. QNAP rows use a stable `qnap-*` device
identity derived from the NAS host. Databases created before this feature
are migrated automatically on first use.

## Database schema

`files(source, path, name, ext, kind, size, modified, category, device_id,
device_label, scanned_at)` with `UNIQUE (source, device_id, path)` and indexes on
`name`, `kind`, `source`, and `device_id`. Query it directly with any SQLite
client if you want more than the built-in search.
