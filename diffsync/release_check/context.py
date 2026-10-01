"""Shared context handed to every stage and sub-check."""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import toml as tomllib  # type: ignore[no-redef]


def load_pyproject(root: Path) -> Dict[str, Any]:
    """Parse ``pyproject.toml``; it is the single source of truth for packaging."""
    with open(root / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)  # type: ignore[attr-defined]


@dataclass
class CheckContext:
    """Everything a stage needs: paths, pyproject data and the run logger."""

    root: Path
    dist_dir: Path
    pyproject: Dict[str, Any]
    log: Callable[[str], None]

    @classmethod
    def build(cls, root: Path, dist_dir: Path, log: Callable[[str], None]) -> "CheckContext":
        """Create a context rooted at the repository checkout."""
        return cls(root=root, dist_dir=dist_dir, pyproject=load_pyproject(root), log=log)
