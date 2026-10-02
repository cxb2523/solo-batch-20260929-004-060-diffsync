"""Orchestration of the release check pipeline via an explicit state machine."""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from diffsync.release_check import checks
from diffsync.release_check.locks import FileLock
from diffsync.release_check.report import ReportWriter, StateWriter, SummaryWriter
from diffsync.release_check.stages import CheckOutcome, RunState, RunStatus, StageState, StageStatus

Check = Callable[[checks.CheckContext], CheckOutcome]


@dataclass
class StageDef:
    """Static definition of a pipeline stage."""

    name: str
    title: str
    checks: List[Check]
    parallel: bool = False


STAGES: List[StageDef] = [
    StageDef("lock", "Lock file check", [checks.check_lock]),
    StageDef("lint", "Ruff and mypy", [checks.check_ruff, checks.check_mypy], parallel=True),
    StageDef("pytest", "Pytest suite", [checks.check_pytest]),
    StageDef("build", "Poetry build", [checks.check_build]),
    StageDef(
        "assertions",
        "Artifact assertions",
        [checks.assert_py_typed, checks.assert_manifest, checks.assert_versions, checks.assert_towncrier],
        parallel=True,
    ),
]


class ReleaseCheckRunner:
    """Drive the release check state machine stage by stage."""

    def __init__(
        self,
        project_root: Optional[Path] = None,
        resume: bool = False,
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        """Resolve paths, shared lock and writers, and build the initial run state."""
        self.project_root = Path(project_root) if project_root else Path(__file__).resolve().parents[2]
        env_vars: Dict[str, str] = dict(env) if env is not None else dict(os.environ)
        self.pyproject = checks.load_pyproject(self.project_root)
        self.version = self.pyproject["tool"]["poetry"]["version"]
        self.run_id = env_vars.get("DIFFSYNC_RELEASE_RUN_ID") or env_vars.get("GITHUB_RUN_ID") or None
        report_dir_override = env_vars.get("DIFFSYNC_RELEASE_REPORT_DIR")
        if report_dir_override:
            report_dir = Path(report_dir_override)
        elif self.run_id:
            report_dir = self.project_root / "build" / "release-reports" / self.run_id
        else:
            report_dir = self.project_root / "build"
        self.report_dir = report_dir
        self.resume = resume
        lock = FileLock(report_dir / ".release-check.lock")
        self.reports = ReportWriter(report_dir / "release-report.json", lock)
        self.states = StateWriter(report_dir / ".release-state.json", lock)
        self.summaries = SummaryWriter(report_dir / "release-summary.txt", lock)
        self.state = RunState(version=self.version, run_id=self.run_id)
        self._previous_report: Dict[str, Any] = {}
        for stage_def in STAGES:
            self.state.stages.append(StageState(name=stage_def.name, title=stage_def.title))

    def run(self) -> int:
        """Execute the pipeline; abort on the first failing stage. Returns the exit code."""
        previous: Dict[str, str] = {}
        self._previous_report = {}
        if self.resume:
            previous = self._load_previous()
            self._previous_report = self.reports.read() or {}
        self.state.transition(RunStatus.RUNNING)
        self._checkpoint()
        aborted = False
        for index, stage_def in enumerate(STAGES):
            stage = self.state.stages[index]
            if previous and previous.get(stage_def.name) == StageStatus.PASSED.value:
                stage.mark_resumed(self._previous_checks(stage_def.name))
                self._emit(stage, "PASS")
                self._checkpoint()
                continue
            stage.transition(StageStatus.RUNNING)
            self._checkpoint()
            outcomes = self._run_stage(stage_def)
            stage.checks = outcomes
            if all(outcome.status is StageStatus.PASSED for outcome in outcomes):
                stage.transition(StageStatus.PASSED)
                self._emit(stage, "PASS")
            else:
                stage.transition(StageStatus.FAILED)
                self._emit(stage, "FAIL")
                aborted = True
                self._checkpoint()
                break
            self._checkpoint()
        if aborted:
            for stage in self.state.stages:
                if stage.status is StageStatus.PENDING:
                    stage.transition(StageStatus.SKIPPED)
                    self._emit(stage, "SKIP")
            self.state.transition(RunStatus.ABORTED)
            exit_code = 1
        else:
            self.state.transition(RunStatus.PASSED)
            exit_code = 0
        self._finalize()
        return exit_code

    def _context(self) -> checks.CheckContext:
        return checks.CheckContext(
            project_root=self.project_root,
            dist_dir=self.project_root / "dist",
            version=self.version,
            pyproject=self.pyproject,
        )

    def _run_stage(self, stage_def: StageDef) -> List[CheckOutcome]:
        context = self._context()
        if stage_def.parallel and len(stage_def.checks) > 1:
            outcomes = []
            with ThreadPoolExecutor(max_workers=len(stage_def.checks)) as pool:
                futures = [pool.submit(check, context) for check in stage_def.checks]
                for future in as_completed(futures):
                    outcomes.append(future.result())
                    self._checkpoint()
            return outcomes
        outcomes = []
        for check in stage_def.checks:
            outcomes.append(check(context))
            self._checkpoint()
        return outcomes

    def _load_previous(self) -> Dict[str, str]:
        checkpoint = self.states.read()
        if not checkpoint or checkpoint.get("version") != self.version:
            return {}
        self.state.resumed = True
        return checkpoint.get("stages", {})  # type: ignore[no-any-return]

    def _previous_checks(self, stage_name: str) -> List[CheckOutcome]:
        for stage in self._previous_report.get("stages", []):
            if stage.get("name") == stage_name:
                return [CheckOutcome.from_dict(check) for check in stage.get("checks", [])]
        return []

    def _checkpoint(self) -> None:
        self.reports.write(self.state.to_dict())
        self.states.write(self.version, {stage.name: stage.status.value for stage in self.state.stages})

    def _finalize(self) -> None:
        self._checkpoint()
        summary = self._render_summary()
        self.summaries.write(summary)
        print()
        print(summary)

    def _emit(self, stage: StageState, verdict: str) -> None:
        line = f"{verdict} {stage.name}"
        if verdict == "FAIL":
            line += f" (exit {stage.exit_code}): {stage.reason}"
        print(line, flush=True)

    def _render_summary(self) -> str:
        lines = [
            "== Release check summary ==",
            f"version: {self.version}",
            f"run:     {self.run_id or 'local'}",
            f"status:  {self.state.status.value.upper()}",
            "",
            f"{'stage':<12} {'status':<8} {'exit':<5} reason",
        ]
        for stage in self.state.stages:
            status = "PASS" if stage.status is StageStatus.PASSED else stage.status.value.upper()
            lines.append(f"{stage.name:<12} {status:<8} {stage.exit_code:<5} {stage.reason}".rstrip())
        return "\n".join(lines)
