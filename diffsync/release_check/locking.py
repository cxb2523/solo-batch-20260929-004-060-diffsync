"""Cross-platform advisory file lock shared by the report writers."""

import os
import sys
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import IO, Optional, Type

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class FileLock:
    """Advisory file lock guarding every writer of the release report directory.

    The same instance is shared by the report writer, the state store and the
    run log writer so that concurrent sub-checks (and concurrently triggered
    processes sharing a report directory) never interleave writes.
    """

    def __init__(self, path: Path, timeout: float = 30.0) -> None:
        """Create a lock guarding the given lock-file path."""
        self._path = path
        self._timeout = timeout
        self._thread_lock = threading.RLock()
        self._handle: Optional[IO[bytes]] = None

    def acquire(self) -> None:
        """Acquire the lock, blocking (with timeout) until it is available."""
        with self._thread_lock:
            if self._handle is not None:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self._path, "a+b")  # noqa: SIM115
            deadline = time.monotonic() + self._timeout
            while True:
                try:
                    if sys.platform == "win32":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        handle.close()
                        raise TimeoutError(f"Timed out acquiring file lock {self._path}") from None
                    time.sleep(0.05)
            self._handle = handle

    def release(self) -> None:
        """Release the lock if held."""
        with self._thread_lock:
            if self._handle is None:
                return
            handle, self._handle = self._handle, None
            try:
                if sys.platform == "win32":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def __enter__(self) -> "FileLock":
        """Acquire the lock as a context manager."""
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        """Release the lock when leaving the context manager."""
        self.release()


def atomic_write_text(path: Path, content: str) -> None:
    """Write text to a path atomically, replacing any previous content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)
