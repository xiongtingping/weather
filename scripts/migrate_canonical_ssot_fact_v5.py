#!/usr/bin/env python3
"""Preview or explicitly apply the canonical fact-store V5 migration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.canonical_ssot_v5_migration import (  # noqa: E402
    CONFIRMATION,
    migrate_canonical_ssot_fact_v5,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    parser.add_argument("--writer-quiescence-receipt", type=Path)
    parser.add_argument("--apply-production-no-live-v5", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args()

    apply = bool(args.apply_production_no_live_v5)
    if apply and args.writer_quiescence_receipt is None:
        parser.error(
            "--apply-production-no-live-v5 requires --writer-quiescence-receipt"
        )
    if apply and args.confirm != CONFIRMATION:
        parser.error(f"--confirm must equal {CONFIRMATION!r}")
    if not apply and (
        args.writer_quiescence_receipt is not None or args.confirm is not None
    ):
        parser.error(
            "quiescence and confirmation arguments are valid only with apply"
        )

    quiescence = (
        load_bounded_json(
            args.writer_quiescence_receipt.expanduser().resolve(strict=True)
        )
        if args.writer_quiescence_receipt is not None
        else None
    )
    receipt = migrate_canonical_ssot_fact_v5(
        args.database,
        apply=apply,
        confirmation=args.confirm,
        writer_quiescence_receipt=quiescence,
    )
    output = args.receipt_output.expanduser().resolve()
    write_bounded_json(output, receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
