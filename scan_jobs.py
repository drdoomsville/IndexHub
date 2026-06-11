"""Background scan/hash jobs for the web UI."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import media_index as mi


class ScanJobManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = self._idle_state()

    @staticmethod
    def _idle_state() -> dict:
        return {
            "running": False,
            "cancelling": False,
            "phase": "idle",
            "source": "",
            "message": "Idle",
            "files": 0,
            "total": 0,
            "started_at": None,
            "finished_at": None,
            "error": None,
            "cancelled": False,
            "results": None,
        }

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def start(self, sources: list[str] | None, path_prefix: str = "",
              rescan: bool = True, hash_missing: bool = True) -> bool:
        with self._lock:
            if self._state["running"]:
                return False
            self._cancel = threading.Event()
            self._state = {
                "running": True,
                "cancelling": False,
                "phase": "starting",
                "source": "",
                "message": "Starting…",
                "files": 0,
                "total": 0,
                "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "finished_at": None,
                "error": None,
                "cancelled": False,
                "results": None,
            }
            args = (sources, path_prefix, rescan, hash_missing)
            self._thread = threading.Thread(target=self._run, args=args, daemon=True)
            self._thread.start()
            return True

    def cancel(self) -> bool:
        with self._lock:
            if not self._state["running"]:
                return False
            self._state["cancelling"] = True
            self._state["message"] = "Cancelling after current step…"
        self._cancel.set()
        return True

    def _progress(self, **kwargs):
        with self._lock:
            for key, value in kwargs.items():
                if key in self._state and value is not None:
                    self._state[key] = value

    def _run(self, sources: list[str] | None, path_prefix: str,
             rescan: bool, hash_missing: bool):
        results = {"sources": {}, "hashed": 0, "cancelled": False}
        error = None
        try:
            if rescan:
                results = mi.scan_sources(
                    sources=sources,
                    path_prefix=path_prefix,
                    hash_missing=hash_missing,
                    cancel_event=self._cancel,
                    progress_cb=self._progress,
                )
            elif hash_missing:
                db = mi.get_db()
                try:
                    mi.backfill_meta_fingerprints(db)
                    results["hashed"] = mi.run_hash_pass(
                        db,
                        sources=sources or mi.default_sources(),
                        path_prefix=path_prefix,
                        missing_only=True,
                        cancel_event=self._cancel,
                        progress_cb=self._progress,
                    )
                    if self._cancel.is_set():
                        results["cancelled"] = True
                finally:
                    db.close()
        except Exception as exc:
            error = str(exc)
        finally:
            with self._lock:
                self._state["running"] = False
                self._state["cancelling"] = False
                self._state["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self._state["error"] = error
                self._state["results"] = results
                self._state["cancelled"] = bool(results.get("cancelled"))
                if error:
                    self._state["phase"] = "error"
                    self._state["message"] = error
                elif self._state["cancelled"]:
                    self._state["phase"] = "cancelled"
                    self._state["message"] = "Cancelled"
                else:
                    self._state["phase"] = "done"
                    self._state["message"] = "Complete"


job_manager = ScanJobManager()
