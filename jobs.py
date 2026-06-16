"""Shared machinery for single-worker background jobs.

Scans, deletions, and Media Org passes all run the same way: one daemon worker
at a time, a lock-guarded status dict that the web UI polls for a progress bar,
and a cancel that stops the queue. BaseJobManager owns that machinery so each
concrete manager only declares its state fields (_idle) and its work (a _run
method), using the lock-safe mutators below instead of re-implementing locking.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone


def now_iso() -> str:
    """UTC timestamp, second precision — the format stored in every job state."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class BaseJobManager:
    """One background worker, a lock-guarded status snapshot, lock-safe mutators.

    Subclasses must override `_idle()` to return their state template (which must
    include the keys `running`, `cancelled`, `finished_at`, and — if they use
    `_err` — `failed` and `errors`). They drive the bar with `_set/_inc/_err`,
    check `_cancelled()` in their loop, launch via `_launch`, and end in a
    `finally` that calls `_finish()`.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state = self._idle()

    @staticmethod
    def _idle() -> dict:
        raise NotImplementedError

    def status(self) -> dict:
        """A snapshot of the live state, safe to hand to the UI."""
        with self._lock:
            return dict(self._state)

    # -- lock-safe state mutators (call from the worker thread) --
    def _set(self, **kw):
        with self._lock:
            self._state.update(kw)

    def _inc(self, key: str, n: int = 1):
        with self._lock:
            self._state[key] += n

    def _err(self, msg: str, limit: int = 20):
        with self._lock:
            self._state["failed"] += 1
            if len(self._state["errors"]) < limit:
                self._state["errors"].append(msg)

    def _cancelled(self) -> bool:
        with self._lock:
            return self._state["cancelled"]

    def _finish(self, **extra):
        """Mark the job finished. Pass any final field overrides as kwargs."""
        with self._lock:
            self._state["running"] = False
            self._state["finished_at"] = now_iso()
            self._state.update(extra)

    def _launch(self, target, args: tuple = ()):
        """Spawn the daemon worker. Call while holding self._lock."""
        self._thread = threading.Thread(target=target, args=args, daemon=True)
        self._thread.start()
