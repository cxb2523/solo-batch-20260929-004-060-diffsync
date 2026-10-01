"""Release check state machine for diffsync.

This package drives the release verification pipeline (lock file check,
linters, tests, build, artifact assertions) as an explicit state machine.
It is only a pre-release backstop: py.typed and package data files are
declared exclusively via ``include``/``packages`` in ``pyproject.toml``,
and wherever this package and ``pyproject.toml`` disagree, ``pyproject.toml``
wins.
"""

from diffsync.release_check.machine import ReleaseCheckMachine
from diffsync.release_check.results import CheckResult, StageResult, Status

__all__ = ["CheckResult", "ReleaseCheckMachine", "StageResult", "Status"]
