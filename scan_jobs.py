"""Background scan/hash jobs for the web UI."""

from __future__ import annotations

import threading

import media_index as mi
from jobs import BaseJobManager, now_iso


class ScanJobManager(BaseJobManager):
    """A scan/hash pass on one background worker. Unlike the delete/organize
    managers it cancels through a threading.Event handed deep into the scan,
    and computes a richer phase/message/results snapshot when it finishes."""

    def __init__(self):
        super().__init__()
        self._cancel = threading.Event()

    @staticmethod
    def _idle() -> dict:
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

    def start(self, sources: list[str] | None, path_prefix: str = "",
              rescan: bool = True, hash_missing: bool = True) -> bool:
        with self._lock:
            if self._state["running"]:
                return False
            self._cancel = threading.Event()
            self._state = self._idle()
            self._state.update(running=True, phase="starting",
                               message="Starting…", started_at=now_iso())
            self._launch(self._run, (sources, path_prefix, rescan, hash_missing))
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
        # Called by media_index as a progress callback with arbitrary fields;
        # only touch known keys, and skip None so a partial update can't wipe a
        # value. (Differs from BaseJobManager._set, which writes unconditionally.)
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
            cancelled = bool(results.get("cancelled"))
            if error:
                phase, message = "error", error
            elif cancelled:
                phase, message = "cancelled", "Cancelled"
            else:
                phase, message = "done", "Complete"
            self._finish(cancelling=False, error=error, results=results,
                         cancelled=cancelled, phase=phase, message=message)


job_manager = ScanJobManager()
