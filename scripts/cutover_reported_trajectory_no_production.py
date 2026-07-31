#!/usr/bin/env python3
"""Retired production_no_live switch entrypoint.

Service ownership and switching are now provided solely by ``targetctl.sh``.
Keeping this file as an explicit no-op refusal makes accidental use fail closed
without leaving a second control implementation behind.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "retired: use scripts/targetctl.sh for the gui/<current uid> "
        "production_no_live service switch",
        file=sys.stderr,
    )
    return 78


if __name__ == "__main__":
    raise SystemExit(main())
