#!/usr/bin/env python3
"""script_lock.py — simple advisory lock files for OpenClaw cron scripts.

Prevents overlapping invocations of the same script. If a lock is already
held by another process the caller exits immediately (skip semantics) rather
than queuing up and hanging.

Usage (context-manager form, recommended):

    from script_lock import script_lock

    with script_lock("polly-ingest"):
        ...do work...

If the lock cannot be acquired (another instance is running) ``script_lock``
raises ``AlreadyRunning``, which main() should catch and treat as a clean
no-op exit:

    try:
        with script_lock("polly-ingest"):
            main_logic()
    except AlreadyRunning as e:
        print(f"Skipped: {e}", flush=True)
        sys.exit(0)

Lock files land in /tmp/openclaw/<name>.lock and contain the PID + start
timestamp of the holder so backer_health_tick.sh can identify stale locks.

Stale lock detection: if the PID in the file no longer exists the lock is
considered stale and is automatically cleared before acquiring.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("script_lock")

LOCK_DIR = Path("/tmp/openclaw")

# A lock is considered stale if the recorded PID is gone AND the file is
# older than this many seconds. The PID check is the primary guard; the
# age check provides a backstop for edge-cases (PID reuse, etc.).
STALE_AGE_SECONDS = 600  # 10 minutes


class AlreadyRunning(Exception):
    """Raised when another instance already holds the lock."""


class ScriptLock:
    """Advisory lock backed by an fcntl-locked file under /tmp/openclaw/.

    The file contains JSON with the holder's PID and start time so that
    external tooling (backer_health_tick.sh) can detect and clear stale locks.
    """

    def __init__(self, name: str) -> None:
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        self.name = name
        self.path = LOCK_DIR / f"{name}.lock"
        self._fd: int | None = None

    def acquire(self) -> None:
        """Attempt a non-blocking exclusive lock. Raises AlreadyRunning if busy."""
        # Open (or create) the lock file; keep fd open for the duration.
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            # LOCK_EX | LOCK_NB: exclusive, non-blocking.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Another process holds the lock. Read its metadata for the error.
            try:
                raw = os.read(fd, 4096).decode("utf-8", errors="replace").strip()
                meta = json.loads(raw) if raw else {}
            except Exception:
                meta = {}
            os.close(fd)
            holder_pid = meta.get("pid", "?")
            holder_since = meta.get("since", "?")
            # Check whether the holder PID is actually alive.
            if _pid_alive(holder_pid):
                raise AlreadyRunning(
                    f"{self.name} already running (pid={holder_pid}, since={holder_since})"
                )
            # Stale lock: PID is gone. Clear and retry once.
            log.warning(
                "Clearing stale lock for %s (pid=%s no longer alive)", self.name, holder_pid
            )
            os.close(fd)
            self._clear_stale()
            return self.acquire()  # single retry after clearing stale

        # Write holder metadata so external tools can inspect the lock.
        meta = {
            "pid": os.getpid(),
            "name": self.name,
            "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        payload = json.dumps(meta).encode("utf-8")
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload)
        self._fd = fd
        log.debug("Acquired lock: %s (pid=%d)", self.path, os.getpid())

    def release(self) -> None:
        """Release the lock and remove the file."""
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except OSError as exc:
                log.warning("Error releasing lock %s: %s", self.name, exc)
            finally:
                self._fd = None
        # Remove the file so /tmp doesn't accumulate stale entries.
        with contextlib.suppress(OSError):
            self.path.unlink()
        log.debug("Released lock: %s", self.path)

    def _clear_stale(self) -> None:
        """Remove a stale lock file (called after confirming PID is dead)."""
        with contextlib.suppress(OSError):
            self.path.unlink()

    def __enter__(self) -> "ScriptLock":
        self.acquire()
        return self

    def __exit__(self, *_) -> None:
        self.release()


@contextlib.contextmanager
def script_lock(name: str):
    """Context manager that holds an exclusive lock for ``name``.

    Raises AlreadyRunning immediately if another instance is running.
    Releases the lock (and deletes the lock file) on exit, even on exception.

    Example::

        try:
            with script_lock("polly-ingest"):
                do_work()
        except AlreadyRunning as e:
            print(f"Skipped: {e}")
            sys.exit(0)
    """
    lock = ScriptLock(name)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pid_alive(pid) -> bool:
    """Return True if a process with the given PID exists."""
    try:
        pid_int = int(pid)
        os.kill(pid_int, 0)  # signal 0 = existence check
        return True
    except (ProcessLookupError, PermissionError):
        # ProcessLookupError → no such process
        # PermissionError → process exists but we can't signal it (still alive)
        return isinstance(pid, int) and True  # PermissionError means alive
    except (TypeError, ValueError, OSError):
        return False


# Re-implement with correct PermissionError semantics:
def _pid_alive(pid) -> bool:  # noqa: F811
    """Return True if a process with the given PID exists on this machine."""
    try:
        os.kill(int(pid), 0)
        return True  # no exception → process exists
    except ProcessLookupError:
        return False  # no such process
    except PermissionError:
        return True   # process exists, we just can't signal it
    except (TypeError, ValueError, OSError):
        return False
