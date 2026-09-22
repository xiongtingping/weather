#!/usr/bin/env python3
"""Supply watch pass (charter 197): egress probe + served-P0 freshness, one JSON out.

Reads the SSOT ``mode=ro`` (primary-key and covering-index reads only), the
forward market registry and the resident's status file; sends one HEAD per
upstream through the explicit production proxy; writes
``data/runtime/supply_watch.json``.  No database write, no order, no push --
the ``notify`` block is there for whatever channel the operator authorises.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import supply_watch  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-probe", action="store_true",
                        help="skip the egress HEAD probes (supply check only)")
    parser.add_argument("--output", type=Path, default=supply_watch.DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        payload = supply_watch.run(
            output_path=args.output,
            probe=None if args.no_probe else supply_watch.http_head_probe,
        )
    except Exception as exc:  # noqa: BLE001 -- a failed pass only means "ask again"
        print(f"断供守望 失败：{type(exc).__name__}: {str(exc)[:200]}")
        return 1
    print(supply_watch.summary_line(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
