"""Stage definitions for the release check state machine.

Stages run strictly in order: lock file check, linters, tests, build and
finally the artifact assertions. Independent sub-checks inside a stage may
run concurrently, but the assertion stage never starts before the build
stage has completed and the artifacts have landed in ``dist/``.
"""

import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, List, Sequence

from diffsync.release_check.assertions import artifact_assertions
from diffsync.release_check.context import CheckContext
from diffsync.release_check.results import CheckResult, Status

CheckCallable = Callable[[CheckContext], CheckResult]


@dataclass
class Stage:
    """One state-machine stage: an ordered group of sub-checks."""

    name: str
    description: str
    run: Callable[[CheckContext], List[CheckResult]]


def _resolve(executable: str) -> str:
    return shutil.which(executable) or executable


def _tail(text: str, lines: int = 15) -> str:
    kept = [line for line in text.strip().splitlines() if line.strip()]
    return "\n".join(kept[-lines:])


def run_command_check(name: str, argv: Sequence[str], context: CheckContext) -> CheckResult:
    """Run one external command and convert its outcome into a CheckResult."""
    start = time.monotonic()
    context.log(f"$ {' '.join(argv)}")
    try:
        proc = subprocess.run(
            list(argv),
            cwd=str(context.root),
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return CheckResult(
            name=name,
            status=Status.FAILED,
            exit_code=None,
            reason=f"executable not found: {argv[0]}",
            duration=time.monotonic() - start,
        )
    output = (proc.stdout or "") + (proc.stderr or "")
    context.log(output)
    duration = time.monotonic() - start
    if proc.returncode != 0:
        return CheckResult(
            name=name,
            status=Status.FAILED,
            exit_code=proc.returncode,
            reason=_tail(output) or f"command exited with {proc.returncode}",
            duration=duration,
        )
    return CheckResult(name=name, status=Status.PASSED, exit_code=0, duration=duration)


def run_checks_concurrently(checks: Sequence[CheckCallable], context: CheckContext) -> List[CheckResult]:
    """Run independent sub-checks concurrently, preserving definition order."""
    if len(checks) == 1:
        return [checks[0](context)]
    with ThreadPoolExecutor(max_workers=len(checks)) as executor:
        return list(executor.map(lambda check: check(context), checks))


def _lock_stage(context: CheckContext) -> List[CheckResult]:
    return [
        run_command_check(
            "poetry-check-lock",
            [_resolve("poetry"), "check", "--lock"],
            context,
        )
    ]


def _lint_stage(context: CheckContext) -> List[CheckResult]:
    checks = [
        lambda ctx: run_command_check("ruff", [_resolve("ruff"), "check", "."], ctx),
        lambda ctx: run_command_check("mypy", [sys.executable, "-m", "mypy", "diffsync/"], ctx),
    ]
    return run_checks_concurrently(checks, context)


def _pytest_stage(context: CheckContext) -> List[CheckResult]:
    return [run_command_check("pytest", [sys.executable, "-m", "pytest"], context)]


def _build_stage(context: CheckContext) -> List[CheckResult]:
    return [run_command_check("poetry-build", [_resolve("poetry"), "build"], context)]


def _assertions_stage(context: CheckContext) -> List[CheckResult]:
    if not context.dist_dir.is_dir():
        return [
            CheckResult(
                name="artifact-assertions",
                status=Status.FAILED,
                exit_code=1,
                reason=f"dist directory {context.dist_dir} does not exist; build stage must run first",
            )
        ]
    return run_checks_concurrently(artifact_assertions(), context)


def default_stages() -> List[Stage]:
    """Return the ordered release check stages."""
    return [
        Stage(name="lock", description="Verify poetry.lock is consistent with pyproject.toml", run=_lock_stage),
        Stage(name="lint", description="Run ruff and mypy", run=_lint_stage),
        Stage(name="pytest", description="Run the pytest suite", run=_pytest_stage),
        Stage(name="build", description="Build sdist and wheel with poetry", run=_build_stage),
        Stage(name="assertions", description="Assert the built artifacts are releasable", run=_assertions_stage),
    ]
