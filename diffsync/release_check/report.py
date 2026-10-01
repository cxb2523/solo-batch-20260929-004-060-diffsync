"""Report directory writers: JSON report, resume state and run log.

All three writers share a single :class:`FileLock` instance so concurrent
sub-checks never interleave writes. Re-running the check for the same
version overwrites the previous report instead of appending to it; on CI,
concurrently triggered runs use per-run-id directories and never overwrite
each other.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from diffsync.release_check.locking import FileLock, atomic_write_text

REPORT_FILENAME = "release-report.json"
STATE_FILENAME = ".release-check-state.json"
LOG_FILENAME = "release-check.log"
LOCK_FILENAME = ".release-check.lock"


def default_report_dir(root: Path, environ: Optional[Mapping[str, str]] = None) -> Path:
    """Return the default report directory for a run.

    Locally this is ``build/`` (so the report lands in
    ``build/release-report.json``); on CI, concurrently triggered jobs each
    get their own ``build/<run-id>/`` directory so they never overwrite each
    other.
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    run_id = env.get("GITHUB_RUN_ID") or env.get("CI_RUN_ID")
    base = root / "build"
    return base / run_id if run_id else base


class ReportWriter:
    """Writes the machine-readable release report (``release-report.json``)."""

    def __init__(self, report_dir: Path, lock: FileLock) -> None:
        """Create a writer targeting ``report_dir`` guarded by ``lock``."""
        self.path = report_dir / REPORT_FILENAME
        self._lock = lock

    def write(self, report: Dict[str, Any]) -> None:
        """Persist the report, overwriting any previous report for this run."""
        with self._lock:
            atomic_write_text(self.path, json.dumps(report, indent=2, sort_keys=True) + "\n")


class StateStore:
    """Persists per-stage status so an aborted run can be resumed."""

    def __init__(self, report_dir: Path, lock: FileLock) -> None:
        """Create a store targeting ``report_dir`` guarded by ``lock``."""
        self.path = report_dir / STATE_FILENAME
        self._lock = lock

    def load(self) -> Dict[str, Any]:
        """Load the persisted state, or an empty state if none exists."""
        with self._lock:
            if not self.path.exists():
                return {}
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
            except (OSError, ValueError):
                return {}

    def save(self, state: Dict[str, Any]) -> None:
        """Persist the state, overwriting any previous state for this run."""
        with self._lock:
            atomic_write_text(self.path, json.dumps(state, indent=2, sort_keys=True) + "\n")

    def clear(self) -> None:
        """Remove any persisted state (used when starting a fresh run)."""
        with self._lock:
            self.path.unlink(missing_ok=True)


class RunLogWriter:
    """Appends stage output to the run log (``release-check.log``)."""

    def __init__(self, report_dir: Path, lock: FileLock) -> None:
        """Create a writer targeting ``report_dir`` guarded by ``lock``."""
        self.path = report_dir / LOG_FILENAME
        self._lock = lock

    def log(self, text: str) -> None:
        """Append text to the run log."""
        if not text:
            return
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(text)
                if not text.endswith("\n"):
                    handle.write("\n")

    def reset(self) -> None:
        """Truncate the log (used when starting a fresh run)."""
        with self._lock:
            self.path.unlink(missing_ok=True)
