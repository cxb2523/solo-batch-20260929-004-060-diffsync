"""Explicit state machine driving the release check stages."""

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from diffsync.release_check.context import CheckContext, load_pyproject
from diffsync.release_check.locking import FileLock
from diffsync.release_check.report import (
    LOCK_FILENAME,
    ReportWriter,
    RunLogWriter,
    StateStore,
    default_report_dir,
)
from diffsync.release_check.results import DISPLAY_TOKENS, CheckResult, StageResult, Status
from diffsync.release_check.stages import Stage, default_stages

EXIT_PASSED = 0
EXIT_FAILED = 1
EXIT_ABORTED = 2

# Explicit transition table: PENDING -> RUNNING -> PASSED|FAILED|ABORTED.
_TRANSITIONS = {
    Status.PENDING: {Status.RUNNING, Status.SKIPPED},
    Status.RUNNING: {Status.PASSED, Status.FAILED, Status.ABORTED},
}


def _transition(stage: StageResult, new_status: Status) -> None:
    allowed = _TRANSITIONS.get(stage.status, set())
    if new_status not in allowed:
        raise ValueError(
            f"Illegal state transition for stage {stage.name!r}: {stage.status.value} -> {new_status.value}"
        )
    stage.status = new_status


def render_summary(report: Dict[str, Any]) -> str:
    """Render the terminal summary table for a finished (or aborted) run."""
    lines = [
        "",
        "=" * 78,
        f"Release check summary: version={report['version']} status={DISPLAY_TOKENS[Status(report['status'])]}",
        "=" * 78,
        f"{'STAGE':<14}{'CHECK':<24}{'STATUS':<9}{'EXIT':<6}REASON",
        "-" * 78,
    ]
    for stage in report["stages"]:
        checks = stage["checks"] or [None]
        for check in checks:
            if check is None:
                lines.append(
                    f"{stage['name']:<14}{'(not reached)':<24}{DISPLAY_TOKENS[Status(stage['status'])]:<9}{'-':<6}"
                )
                continue
            token = DISPLAY_TOKENS[Status(check["status"])]
            exit_code = "-" if check["exit_code"] is None else str(check["exit_code"])
            reason = (check["reason"] or "").splitlines()[0] if check["reason"] else ""
            lines.append(f"{stage['name']:<14}{check['name']:<24}{token:<9}{exit_code:<6}{reason}")
    lines.append("-" * 78)
    return "\n".join(lines)


class ReleaseCheckMachine:
    """Drive the release check stages as an explicit state machine.

    Any failure aborts the run immediately, but every completed stage, its
    exit code and its failure reason are persisted to the release report and
    rendered in the terminal summary. An interrupted run is marked
    ``aborted`` (not ``failed``) and can be re-entered with ``--resume``,
    which skips stages that already passed.
    """

    def __init__(
        self,
        root: Path,
        report_dir: Optional[Path] = None,
        dist_dir: Optional[Path] = None,
        stages: Optional[List[Stage]] = None,
        environ: Optional[Mapping[str, str]] = None,
        printer: Callable[[str], None] = print,
    ) -> None:
        """Create the machine; ``report_dir``/``dist_dir`` default to build/ and dist/."""
        self.root = Path(root)
        self.environ = os.environ if environ is None else environ
        self.report_dir = Path(report_dir) if report_dir else default_report_dir(self.root, self.environ)
        self.dist_dir = Path(dist_dir) if dist_dir else self.root / "dist"
        self.stages = stages if stages is not None else default_stages()
        self._printer = printer
        self.pyproject = load_pyproject(self.root)
        # The three writers (report, resume state, run log) share one lock.
        self._lock = FileLock(self.report_dir / LOCK_FILENAME)
        self._report_writer = ReportWriter(self.report_dir, self._lock)
        self._state_store = StateStore(self.report_dir, self._lock)
        self._log_writer = RunLogWriter(self.report_dir, self._lock)
        self.context = CheckContext(
            root=self.root,
            dist_dir=self.dist_dir,
            pyproject=self.pyproject,
            log=self._log_writer.log,
        )

    @property
    def version(self) -> str:
        """Return the project version from pyproject.toml (source of truth)."""
        return str(self.pyproject.get("tool", {}).get("poetry", {}).get("version", "unknown"))

    def run(self, resume: bool = False) -> int:
        """Run the state machine and return the process exit code."""
        started_at = datetime.now(timezone.utc)
        saved_stages: Dict[str, Any] = {}
        if resume:
            state = self._state_store.load()
            if state.get("version") == self.version:
                saved_stages = state.get("stages", {})
            else:
                self._printer("Saved state is stale or missing; starting a fresh run.")
        else:
            self._state_store.clear()
            self._log_writer.reset()

        stage_results: List[StageResult] = []
        run_status = Status.PASSED
        for stage in self.stages:
            saved = saved_stages.get(stage.name)
            if resume and saved and saved.get("status") == Status.PASSED.value:
                cached = StageResult(
                    name=stage.name,
                    status=Status.PASSED,
                    checks=[
                        CheckResult(
                            name=check["name"],
                            status=Status(check["status"]),
                            exit_code=check.get("exit_code"),
                            reason=check.get("reason"),
                            duration=check.get("duration", 0.0),
                        )
                        for check in saved.get("checks", [])
                    ],
                    duration=saved.get("duration", 0.0),
                )
                stage_results.append(cached)
                self._printer(f"PASS  {stage.name} (already passed, skipped by --resume)")
                continue

            result = StageResult(name=stage.name)
            _transition(result, Status.RUNNING)
            self._printer(f"==> {stage.name}: {stage.description}")
            start = time.monotonic()
            try:
                result.checks = stage.run(self.context)
            except KeyboardInterrupt:
                _transition(result, Status.ABORTED)
                result.duration = time.monotonic() - start
                stage_results.append(result)
                run_status = Status.ABORTED
                self._printer(f"ABORT {stage.name} (interrupted)")
                break
            result.duration = time.monotonic() - start
            for check in result.checks:
                token = DISPLAY_TOKENS[check.status]
                exit_code = "-" if check.exit_code is None else str(check.exit_code)
                self._printer(f"{token:<5} {stage.name}/{check.name} exit={exit_code} ({check.duration:.1f}s)")
                if check.reason and check.status != Status.PASSED:
                    self._printer(f"      reason: {check.reason.splitlines()[0]}")
            if all(check.status == Status.PASSED for check in result.checks) and result.checks:
                _transition(result, Status.PASSED)
            else:
                _transition(result, Status.FAILED)
                run_status = Status.FAILED
                stage_results.append(result)
                break
            stage_results.append(result)

        # Stages that were never reached stay PENDING in the report.
        reached = {result.name for result in stage_results}
        for stage in self.stages:
            if stage.name not in reached:
                stage_results.append(StageResult(name=stage.name, status=Status.PENDING))

        finished_at = datetime.now(timezone.utc)
        report = {
            "version": self.version,
            "run_id": self.environ.get("GITHUB_RUN_ID") or self.environ.get("CI_RUN_ID"),
            "status": run_status.value,
            "resumed": resume,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "stages": [result.as_dict() for result in stage_results],
        }
        self._report_writer.write(report)
        if run_status == Status.PASSED:
            self._state_store.clear()
        else:
            self._state_store.save(
                {
                    "version": self.version,
                    "status": run_status.value,
                    "stages": {result.name: result.as_dict() for result in stage_results},
                }
            )

        self._printer(render_summary(report))
        self._printer(f"Report written to {self._report_writer.path}")
        if run_status == Status.PASSED:
            return EXIT_PASSED
        if run_status == Status.ABORTED:
            return EXIT_ABORTED
        return EXIT_FAILED
