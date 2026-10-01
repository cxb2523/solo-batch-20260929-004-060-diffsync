"""Unit tests for the diffsync.release_check state machine."""

import json
from pathlib import Path

import pytest

from diffsync.release_check.assertions import (
    is_valid_fragment_name,
    read_init_version,
    towncrier_fragment_types,
)
from diffsync.release_check.machine import EXIT_ABORTED, EXIT_FAILED, EXIT_PASSED, ReleaseCheckMachine
from diffsync.release_check.report import REPORT_FILENAME, default_report_dir
from diffsync.release_check.results import CheckResult, Status
from diffsync.release_check.stages import Stage

PYPROJECT = """
[tool.poetry]
name = "diffsync"
version = "1.2.3"

[[tool.towncrier.type]]
directory = "fixed"

[[tool.towncrier.type]]
directory = "housekeeping"
"""


def make_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    (root / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    return root


def make_machine(tmp_path, stages):
    root = make_root(tmp_path)
    report_dir = tmp_path / "report"
    return ReleaseCheckMachine(
        root=root,
        report_dir=report_dir,
        dist_dir=tmp_path / "dist",
        stages=stages,
        environ={},
        printer=lambda _line: None,
    )


def ok_stage(name):
    return Stage(
        name=name,
        description=name,
        run=lambda _ctx: [CheckResult(name=f"{name}-check", status=Status.PASSED, exit_code=0)],
    )


def failing_stage(name):
    return Stage(
        name=name,
        description=name,
        run=lambda _ctx: [CheckResult(name=f"{name}-check", status=Status.FAILED, exit_code=3, reason="boom")],
    )


def interrupting_stage(name):
    def run(_ctx):
        raise KeyboardInterrupt

    return Stage(name=name, description=name, run=run)


def read_report(machine):
    return json.loads((machine.report_dir / REPORT_FILENAME).read_text(encoding="utf-8"))


def test_all_stages_pass(tmp_path):
    machine = make_machine(tmp_path, [ok_stage("one"), ok_stage("two")])
    assert machine.run() == EXIT_PASSED
    report = read_report(machine)
    assert report["status"] == "passed"
    assert [stage["status"] for stage in report["stages"]] == ["passed", "passed"]
    assert report["version"] == "1.2.3"


def test_failure_aborts_remaining_stages(tmp_path):
    machine = make_machine(tmp_path, [ok_stage("one"), failing_stage("two"), ok_stage("three")])
    assert machine.run() == EXIT_FAILED
    report = read_report(machine)
    statuses = {stage["name"]: stage["status"] for stage in report["stages"]}
    assert statuses == {"one": "passed", "two": "failed", "three": "pending"}
    failed = next(stage for stage in report["stages"] if stage["name"] == "two")
    assert failed["exit_code"] == 3
    assert failed["reason"] == "boom"


def test_interrupt_marks_aborted_not_failed(tmp_path):
    machine = make_machine(tmp_path, [ok_stage("one"), interrupting_stage("two"), ok_stage("three")])
    assert machine.run() == EXIT_ABORTED
    report = read_report(machine)
    assert report["status"] == "aborted"
    statuses = {stage["name"]: stage["status"] for stage in report["stages"]}
    assert statuses["two"] == "aborted"


def test_resume_skips_passed_stages(tmp_path):
    calls = []
    stage_one = Stage(
        name="one",
        description="one",
        run=lambda _ctx: calls.append("one") or [CheckResult(name="one-check", status=Status.PASSED, exit_code=0)],
    )
    machine = make_machine(tmp_path, [stage_one, interrupting_stage("two")])
    assert machine.run() == EXIT_ABORTED
    assert calls == ["one"]

    calls.clear()
    machine2 = make_machine(tmp_path, [stage_one, ok_stage("two")])
    assert machine2.run(resume=True) == EXIT_PASSED
    assert calls == [], "passed stage must not be re-executed on resume"
    report = read_report(machine2)
    assert report["status"] == "passed"


def test_rerun_overwrites_report(tmp_path):
    machine = make_machine(tmp_path, [failing_stage("one")])
    machine.run()
    machine2 = make_machine(tmp_path, [ok_stage("one")])
    machine2.run()
    report = read_report(machine2)
    assert report["status"] == "passed"
    assert len(report["stages"]) == 1


def test_report_dir_uses_run_id(tmp_path):
    root = make_root(tmp_path)
    assert default_report_dir(root, {}) == root / "build"
    assert default_report_dir(root, {"GITHUB_RUN_ID": "42"}) == root / "build" / "42"


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("346.fixed", True),
        ("+main.housekeeping", True),
        ("1.2.fixed", True),
        (".gitignore", True),
        ("346.unknown", False),
        ("fixed", False),
        (".fixed", False),
        ("+.fixed", False),
        ("346.fixed.bak", False),
    ],
)
def test_towncrier_fragment_names(filename, expected):
    valid_types = {"fixed", "housekeeping"}
    assert is_valid_fragment_name(filename, valid_types) is expected


def test_towncrier_types_from_pyproject(tmp_path):
    machine = make_machine(tmp_path, [])
    assert towncrier_fragment_types(machine.pyproject) == {"fixed", "housekeeping"}


def test_read_init_version(tmp_path):
    init_file = tmp_path / "__init__.py"
    init_file.write_text('__version__ = "2.2.3a0"\n', encoding="utf-8")
    assert read_init_version(init_file) == "2.2.3a0"
    init_file.write_text("VERSION = 1\n", encoding="utf-8")
    assert read_init_version(init_file) is None


def test_real_package_has_version():
    init_file = Path(__file__).parents[2] / "diffsync" / "__init__.py"
    assert read_init_version(init_file) is not None
