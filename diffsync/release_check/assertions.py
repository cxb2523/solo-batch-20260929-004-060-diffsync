"""Artifact assertions run after ``poetry build`` has produced dist/.

These checks are a pre-release backstop only. py.typed and package data
files are declared exclusively via ``include``/``packages`` in
``pyproject.toml``; wherever this module and ``pyproject.toml`` disagree,
``pyproject.toml`` wins.
"""

import ast
import re
import tarfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from diffsync.release_check.context import CheckContext
from diffsync.release_check.results import CheckResult, Status

PACKAGE_NAME = "diffsync"
IGNORED_FRAGMENT_FILES = frozenset({".gitignore", ".gitkeep"})


def _pass(name: str, duration: float = 0.0) -> CheckResult:
    return CheckResult(name=name, status=Status.PASSED, exit_code=0, duration=duration)


def _fail(name: str, reason: str) -> CheckResult:
    return CheckResult(name=name, status=Status.FAILED, exit_code=1, reason=reason)


def find_artifacts(dist_dir: Path) -> Tuple[Optional[Path], Optional[Path]]:
    """Return the newest (wheel, sdist) in the dist directory, if present."""
    wheels = sorted(dist_dir.glob("*.whl"), key=lambda path: path.stat().st_mtime)
    sdists = sorted(dist_dir.glob("*.tar.gz"), key=lambda path: path.stat().st_mtime)
    wheel = wheels[-1] if wheels else None
    sdist = sdists[-1] if sdists else None
    return wheel, sdist


def read_init_version(init_file: Path) -> Optional[str]:
    """Extract ``__version__`` from ``diffsync/__init__.py`` without importing it."""
    tree = ast.parse(init_file.read_text(encoding="utf-8"), filename=str(init_file))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__version__":
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        return node.value.value
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "__version__":
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    return None


def _read_metadata_version(text: str) -> Optional[str]:
    match = re.search(r"^Version:\s*(\S+)\s*$", text, flags=re.MULTILINE)
    return match.group(1) if match else None


def wheel_members(wheel_path: Path) -> Set[str]:
    """Return the member names of a wheel archive."""
    with zipfile.ZipFile(wheel_path) as archive:
        return set(archive.namelist())


def _wheel_metadata_version(wheel_path: Path) -> Optional[str]:
    with zipfile.ZipFile(wheel_path) as archive:
        for name in archive.namelist():
            if name.endswith(".dist-info/METADATA"):
                return _read_metadata_version(archive.read(name).decode("utf-8"))
    return None


def _sdist_metadata_version(sdist_path: Path) -> Optional[str]:
    with tarfile.open(sdist_path) as archive:
        for member in archive.getmembers():
            if member.name.endswith("PKG-INFO"):
                extracted = archive.extractfile(member)
                if extracted is not None:
                    return _read_metadata_version(extracted.read().decode("utf-8"))
    return None


def source_package_files(root: Path) -> Tuple[Set[str], Set[str]]:
    """Return (module files, data files) of the package, relative to the repo root."""
    package_dir = root / PACKAGE_NAME
    modules = {
        path.relative_to(root).as_posix() for path in package_dir.rglob("*.py") if "__pycache__" not in path.parts
    }
    data = {
        path.relative_to(root).as_posix()
        for path in package_dir.rglob("*")
        if path.is_file() and path.suffix != ".py" and "__pycache__" not in path.parts
    }
    return modules, data


def declared_package_data(pyproject: Dict[str, Any]) -> Set[str]:
    """Return data files declared via ``include``/``packages`` in pyproject.toml.

    pyproject.toml is the single source of truth for what ships; the release
    check only verifies that the declaration was honored by the build.
    """
    poetry = pyproject.get("tool", {}).get("poetry", {})
    declared: Set[str] = set()
    include = poetry.get("include", [])
    for entry in include:
        path = entry.get("path") if isinstance(entry, dict) else entry
        if isinstance(path, str) and path.startswith(f"{PACKAGE_NAME}/"):
            declared.add(path)
    return declared


def check_py_typed_in_wheel(context: CheckContext) -> CheckResult:
    """Assert that py.typed (and any declared package data) made it into the wheel."""
    name = "py-typed-in-wheel"
    wheel, _ = find_artifacts(context.dist_dir)
    if wheel is None:
        return _fail(name, f"no wheel found in {context.dist_dir}")
    members = wheel_members(wheel)
    _, source_data = source_package_files(context.root)
    expected = source_data | declared_package_data(context.pyproject)
    if (context.root / PACKAGE_NAME / "py.typed").exists():
        expected.add(f"{PACKAGE_NAME}/py.typed")
    missing = sorted(path for path in expected if path not in members)
    if missing:
        return _fail(name, f"data files missing from {wheel.name}: {', '.join(missing)}")
    return _pass(name)


def check_package_manifest(context: CheckContext) -> CheckResult:
    """Assert the wheel's module/data manifest matches the diffsync/ directory."""
    name = "package-manifest"
    wheel, _ = find_artifacts(context.dist_dir)
    if wheel is None:
        return _fail(name, f"no wheel found in {context.dist_dir}")
    members = {member for member in wheel_members(wheel) if member.startswith(f"{PACKAGE_NAME}/")}
    expected_modules, expected_data = source_package_files(context.root)
    expected = expected_modules | expected_data | declared_package_data(context.pyproject)
    missing = sorted(expected - members)
    unexpected = sorted(member for member in members - expected if member.endswith(".py"))
    problems = []
    if missing:
        problems.append(f"missing from wheel: {', '.join(missing)}")
    if unexpected:
        problems.append(f"not present in {PACKAGE_NAME}/: {', '.join(unexpected)}")
    if problems:
        return _fail(name, "; ".join(problems))
    return _pass(name)


def check_version_consistency(context: CheckContext) -> CheckResult:
    """Assert the sdist, wheel and ``diffsync/__init__.py`` versions all match."""
    name = "version-consistency"
    wheel, sdist = find_artifacts(context.dist_dir)
    if wheel is None or sdist is None:
        return _fail(name, f"need both a wheel and an sdist in {context.dist_dir}")
    versions = {
        "sdist": _sdist_metadata_version(sdist),
        "wheel": _wheel_metadata_version(wheel),
        "__init__.py": read_init_version(context.root / PACKAGE_NAME / "__init__.py"),
    }
    missing = [source for source, version in versions.items() if version is None]
    if missing:
        return _fail(name, f"could not determine version from: {', '.join(missing)}")
    distinct = {version for version in versions.values() if version is not None}
    if len(distinct) != 1:
        details = ", ".join(f"{source}={version}" for source, version in sorted(versions.items()))
        return _fail(name, f"version mismatch: {details}")
    return _pass(name)


def towncrier_fragment_types(pyproject: Dict[str, Any]) -> Set[str]:
    """Return the valid towncrier fragment types declared in pyproject.toml."""
    types = pyproject.get("tool", {}).get("towncrier", {}).get("type", [])
    return {entry["directory"] for entry in types if isinstance(entry, dict) and "directory" in entry}


def is_valid_fragment_name(filename: str, valid_types: Set[str]) -> bool:
    """Check a ``changes/`` entry against towncrier's ``<name>.<type>`` convention."""
    if filename in IGNORED_FRAGMENT_FILES:
        return True
    stem, dot, fragment_type = filename.rpartition(".")
    if not dot or fragment_type not in valid_types:
        return False
    if stem.startswith("+"):
        stem = stem[1:]
    return bool(stem) and all(stem.split("."))


def check_towncrier_fragments(context: CheckContext) -> CheckResult:
    """Assert every fragment in ``changes/`` follows towncrier naming rules."""
    name = "towncrier-fragments"
    changes_dir = context.root / context.pyproject.get("tool", {}).get("towncrier", {}).get("directory", "changes")
    valid_types = towncrier_fragment_types(context.pyproject)
    if not changes_dir.is_dir():
        return _pass(name)
    invalid = sorted(
        entry.name
        for entry in changes_dir.iterdir()
        if entry.is_file() and not is_valid_fragment_name(entry.name, valid_types)
    )
    if invalid:
        return _fail(
            name,
            f"invalid towncrier fragment names in {changes_dir.name}/: {', '.join(invalid)} "
            f"(expected <name>.<{'|'.join(sorted(valid_types))}>)",
        )
    return _pass(name)


def artifact_assertions() -> List[Any]:
    """Return the assertion checks run, concurrently, after the build stage."""
    return [
        check_py_typed_in_wheel,
        check_package_manifest,
        check_version_consistency,
        check_towncrier_fragments,
    ]
