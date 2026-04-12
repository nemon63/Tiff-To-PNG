from __future__ import annotations

import sys
from collections.abc import Sequence

from image_converter.cli import run_cli


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        return run_cli(args)

    from image_converter.application.bootstrap import run_gui

    return run_gui()
