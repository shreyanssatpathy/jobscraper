"""A single-writer lock so scheduled polls never collide on the database.

SQLite allows one writer at a time. The Tier 1 and Workday schedules overlap by
design -- Workday takes ~11 minutes and Tier 1 runs every 20 -- so without this
a Tier 1 run landing mid-Workday would hit `database is locked`. The lock makes
the second run wait for the first instead of failing or corrupting a poll.
"""
from __future__ import annotations

import errno
import fcntl
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class Locked(RuntimeError):
    """Another poll holds the lock and the wait budget ran out."""


@contextmanager
def poll_lock(path: str | Path = ".poll.lock", wait: float = 900,
              interval: float = 5):
    """Hold an exclusive lock, waiting up to `wait` seconds to acquire it."""
    fh = open(path, "a+")
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                fh.close()
                raise
            if time.monotonic() >= deadline:
                fh.seek(0)
                holder = fh.read().strip() or "another poll"
                fh.close()
                raise Locked(f"lock held by {holder}")
            time.sleep(interval)
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid {os.getpid()} since "
                 f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}")
        fh.flush()
        yield
    finally:
        try:
            fh.seek(0)
            fh.truncate()
            fh.flush()
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()
