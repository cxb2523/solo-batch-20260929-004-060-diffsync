"""Individual checks executed by the release check pipeline stages."""

import re
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

from diffsync.release_check.stages import CheckOutcome, StageStatus


@dataclass
class CheckContext:
    """Shared context handed to every check."""

    project_root: Path
    dist_dir: Path
    version: str
    pyproject: Dict[str, Any]


def load_pyproject(project_root: Path) -> Dict[str, Any]:
    """Parse pyproject.toml, preferring stdlib tomllib with a toml fallback."""
    text = (project_root / "pyproject.toml").read_text(encoding="utf-8")
    try:
        import tomllib

        return tomllib.loads(text)  # type: ignore[no-any-return]
    except ModuleNotFoundError:
        import toml

        return toml.loads(text)  # type: ignore[no-any-return]


def _resolve(executable: str) -> str:
    return shutil.which(executable) or executable


def _run_command(command: List[str], cwd: Path) -> Tuple[int, str]:
    try:
        proc = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return 127, f"executable not found: {command[0]}"
    output = (proc.stdout + "\n" + proc.stderr).strip()
    return proc.returncode, output


def _tail(output: str, lines: int = 15) -> str:
    kept = output.splitlines()[-lines:]
    return "\n".join(kept)


def _run_as_outcome(name: str, commands: List[List[str]], context: CheckContext) -> CheckOutcome:
    started = time.monotonic()
    exit_code = 0
    reason = ""
    for command in commands:
        exit_code, output = _run_command(command, context.project_root)
        if exit_code != 0:
            reason = f"`{' '.join(command)}` exited {exit_code}: {_tail(output)}"
            break
    return CheckOutcome(
        name=name,
        status=StageStatus.PASSED if exit_code == 0 else StageStatus.FAILED,
        exit_code=exit_code,
        reason=reason,
        duration=time.monotonic() - started,
    )


def check_lock(context: CheckContext) -> CheckOutcome:
    """Verify that pyproject.toml and poetry.lock are consistent."""
    return _run_as_outcome("poetry-check", [[_resolve("poetry"), "check", "--lock"]], context)


def check_ruff(context: CheckContext) -> CheckOutcome:
    """Run ruff linting and format verification."""
    ruff = _resolve("ruff")
    commands = [
        [ruff, "check", "--output-format", "concise", "."],
        [ruff, "format", "--check", "."],
    ]
    return _run_as_outcome("ruff", commands, context)


def check_mypy(context: CheckContext) -> CheckOutcome:
    """Run mypy type checking over the diffsync package."""
    return _run_as_outcome("mypy", [[_resolve("mypy"), "diffsync/"]], context)


def check_pytest(context: CheckContext) -> CheckOutcome:
    """Run the pytest suite."""
    return _run_as_outcome("pytest", [[sys.executable, "-m", "pytest"]], context)


def check_build(context: CheckContext) -> CheckOutcome:
    """Build the sdist and wheel artifacts with poetry."""
    return _run_as_outcome("poetry-build", [[_resolve("poetry"), "build"]], context)


def _find_artifacts(context: CheckContext) -> Tuple[Path, Path]:
    wheels = sorted(context.dist_dir.glob(f"diffsync-{context.version}-*.whl"))
    sdists = sorted(context.dist_dir.glob(f"diffsync-{context.version}.tar.gz"))
    missing = []
    if not wheels:
        missing.append(f"diffsync-{context.version}-*.whl")
    if not sdists:
        missing.append(f"diffsync-{context.version}.tar.gz")
    if missing:
        raise FileNotFoundError(f"artifacts not found in {context.dist_dir}: {', '.join(missing)}")
    return wheels[-1], sdists[-1]


def _assertion_outcome(name: str, check_fn: Any, context: CheckContext) -> CheckOutcome:
    started = time.monotonic()
    try:
        check_fn(context)
    except (FileNotFoundError, ValueError) as exc:
        return CheckOutcome(
            name=name,
            status=StageStatus.FAILED,
            exit_code=1,
            reason=str(exc),
            duration=time.monotonic() - started,
        )
    return CheckOutcome(name=name, status=StageStatus.PASSED, exit_code=0, duration=time.monotonic() - started)


def _wheel_members(wheel_path: Path) -> Set[str]:
    with zipfile.ZipFile(wheel_path) as archive:
        return {name for name in archive.namelist() if not name.endswith("/")}


def _package_listing(names: Set[str]) -> Tuple[Set[str], Set[str]]:
    modules = set()
    data = set()
    for name in names:
        if not name.startswith("diffsync/") or "__pycache__" in name:
            continue
        if name.endswith(".py"):
            modules.add(name)
        elif not name.endswith(".pyc"):
            data.add(name)
    return modules, data


def _source_listing(project_root: Path) -> Tuple[Set[str], Set[str]]:
    modules = set()
    data = set()
    for path in sorted((project_root / "diffsync").rglob("*")):
        if path.is_dir() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(project_root).as_posix()
        if path.suffix == ".py":
            modules.add(relative)
        elif path.suffix != ".pyc":
            data.add(relative)
    return modules, data


def _assert_py_typed(context: CheckContext) -> None:
    wheel_path, _ = _find_artifacts(context)
    if "diffsync/py.typed" not in _wheel_members(wheel_path):
        raise ValueError(f"diffsync/py.typed is missing from {wheel_path.name}")


def assert_py_typed(context: CheckContext) -> CheckOutcome:
    """Assert that py.typed is shipped inside the wheel."""
    return _assertion_outcome("py-typed-in-wheel", _assert_py_typed, context)


def _assert_manifest(context: CheckContext) -> None:
    wheel_path, _ = _find_artifacts(context)
    wheel_modules, wheel_data = _package_listing(_wheel_members(wheel_path))
    source_modules, source_data = _source_listing(context.project_root)
    problems = []
    for label, expected, actual in (
        ("module", source_modules, wheel_modules),
        ("package-data", source_data, wheel_data),
    ):
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        if missing:
            problems.append(f"{label} files missing from wheel: {', '.join(missing)}")
        if extra:
            problems.append(f"unexpected {label} files in wheel: {', '.join(extra)}")
    if problems:
        raise ValueError("package manifest does not match diffsync/ directory: " + "; ".join(problems))


def assert_manifest(context: CheckContext) -> CheckOutcome:
    """Assert that wheel modules and package data match the diffsync/ directory."""
    return _assertion_outcome("package-manifest", _assert_manifest, context)


def _read_init_version(project_root: Path) -> str:
    source = (project_root / "diffsync" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r"^__version__\s*=\s*[\"']([^\"']+)[\"']", source, re.MULTILINE)
    if not match:
        raise ValueError("__version__ is not defined in diffsync/__init__.py")
    return match.group(1)


def _read_wheel_version(wheel_path: Path) -> str:
    with zipfile.ZipFile(wheel_path) as archive:
        metadata_name = next(
            (name for name in archive.namelist() if name.endswith(".dist-info/METADATA")),
            None,
        )
        if metadata_name is None:
            raise ValueError(f"no dist-info METADATA found in {wheel_path.name}")
        metadata = archive.read(metadata_name).decode("utf-8")
    match = re.search(r"^Version:\s*(\S+)", metadata, re.MULTILINE)
    if not match:
        raise ValueError(f"no Version field in {wheel_path.name} METADATA")
    return match.group(1)


def _read_sdist_version(sdist_path: Path) -> str:
    with tarfile.open(sdist_path) as archive:
        pkg_info = next((member for member in archive.getmembers() if member.name.endswith("PKG-INFO")), None)
        if pkg_info is None:
            raise ValueError(f"no PKG-INFO found in {sdist_path.name}")
        extracted = archive.extractfile(pkg_info)
        if extracted is None:
            raise ValueError(f"cannot read PKG-INFO from {sdist_path.name}")
        metadata = extracted.read().decode("utf-8")
    match = re.search(r"^Version:\s*(\S+)", metadata, re.MULTILINE)
    if not match:
        raise ValueError(f"no Version field in {sdist_path.name} PKG-INFO")
    return match.group(1)


def _assert_versions(context: CheckContext) -> None:
    wheel_path, sdist_path = _find_artifacts(context)
    versions = {
        "diffsync/__init__.py": _read_init_version(context.project_root),
        wheel_path.name: _read_wheel_version(wheel_path),
        sdist_path.name: _read_sdist_version(sdist_path),
    }
    if len(set(versions.values())) != 1:
        details = ", ".join(f"{where}={version}" for where, version in sorted(versions.items()))
        raise ValueError(f"version mismatch across artifacts: {details}")


def assert_versions(context: CheckContext) -> CheckOutcome:
    """Assert that sdist, wheel and __init__.py all carry the same version."""
    return _assertion_outcome("version-consistency", _assert_versions, context)


def _towncrier_types(pyproject: Dict[str, Any]) -> List[str]:
    tool = pyproject.get("tool", {})
    types = tool.get("towncrier", {}).get("type", [])
    return [entry["directory"] for entry in types]


def _assert_towncrier(context: CheckContext) -> None:
    changes_dir = context.project_root / "changes"
    types = _towncrier_types(context.pyproject)
    if not types:
        raise ValueError("no towncrier fragment types configured in pyproject.toml")
    pattern = re.compile(r"\+?[^.]+(?:\.[^.]+)*\.(?:" + "|".join(re.escape(entry) for entry in types) + r")")
    invalid = []
    if changes_dir.is_dir():
        for path in sorted(changes_dir.iterdir()):
            if path.is_dir() or path.name == ".gitignore":
                continue
            if not pattern.fullmatch(path.name):
                invalid.append(path.name)
    if invalid:
        raise ValueError(
            "invalid towncrier fragment names in changes/: "
            + ", ".join(invalid)
            + " (expected <name>.<"
            + "|".join(types)
            + ">)"
        )


def assert_towncrier(context: CheckContext) -> CheckOutcome:
    """Assert that every changes/ fragment follows towncrier naming rules."""
    return _assertion_outcome("towncrier-fragments", _assert_towncrier, context)
