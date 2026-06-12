"""Cross-process lock so insights rebuilds do not overlap (upload thread, timer, CLI)."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from typing import Iterator


def insights_lock_path() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "instance", "rebuild-insights.lock")


@contextmanager
def insights_rebuild_lock(*, block: bool = False) -> Iterator[bool]:
    """
    Acquire an exclusive flock on instance/rebuild-insights.lock.

    Yields True when the lock was acquired, False when another rebuild is running
    (non-blocking mode only).
    """
    path = insights_lock_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o664)
    acquired = False
    try:
        flags = fcntl.LOCK_EX if block else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
            acquired = True
        except BlockingIOError:
            acquired = False
        yield acquired
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)
