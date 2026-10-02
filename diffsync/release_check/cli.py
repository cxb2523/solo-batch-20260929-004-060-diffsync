"""Command line entry point for the release check pipeline."""

import argparse
from pathlib import Path
from typing import List, Optional

from diffsync.release_check.runner import ReleaseCheckRunner


def main(argv: Optional[List[str]] = None) -> int:
    """Parse arguments and run the release check pipeline."""
    parser = argparse.ArgumentParser(
        prog="diffsync.release_check",
        description="Run the diffsync release check pipeline (lock, lint/type, tests, build, artifact assertions).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an aborted run; stages that already passed are not re-executed.",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Project root containing pyproject.toml (default: the repository this package lives in).",
    )
    args = parser.parse_args(argv)
    runner = ReleaseCheckRunner(project_root=args.project_root, resume=args.resume)
    return runner.run()
