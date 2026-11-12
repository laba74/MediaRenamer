"""MediaRenamer entry point: renames photos and videos from their metadata."""

import sys
from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
    """
    Run the MediaRenamer command line interface.

    :param argv: Command line arguments, defaults to ``sys.argv[1:]`` when ``None``.
    :return: Process exit code (0 on success).
    """
    _ = sys.argv[1:] if argv is None else argv
    return 0


if __name__ == "__main__":
    sys.exit(main())
