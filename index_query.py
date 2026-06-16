"""Read queries over the index (the `files` table).

All knowledge of the files-table schema for browsing, faceting, and duplicate
analysis lives here, behind functions that take a db connection plus plain
filter objects and return plain Python data. The web handlers parse request
params into these filters and format the results; they hold no SQL. Because the
seam is (db, filters) -> data, the dedup analytics can be tested against a DB
fixture with no HTTP.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import media_index as mi

MEDIA_KINDS = ("video", "audio", "image")
CATEGORY_VALUES = {"photo", "graphic",
                   "text", "data", "word", "spreadsheet", "presentation", "pdf"}
SOURCES = ("local", "onedrive", "gdrive", "qnap")

SORT_COLUMNS = {
    "name": "name COLLATE NOCASE ASC",
    "size": "size DESC",
    "modified": "modified DESC",
    "path": "path COLLATE NOCASE ASC",
}

DUP_MODES = {
    # "name COLLATE NOCASE" groups case-insensitively like LOWER(name) but can
    # use the idx_files_name_lower index, where LOWER(name) cannot.
    "name": "name COLLATE NOCASE",
    "meta": "meta_fingerprint",
    "hash": "content_hash",
}

REPORT_BUCKETS = [
    ("≥ 1 GB", 1_000_000_000, None),
    ("100 MB – 1 GB", 100_000_000, 1_000_000_000),
    ("10 – 100 MB", 10_000_000, 100_000_000),
    ("< 10 MB", 0, 10_000_000),
]


@dataclass
class SearchFilters:
    """Browse/facet filters, as plain values (already parsed from the request)."""
    domain: str = "media"
    q: str = ""
    kind: str = ""
    source: str = ""
    machine: str = ""
    year: str = ""


@dataclass
class DupFilters:
    """Extra scoping for the duplicate-group queries."""
    q: str = ""
    source: str = ""
    min_size: int = 0
    possible: bool = False


def _domain_clause(domain: str) -> str:
    if domain == "documents":
        return "kind = 'document'"
    return "kind IN ('video', 'audio', 'image')"


def _where(f: SearchFilters, exclude: str | None = None):
    """WHERE clause from a SearchFilters, optionally ignoring one facet."""
    where, args = [_domain_clause(f.domain)], []
    if f.q:
        where.append("(name LIKE ? OR path LIKE ?)")
        args += [f"%{f.q}%", f"%{f.q}%"]
    if exclude != "kind":
        if f.kind in MEDIA_KINDS:
            where.append("kind = ?")
            args.append(f.kind)
        elif f.kind in CATEGORY_VALUES:
            where.append("category = ?")
            args.append(f.kind)
    if exclude != "source" and f.source in SOURCES:
        where.append("source = ?")
        args.append(f.source)
    if exclude != "machine" and f.machine:
        where.append("device_id = ?")
        args.append(f.machine)
    if exclude != "year" and f.year.isdigit() and len(f.year) == 4:
        where.append("substr(modified,1,4) = ?")
        args.append(f.year)
    return " AND ".join(where), args


def _dup_where(f: DupFilters):
    """Extra conditions for duplicate queries; fragment starts with ' AND '."""
    where, args = [], []
    if f.q:
        where.append("(name LIKE ? OR path LIKE ?)")
        args += [f"%{f.q}%", f"%{f.q}%"]
    if f.source in SOURCES:
        where.append("source = ?")
        args.append(f.source)
    if f.min_size > 0:
        where.append("size >= ?")
        args.append(f.min_size)
    if f.possible:
        where.append("possible_dupe = 1")
    frag = "".join(f" AND {c}" for c in where)
    return frag, args


def _file_brief(row) -> dict:
    return {
        "id": row["id"], "source": row["source"], "name": row["name"],
        "path": row["path"], "size": row["size"], "modified": row["modified"],
        "content_hash": row["content_hash"], "meta_fingerprint": row["meta_fingerprint"],
        "possible_dupe": row["possible_dupe"], "kind": row["kind"],
    }


def stats(db: sqlite3.Connection, domain: str = "media") -> dict:
    dom = _domain_clause(domain)
    sources = [
        {"source": r["source"], "count": r["c"], "bytes": r["b"] or 0}
        for r in db.execute(
            f"SELECT source, COUNT(*) c, SUM(size) b FROM files "
            f"WHERE {dom} GROUP BY source")
    ]
    total, total_bytes = db.execute(
        f"SELECT COUNT(*), SUM(size) FROM files WHERE {dom}").fetchone()
    years = [r[0] for r in db.execute(
        f"SELECT DISTINCT substr(modified,1,4) y FROM files "
        f"WHERE {dom} AND modified != '' ORDER BY y DESC")]
    categories = dict(db.execute(
        f"SELECT category, COUNT(*) FROM files "
        f"WHERE {dom} AND category IS NOT NULL GROUP BY category"))
    return {"sources": sources, "total": total, "bytes": total_bytes or 0,
            "years": years, "categories": categories}


def facets(db: sqlite3.Connection, f: SearchFilters) -> dict:
    """Valid options per filter, each computed with the *other* filters applied."""
    cond, args = _where(f, exclude="kind")
    kinds = dict(db.execute(
        f"SELECT kind, COUNT(*) FROM files WHERE {cond} GROUP BY kind", args))
    categories = dict(db.execute(
        f"SELECT category, COUNT(*) FROM files WHERE {cond} "
        f"AND category IS NOT NULL GROUP BY category", args))
    cond, args = _where(f, exclude="source")
    sources = dict(db.execute(
        f"SELECT source, COUNT(*) FROM files WHERE {cond} GROUP BY source", args))
    cond, args = _where(f, exclude="machine")
    devices = [{"value": r[0], "label": r[1] or "Unknown machine", "count": r[2]}
               for r in db.execute(
        f"SELECT device_id, device_label, COUNT(*) FROM files "
        f"WHERE {cond} GROUP BY device_id, device_label ORDER BY device_label", args)]
    cond, args = _where(f, exclude="year")
    years = [{"value": r[0], "count": r[1]} for r in db.execute(
        f"SELECT substr(modified,1,4) y, COUNT(*) FROM files "
        f"WHERE {cond} AND modified != '' GROUP BY y ORDER BY y DESC", args)]
    return {"kinds": kinds, "categories": categories,
            "sources": sources, "devices": devices, "years": years}


def search(db: sqlite3.Connection, f: SearchFilters, sort_key: str = "modified",
           page: int = 0, page_size: int = 100) -> dict:
    sort = SORT_COLUMNS.get(sort_key, SORT_COLUMNS["modified"])
    cond, args = _where(f)
    total = db.execute(f"SELECT COUNT(*) FROM files WHERE {cond}", args).fetchone()[0]
    rows = [dict(r) for r in db.execute(
        f"SELECT id, source, device_id, device_label, path, name, ext, kind, size, modified, "
        f"category, marked_delete "
        f"FROM files WHERE {cond} ORDER BY {sort} LIMIT ? OFFSET ?",
        args + [page_size, page * page_size])]
    return {"total": total, "rows": rows, "page": page, "page_size": page_size}


def duplicates_summary(db: sqlite3.Connection) -> dict:
    mi.backfill_meta_fingerprints(db)
    total = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    hashed = db.execute(
        "SELECT COUNT(*) FROM files WHERE content_hash IS NOT NULL AND content_hash != ''"
    ).fetchone()[0]
    groups = db.execute(
        "SELECT COUNT(*) FROM ("
        "SELECT content_hash FROM files WHERE content_hash IS NOT NULL AND content_hash != '' "
        "GROUP BY content_hash HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    possible = db.execute(
        "SELECT COUNT(*) FROM files WHERE possible_dupe = 1").fetchone()[0]
    return {"total": total, "hashed": hashed, "groups": groups, "possible": possible}


def duplicates_report(db: sqlite3.Connection, source: str = "") -> dict:
    """Exact-content-hash duplicate breakdown, optionally scoped to one source.
    Reports group/redundant-copy counts, reclaimable bytes, size buckets, a
    per-source rollup, and the biggest groups by reclaimable space."""
    if source not in SOURCES:
        source = ""
    hashed_clause = "content_hash IS NOT NULL AND content_hash != ''"
    scope_clause = hashed_clause + (" AND source = ?" if source else "")
    scope_args = [source] if source else []

    files, ibytes = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE "
        + (("source = ?") if source else "1=1"), scope_args).fetchone()
    hashed = db.execute(
        f"SELECT COUNT(*) FROM files WHERE {scope_clause}", scope_args).fetchone()[0]

    # Duplicate groups within scope (cross-source when no source is selected).
    rows = db.execute(
        f"SELECT content_hash h, COUNT(*) c, MAX(size) sz FROM files "
        f"WHERE {scope_clause} GROUP BY content_hash HAVING c > 1", scope_args).fetchall()

    groups = len(rows)
    redundant = sum(r["c"] - 1 for r in rows)
    reclaim = sum((r["c"] - 1) * (r["sz"] or 0) for r in rows)

    buckets = []
    for label, lo, hi in REPORT_BUCKETS:
        sel = [r for r in rows
               if (r["sz"] or 0) >= lo and (hi is None or (r["sz"] or 0) < hi)]
        buckets.append({
            "label": label,
            "groups": len(sel),
            "copies": sum(r["c"] - 1 for r in sel),
            "bytes": sum((r["c"] - 1) * (r["sz"] or 0) for r in sel),
        })

    top = sorted(rows, key=lambda r: (r["c"] - 1) * (r["sz"] or 0), reverse=True)[:25]
    top_out = []
    for r in top:
        sample = db.execute(
            "SELECT id, name, path, source FROM files WHERE content_hash = ?"
            + (" AND source = ?" if source else "") + " ORDER BY source, path LIMIT 1",
            [r["h"]] + scope_args).fetchone()
        if not sample:
            continue
        top_out.append({
            "id": sample["id"], "name": sample["name"], "path": sample["path"],
            "source": sample["source"], "count": r["c"], "each": r["sz"] or 0,
            "waste": (r["c"] - 1) * (r["sz"] or 0),
        })

    # Per-source rollup: duplicate groups *within* each source.
    per_rows = db.execute(
        f"SELECT source, content_hash, COUNT(*) c, MAX(size) sz FROM files "
        f"WHERE {hashed_clause} GROUP BY source, content_hash HAVING c > 1").fetchall()
    roll = {}
    for r in per_rows:
        d = roll.setdefault(r["source"], {"groups": 0, "copies": 0, "reclaim": 0})
        d["groups"] += 1
        d["copies"] += r["c"] - 1
        d["reclaim"] += (r["c"] - 1) * (r["sz"] or 0)
    counts = dict(db.execute(
        f"SELECT source, COUNT(*) FROM files WHERE {hashed_clause} GROUP BY source"))
    per_source = []
    for src in SOURCES:
        if src not in counts and src not in roll:
            continue
        d = roll.get(src, {"groups": 0, "copies": 0, "reclaim": 0})
        per_source.append({"source": src, "files": counts.get(src, 0), **d})

    return {
        "scope": source or "all", "files": files, "ibytes": ibytes, "hashed": hashed,
        "groups": groups, "redundant": redundant, "reclaim": reclaim,
        "buckets": buckets, "top": top_out, "per_source": per_source,
    }


def duplicate_groups(db: sqlite3.Connection, mode: str, f: DupFilters,
                     page: int = 0, limit: int = 25, file_id: str = "",
                     group_file_cap: int = 50) -> dict:
    if mode not in DUP_MODES:
        mode = "name"
    key_expr = DUP_MODES[mode]
    if mode == "meta":
        mi.backfill_meta_fingerprints(db)
    anchor = None

    if file_id:
        row = db.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        if not row:
            return {"groups": [], "total_groups": 0, "page": 0, "page_size": limit}
        anchor = _file_brief(row)
        if mode == "name":
            key_val = row["name"].lower()
        elif mode == "meta":
            if not row["meta_fingerprint"]:
                return {"groups": [], "total_groups": 0, "page": 0,
                        "page_size": limit, "anchor": anchor}
            key_val = row["meta_fingerprint"]
        else:
            if not row["content_hash"]:
                return {"groups": [], "total_groups": 0, "page": 0,
                        "page_size": limit, "anchor": anchor}
            key_val = row["content_hash"]
        files = [dict(r) for r in db.execute(
            f"SELECT * FROM files WHERE {key_expr} = ? ORDER BY source, name", [key_val])]
        if len(files) < 2:
            return {"groups": [], "total_groups": 0, "page": 0,
                    "page_size": limit, "anchor": anchor}
        label = key_val if mode != "name" else row["name"]
        return {
            "groups": [{
                "key": key_val, "label": label, "count": len(files),
                "files": [_file_brief(r) for r in files[:group_file_cap]],
            }],
            "total_groups": 1,
            "page": 0,
            "page_size": limit,
            "anchor": anchor,
        }

    null_guard = f"{key_expr} IS NOT NULL AND {key_expr} != ''"
    if mode == "name":
        null_guard = f"{key_expr} IS NOT NULL"

    frag, frag_args = _dup_where(f)
    where_full = null_guard + frag

    total_groups = db.execute(
        f"SELECT COUNT(*) FROM ("
        f"SELECT {key_expr} k FROM files WHERE {where_full} "
        f"GROUP BY k HAVING COUNT(*) > 1)", frag_args
    ).fetchone()[0]

    key_rows = db.execute(
        f"SELECT k, c FROM ("
        f"SELECT {key_expr} k, COUNT(*) c FROM files WHERE {where_full} "
        f"GROUP BY k HAVING c > 1) ORDER BY c DESC, k LIMIT ? OFFSET ?",
        frag_args + [limit, page * limit]).fetchall()

    groups = []
    for key_val, count in key_rows:
        # Fetch only the capped slice, not every row in the group; the true
        # total comes from the grouped count above. Groups can be huge
        # (thousands of identical files), so this avoids a heavy fetchall and
        # the resulting oversized payload.
        rows = db.execute(
            f"SELECT * FROM files WHERE {key_expr} = ?{frag} ORDER BY source, name LIMIT ?",
            [key_val] + frag_args + [group_file_cap]).fetchall()
        label = rows[0]["name"] if (mode == "name" and rows) else key_val
        groups.append({
            "key": key_val,
            "label": label,
            "count": count,
            "files": [_file_brief(r) for r in rows],
        })
    return {
        "groups": groups,
        "total_groups": total_groups,
        "page": page,
        "page_size": limit,
        "anchor": anchor,
    }
