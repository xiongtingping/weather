"""Recompute the fleet regime daily residual series for every served stage.

The layer that reads this series does so on the serving path, where it must
cost one indexed row read.  Producing the series costs a scan of the bridge
history -- 3s for P0-M, 192s for P1, which re-decides all day -- so it is
produced here, on the resident's sweep clock, and never on a decision.

A pass with nothing to do is nearly free: the days whose truth has not been
revised since they were last recorded are decided from the truth table alone,
and if none have moved the expensive scan is skipped entirely.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet_regime_shift import (  # noqa: E402
    FLEET_REGIME_STAGES,
    ensure_fleet_regime_schema,
    refresh_fleet_regime_daily,
)

DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def main() -> int:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    conn = sqlite3.connect(str(DB), timeout=60.0)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        ensure_fleet_regime_schema(conn)
        conn.commit()
        for stage in FLEET_REGIME_STAGES:
            try:
                result = refresh_fleet_regime_daily(
                    conn, stage=stage, now_utc=now
                )
            except Exception as exc:  # noqa: BLE001
                # One stage that cannot be read must not take the others with
                # it: each stage is its own book and its own evidence.
                print(f"  {stage}: FAILED {type(exc).__name__}: {exc}")
                continue
            print(
                f"  {stage}: {result.get('state')}"
                f" days_written={result.get('days_written')}"
                f" {','.join(result.get('event_dates') or []) or '-'}"
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
