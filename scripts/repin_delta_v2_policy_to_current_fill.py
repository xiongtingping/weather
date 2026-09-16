#!/usr/bin/env python3
"""Re-pin every live delta-v2 policy to the fill batch production resolves.

Every candidate that reached the execution compile after 2026-08-22 died on
``delta_v2_fill_distribution_policy_binding_mismatch``, and no data existed
that could have satisfied the check.  The policies pin the placeholder fill
generation (one sha per scope, minted 07-29); the 08-22 refit replaced it
with measured per-cell distributions -- 151 hashes per scope -- and the
resolver picks that newer batch by valid_from while the binding check
demanded equality with a single pinned sha.  The fitting script's own
re-mint step updated the execution grids but never touched the pin.

This mints the successor generation of each live policy activation --
artifact, promotion and activation together, generation + 1, the same
append-only path the fitting script uses -- with the pin set to the
CURRENT fill batch's fingerprint (``content_sha256`` over the batch's
sorted distribution hashes, the identity the resolver now accepts
alongside the legacy exact form).  The batch is resolved per scope by the
resolver's own preference: newest valid_from among rows active at the
supplied evaluation instant.

A generation is also minted when the newest one ends inside sixty days
(2026-09-17).  The pin check alone never extended a window: a pin that stayed
current let its thirty days run out, and the activations minted 08-28 and
08-31 would have ended 09-27 and 09-30 with every candidate refused.
Validity is now a year; the rules live in
``bayes_basket_target.delta_v2_authority_clock``.

Read-only unless ``--commit``.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.delta_v2_authority_clock import (  # noqa: E402
    RENEW_WITHIN_DAYS,
    repin_policies,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--commit", action="store_true",
        help="mint the re-pinned generation (default: report only)",
    )
    parser.add_argument(
        "--renew-within-days", type=float, default=RENEW_WITHIN_DAYS,
        help="also mint a generation whose window ends inside this many days",
    )
    args = parser.parse_args()
    database = args.db.expanduser().resolve(strict=True)
    if args.commit:
        conn = sqlite3.connect(str(database), timeout=60.0)
    else:
        conn = sqlite3.connect(
            f"file:{database}?mode=ro", uri=True, timeout=60.0
        )
    conn.execute("PRAGMA busy_timeout=60000")
    receipt = repin_policies(
        conn,
        now=datetime.now(timezone.utc),
        commit=args.commit,
        renew_within_days=args.renew_within_days,
    )
    print(
        f"\n{'minted' if args.commit else 'would mint'}={receipt['minted']}"
        f" (repinned={receipt['repinned']} renewed={receipt['renewed']})"
        f" skipped={receipt['skipped']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
