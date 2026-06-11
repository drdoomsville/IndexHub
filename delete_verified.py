"""Delete verified duplicate copies via the app API (files go to NAS trash).

Usage: python delete_verified.py <verified.json> <manifest-out.json> <session-name>
"""
import json
import sys
import urllib.request

verified_json, manifest_out, session = sys.argv[1], sys.argv[2], sys.argv[3]
with open(verified_json, encoding="utf-8") as f:
    verified = json.load(f)

print(f"deleting {len(verified)} verified copies "
      f"({sum(v['size'] for v in verified)/1e9:.1f} GB)", flush=True)
results = []
for v in verified:
    req = urllib.request.Request(
        "http://localhost:8765/api/delete",
        data=json.dumps({"id": v["id"]}).encode(),
        headers={"Content-Type": "application/json",
                 "Cookie": f"indexhub_session={session}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            res = json.loads(resp.read())
    except Exception as exc:
        res = {"ok": False, "error": str(exc)}
    results.append({**v, "ok": res.get("ok"), "entry": res.get("entry"),
                    "error": res.get("error")})
    status = "ok" if res.get("ok") else f"FAILED: {res.get('error')}"
    print(f"  [{status}] {v['path']}", flush=True)

with open(manifest_out, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2)
ok = sum(1 for r in results if r["ok"])
freed = sum(r["size"] for r in results if r["ok"])
print(f"\ndeleted {ok}/{len(results)} ({freed/1e9:.1f} GB to NAS trash); "
      f"manifest: {manifest_out}", flush=True)
