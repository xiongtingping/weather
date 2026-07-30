#!/usr/bin/env python3
"""Probe M5 writer quiescence without stopping services or touching SQLite."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.migration_quiescence import (  # noqa: E402
    SCHEMA,
    build_writer_quiescence_receipt,
    isolated_target_writer_quiescence_receipt,
)
from bayes_basket_target.database import (  # noqa: E402
    DEFAULT_TARGET_DB,
    is_isolated_target_database,
    resolve_target_database,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--output", type=Path, default=ROOT / "output/audits/bayes_basket_target_writer_quiescence.json")
    args = parser.parse_args()
    try:
        db_path = resolve_target_database(args.db)
        receipt = (
            isolated_target_writer_quiescence_receipt(db_path)
            if is_isolated_target_database(db_path)
            else build_writer_quiescence_receipt(db_path=db_path)
        )
    except Exception as exc:
        print(json.dumps({
            "schema": SCHEMA,
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "quiescent": False,
        }, ensure_ascii=False, sort_keys=True))
        return 2
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "schema": receipt["schema"],
        "quiescent": receipt["quiescent"],
        "writer_process_hits": receipt["writer_process_hits"],
        "output": str(output),
    }, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["quiescent"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
