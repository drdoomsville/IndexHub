"""Targeted hash + verify for duplicates under a qnap path prefix.

Usage:
  python targeted_dedupe.py scope  <prefix> <out.json>
  python targeted_dedupe.py hash   <prefix> <out.json>   # hash + verify
  python targeted_dedupe.py verify <prefix> <out.json>

<prefix> marks the copies to delete; keepers are matching files anywhere
else on the share. Verification requires an exact content-hash match.
"""
import json
import sqlite3
import sys
import time

import media_index as mi


def members_by_group(conn, prefix):
    fps = [r[0] for r in conn.execute(
        """
        SELECT meta_fingerprint FROM files
        WHERE source='qnap' AND path LIKE ? AND meta_fingerprint IS NOT NULL
          AND meta_fingerprint != ''
        GROUP BY meta_fingerprint
        """, (prefix + "%",))]
    groups = []
    for fp in fps:
        rows = conn.execute(
            "SELECT id, path, size, content_hash FROM files "
            "WHERE source='qnap' AND meta_fingerprint = ? ORDER BY path", (fp,)).fetchall()
        if len(rows) > 1:
            groups.append((fp, rows))
    return groups


def main():
    mode, prefix, out_json = sys.argv[1], sys.argv[2], sys.argv[3]
    conn = mi.get_db()
    conn.row_factory = sqlite3.Row
    groups = members_by_group(conn, prefix)
    todo = [r for _, rows in groups for r in rows if not r["content_hash"]]
    n_bytes = sum(r["size"] for r in todo)
    print(f"dup groups touching {prefix}: {len(groups)}; files to hash: {len(todo)}; "
          f"{n_bytes/1e9:.1f} GB", flush=True)

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

    if mode in ("hash", "verify"):
        verified, unverified = [], []
        for fp, _ in members_by_group(conn, prefix):
            rows = conn.execute(
                "SELECT id, path, size, content_hash FROM files "
                "WHERE source='qnap' AND meta_fingerprint = ? ORDER BY path", (fp,)).fetchall()
            for r in rows:
                if not r["path"].startswith(prefix):
                    continue
                keep = next((k["path"] for k in rows
                             if not k["path"].startswith(prefix)
                             and r["content_hash"]
                             and k["content_hash"] == r["content_hash"]), None)
                if keep:
                    verified.append({"id": r["id"], "path": r["path"],
                                     "size": r["size"], "keep": keep})
                else:
                    unverified.append({"path": r["path"], "size": r["size"]})
        print(f"\nVERIFIED byte-identical copies under {prefix}: {len(verified)} "
              f"({sum(v['size'] for v in verified)/1e9:.1f} GB)", flush=True)
        for v in verified:
            print(f"  {v['path']}  ==  {v['keep']}")
        print(f"NOT verified (no matching outside hash): {len(unverified)}")
        for u in unverified:
            print(f"  {u['path']} ({u['size']/1e9:.2f} GB)")
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(verified, f, indent=2)
    conn.close()


if __name__ == "__main__":
    main()
