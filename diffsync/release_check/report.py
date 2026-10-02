"""Disk writers for release check artifacts; all three share one file lock."""

import json
from pathlib import Path
from typing import Any, Dict, Optional

from diffsync.release_check.locks import FileLock


class _LockedWriter:
    """Base class for writers that serialize access through the shared lock."""

    def __init__(self, path: Path, lock: FileLock) -> None:
        """Store the target path and the shared lock."""
        self._path = path
        self._lock = lock

    @property
    def path(self) -> Path:
        """Return the file this writer owns."""
        return self._path

    def _write_text(self, content: str) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_name(self._path.name + ".tmp")
            tmp_path.write_text(content, encoding="utf-8")
            tmp_path.replace(self._path)


class ReportWriter(_LockedWriter):
    """Writes release-report.json; a rerun of the same version overwrites it."""

    def write(self, report: Dict[str, Any]) -> None:
        """Atomically overwrite the report under the shared lock."""
        self._write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    def read(self) -> Optional[Dict[str, Any]]:
        """Read the current report, or None if no run has been recorded yet."""
        with self._lock:
            if not self._path.exists():
                return None
            return json.loads(self._path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


class StateWriter(_LockedWriter):
    """Writes the .release-state.json checkpoint used by --resume."""

    def write(self, version: str, stage_status: Dict[str, str]) -> None:
        """Atomically overwrite the resume checkpoint under the shared lock."""
        payload = {"version": version, "stages": stage_status}
        self._write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def read(self) -> Optional[Dict[str, Any]]:
        """Read the current checkpoint, or None if no run has been recorded yet."""
        with self._lock:
            if not self._path.exists():
                return None
            return json.loads(self._path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


class SummaryWriter(_LockedWriter):
    """Writes the human-readable release-summary.txt table."""

    def write(self, summary: str) -> None:
        """Atomically overwrite the summary under the shared lock."""
        self._write_text(summary if summary.endswith("\n") else summary + "\n")
