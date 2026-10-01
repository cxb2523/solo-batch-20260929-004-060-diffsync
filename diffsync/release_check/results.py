"""Result and status types for the release check state machine."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Status(str, Enum):
    """Lifecycle status of a stage, a single check, or a whole run."""

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ABORTED = "aborted"
    SKIPPED = "skipped"


# Token used in the terminal summary table. A finished, releasable run must
# show PASS for every item; anything else means the run is not done.
DISPLAY_TOKENS = {
    Status.PENDING: "PENDING",
    Status.RUNNING: "RUNNING",
    Status.PASSED: "PASS",
    Status.FAILED: "FAIL",
    Status.ABORTED: "ABORT",
    Status.SKIPPED: "SKIP",
}


@dataclass
class CheckResult:
    """Outcome of a single sub-check inside a stage."""

    name: str
    status: Status
    exit_code: Optional[int] = None
    reason: Optional[str] = None
    duration: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable representation of this check."""
        return {
            "name": self.name,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "reason": self.reason,
            "duration": round(self.duration, 3),
        }


@dataclass
class StageResult:
    """Outcome of one state-machine stage, aggregating its sub-checks."""

    name: str
    status: Status = Status.PENDING
    checks: List[CheckResult] = field(default_factory=list)
    duration: float = 0.0

    @property
    def exit_code(self) -> Optional[int]:
        """Return the first non-zero exit code among the checks, if any."""
        for check in self.checks:
            if check.exit_code:
                return check.exit_code
        return 0 if self.status == Status.PASSED else None

    @property
    def reason(self) -> Optional[str]:
        """Return the failure reason of the first non-passing check, if any."""
        for check in self.checks:
            if check.status not in (Status.PASSED, Status.SKIPPED) and check.reason:
                return check.reason
        return None

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable representation of this stage."""
        return {
            "name": self.name,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "reason": self.reason,
            "duration": round(self.duration, 3),
            "checks": [check.as_dict() for check in self.checks],
        }
