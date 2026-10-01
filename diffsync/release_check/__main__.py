"""Command line entry point: ``python -m diffsync.release_check``."""

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from diffsync.release_check.machine import ReleaseCheckMachine


def main(argv: Optional[List[str]] = None) -> int:
    """Parse arguments and run the release check state machine."""
    parser = argparse.ArgumentParser(
        prog="release-check",
        description="Run the diffsync release check state machine "
        "(lock file, ruff/mypy, pytest, poetry build, artifact assertions).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an aborted run from the last checkpoint; stages that already passed are not re-executed.",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="Directory for release-report.json (default: build/, or build/<run-id>/ on CI).",
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=None,
        help="Directory containing the built artifacts (default: dist/).",
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    machine = ReleaseCheckMachine(root=root, report_dir=args.report_dir, dist_dir=args.dist_dir)
    return machine.run(resume=args.resume)


if __name__ == "__main__":
    sys.exit(main())
