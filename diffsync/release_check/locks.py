"""A single file lock shared by every writer of the release check run."""

import os
import sys
import threading
from pathlib import Path
from types import TracebackType
from typing import Optional, Type

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class FileLock:
    """Cross-thread and cross-process advisory lock backed by a lock file."""

    def __init__(self, path: Path) -> None:
        """Store the lock file path; the file is created on first acquire."""
        self._path = path
        self._thread_lock = threading.Lock()
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        """Acquire the thread lock first, then the OS-level file lock."""
        self._thread_lock.acquire()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(str(self._path), os.O_CREAT | os.O_RDWR)
            if sys.platform == "win32":
                msvcrt.locking(self._fd, msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(self._fd, fcntl.LOCK_EX)
        except Exception:
            self._thread_lock.release()
            raise

    def release(self) -> None:
        """Release the OS-level file lock, then the thread lock."""
        try:
            if self._fd is not None:
                if sys.platform == "win32":
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
                self._fd = None
        finally:
            self._thread_lock.release()

    def __enter__(self) -> "FileLock":
        """Acquire the lock when entering the context."""
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        """Release the lock when leaving the context."""
        self.release()
