"""Allow running the pipeline with python -m diffsync.release_check."""

import sys

from diffsync.release_check.cli import main

if __name__ == "__main__":
    sys.exit(main())
