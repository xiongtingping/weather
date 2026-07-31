#!/usr/bin/env python3
"""Retired live cutover; target production is permanently no-live."""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "retired: live basket cutover is forbidden; use "
        "scripts/targetctl.sh for user-domain production_no_live control",
        file=sys.stderr,
    )
    return 78


if __name__ == "__main__":
    raise SystemExit(main())
