"""Hash + verify large (>=500 MB) metadata-duplicate groups on qnap, then
propose a keeper per verified group.

Usage:
  python mirror_dedupe.py scope
  python mirror_dedupe.py hash      # hash missing members, then propose
  python mirror_dedupe.py propose   # propose from existing hashes only
"""
import json
import re
import sqlite3
import sys
import time

import media_index as mi

MIN_SIZE = 500e6


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def groups_to_do(conn):
    fps = [r[0] for r in conn.execute(
        """
        SELECT meta_fingerprint FROM files
        WHERE source='qnap' AND size >= ? AND meta_fingerprint != ''
        GROUP BY meta_fingerprint HAVING COUNT(*) > 1
        """, (MIN_SIZE,))]
    out = []
    for fp in fps:
        rows = conn.execute(
            "SELECT id, path, name, size, content_hash FROM files "
            "WHERE source='qnap' AND meta_fingerprint = ? ORDER BY path", (fp,)).fetchall()
        out.append((fp, rows))
    return out


def keeper_rank(row):
    parts = row["path"].split("/")
    parent = parts[-2] if len(parts) > 1 else ""
    stem = row["name"].rsplit(".", 1)[0]
    # prefer parent folder that looks like the movie itself, then shallow paths
    name_match = 0 if (norm(parent) and (norm(parent) in norm(stem)
                       or norm(stem) in norm(parent))) else 1
    return (name_match, len(parts), row["path"].lower())


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "scope"
    conn = mi.get_db()
    conn.row_factory = sqlite3.Row
    groups = groups_to_do(conn)
    todo = [r for _, rows in groups for r in rows if not r["content_hash"]]
    print(f"groups >=500MB: {len(groups)}; members to hash: {len(todo)}; "
          f"{sum(r['size'] for r in todo)/1e9:.1f} GB", flush=True)

    if mode == "hash":
        t0 = time.time()
        done_bytes = 0
        for i, r in enumerate(todo, 1):
            digest = mi.hash_file_row("qnap", r["path"])
            if digest:
                conn.execute("UPDATE files SET content_hash = ? WHERE id = ?",
                             (digest, r["id"]))
                conn.commit()
            done_bytes += r["size"]
            rate = done_bytes / max(time.time() - t0, 1) / 1e6
            print(f"[{i}/{len(todo)}] {'ok' if digest else 'FAILED'} "
                  f"{r['path']} ({r['size']/1e9:.2f} GB, {rate:.0f} MB/s avg)", flush=True)
        print("hashing done", flush=True)

    if mode in ("hash", "propose"):
        proposal = []
        for fp, _ in groups_to_do(conn):
            rows = conn.execute(
                "SELECT id, path, name, size, content_hash FROM files "
                "WHERE source='qnap' AND meta_fingerprint = ? ORDER BY path", (fp,)).fetchall()
            by_hash = {}
            for r in rows:
                if r["content_hash"]:
                    by_hash.setdefault(r["content_hash"], []).append(r)
            for h, members in by_hash.items():
                if len(members) < 2:
                    continue
                members = sorted(members, key=keeper_rank)
                keep, dupes = members[0], members[1:]
                proposal.append({
                    "keep": keep["path"],
                    "delete": [{"id": d["id"], "path": d["path"], "size": d["size"]}
                               for d in dupes],
                })
        n_del = sum(len(p["delete"]) for p in proposal)
        gb = sum(d["size"] for p in proposal for d in p["delete"]) / 1e9
        print(f"\nPROPOSAL: {len(proposal)} verified groups, {n_del} deletions, {gb:.1f} GB", flush=True)
        for p in proposal:
            print(f"\nKEEP   {p['keep']}")
            for d in p["delete"]:
                print(f"DELETE {d['path']} ({d['size']/1e9:.2f} GB)")
        flat = [{**d, "keep": p["keep"]} for p in proposal for d in p["delete"]]
        with open("mirror_verified_dupes.json", "w", encoding="utf-8") as f:
            json.dump(flat, f, indent=2)
    conn.close()


if __name__ == "__main__":
    main()
