#!/usr/bin/env python3
"""Retired legacy calibration-ingress CLI; canonical ingress is mandatory."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.database import DEFAULT_TARGET_DB


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    del args
    raise ContractError("calibration_legacy_ingress_retired")


if __name__ == "__main__":
    raise SystemExit(main())
