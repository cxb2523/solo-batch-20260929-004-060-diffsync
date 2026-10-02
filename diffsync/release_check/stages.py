"""Explicit state machine for the release check pipeline."""

import enum
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, TypeVar


class StageStatus(enum.Enum):
    """Lifecycle states of a single pipeline stage."""

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunStatus(enum.Enum):
    """Lifecycle states of a full release check run."""

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    ABORTED = "aborted"


class InvalidTransition(RuntimeError):
    """Raised when the state machine is asked to perform an illegal transition."""


StatusT = TypeVar("StatusT", StageStatus, RunStatus)

_STAGE_TRANSITIONS: Dict[StageStatus, Set[StageStatus]] = {
    StageStatus.PENDING: {StageStatus.RUNNING, StageStatus.SKIPPED},
    StageStatus.RUNNING: {StageStatus.PASSED, StageStatus.FAILED},
    StageStatus.PASSED: set(),
    StageStatus.FAILED: set(),
    StageStatus.SKIPPED: set(),
}

_RUN_TRANSITIONS: Dict[RunStatus, Set[RunStatus]] = {
    RunStatus.PENDING: {RunStatus.RUNNING},
    RunStatus.RUNNING: {RunStatus.PASSED, RunStatus.ABORTED},
    RunStatus.PASSED: set(),
    RunStatus.ABORTED: set(),
}


def _transition(table: Dict[StatusT, Set[StatusT]], current: StatusT, target: StatusT) -> StatusT:
    if target not in table[current]:
        raise InvalidTransition(f"Illegal transition {current.value} -> {target.value}")
    return target


@dataclass
class CheckOutcome:
    """Result of a single sub-check inside a stage."""

    name: str
    status: StageStatus = StageStatus.PENDING
    exit_code: Optional[int] = None
    reason: str = ""
    duration: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "name": self.name,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "reason": self.reason,
            "duration": round(self.duration, 3),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CheckOutcome":
        """Rebuild an outcome from its serialized form."""
        return cls(
            name=data["name"],
            status=StageStatus(data["status"]),
            exit_code=data.get("exit_code"),
            reason=data.get("reason", ""),
            duration=data.get("duration", 0.0),
        )


@dataclass
class StageState:
    """State of one pipeline stage and its sub-checks."""

    name: str
    title: str
    status: StageStatus = StageStatus.PENDING
    checks: List[CheckOutcome] = field(default_factory=list)

    def transition(self, target: StageStatus) -> None:
        """Move the stage to the target status if the transition is legal."""
        self.status = _transition(_STAGE_TRANSITIONS, self.status, target)

    def mark_resumed(self, checks: List[CheckOutcome]) -> None:
        """Re-enter a previously passed stage without re-executing its checks."""
        self.status = StageStatus.PASSED
        self.checks = checks

    @property
    def exit_code(self) -> int:
        """First non-zero sub-check exit code, or zero."""
        for check in self.checks:
            if check.exit_code:
                return check.exit_code
        return 0

    @property
    def reason(self) -> str:
        """First failing sub-check reason, or an empty string."""
        for check in self.checks:
            if check.status is StageStatus.FAILED:
                return f"{check.name}: {check.reason}" if check.reason else check.name
        return ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "name": self.name,
            "title": self.title,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "reason": self.reason,
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass
class RunState:
    """State of the whole release check run."""

    version: str
    run_id: Optional[str] = None
    status: RunStatus = RunStatus.PENDING
    resumed: bool = False
    stages: List[StageState] = field(default_factory=list)

    def transition(self, target: RunStatus) -> None:
        """Move the run to the target status if the transition is legal."""
        self.status = _transition(_RUN_TRANSITIONS, self.status, target)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "version": self.version,
            "run_id": self.run_id,
            "status": self.status.value,
            "resumed": self.resumed,
            "stages": [stage.to_dict() for stage in self.stages],
        }
