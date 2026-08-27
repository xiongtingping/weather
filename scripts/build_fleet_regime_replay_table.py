"""Build the per-event-date fleet regime shift table the ledger replays.

Same discipline as ``center_width_replay_table.json`` beside it: an entry for
event day D is derived only from days whose truth was already visible before
D began, so a replay cannot buy information the decision never had.

Conservative on purpose.  Production reads the series as of the decision
timestamp, and a P0 decision taken during D would see any truth that landed
that morning; this table cuts at D's UTC midnight and so sometimes withholds
a day production would have had.  Understating the layer is the safe
direction for an acceptance test.

The shift itself is not computed here -- ``derive_offset`` is production's
own and is called directly, so the table cannot drift from what serving
does.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet_regime_shift import (  # noqa: E402
    FLEET_REGIME_SERVED_STAGES,
    FLEET_REGIME_STAGES,
    derive_offset,
)

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
OUT = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "fleet_regime_replay_table.json"
)


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    ).astimezone(timezone.utc)


def main() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60.0)
    table: dict[str, dict[str, dict[str, float]]] = {}
    try:
        for stage in FLEET_REGIME_STAGES:
            rows = [
                {
                    "event_date": str(row[0]),
                    "residual_c_mean": float(row[1]),
                    "station_count": int(row[2]),
                    "cell_count": int(row[3]),
                    "truth_visible_at_utc": str(row[4]),
                }
                for row in conn.execute(
                    "SELECT event_date, residual_c_mean, station_count,"
                    " cell_count, truth_visible_at_utc"
                    " FROM calibration_fleet_regime_daily"
                    " WHERE stage=? ORDER BY event_date",
                    (stage,),
                )
            ]
            per_day: dict[str, dict[str, float]] = {}
            for row in rows:
                day = row["event_date"]
                cutoff = _utc(f"{day}T00:00:00Z")
                visible = [
                    other for other in rows
                    if _utc(other["truth_visible_at_utc"]) < cutoff
                ]
                receipt = derive_offset(visible, event_date=day)
                shift = float(receipt["regime_delta_c"])
                # A stage nothing serves contributes an explicit zero rather
                # than being absent, so the replay records that it was asked
                # and declined rather than that it was never wired.
                if stage not in FLEET_REGIME_SERVED_STAGES:
                    shift = 0.0
                per_day[day] = {
                    "regime_delta_c": shift,
                    "tier": receipt["tier"],
                    "run_length": receipt["run_length"],
                    "signal_c": receipt["signal_c"],
                    "signal_event_date": receipt["signal_event_date"],
                    "state": receipt["state"],
                    "served_stage": stage in FLEET_REGIME_SERVED_STAGES,
                }
            table[stage] = per_day
    finally:
        conn.close()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "schema": "fleet_regime_replay_table_v1",
                "visibility_rule": "truth_visible_before_event_day_utc_start",
                "served_stages": list(FLEET_REGIME_SERVED_STAGES),
                "stages": table,
            },
            ensure_ascii=False, indent=1, sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    for stage, per_day in sorted(table.items()):
        nonzero = [
            day for day, entry in per_day.items()
            if entry["regime_delta_c"] != 0.0
        ]
        print(
            f"{stage}: {len(per_day)} days,"
            f" {len(nonzero)} with a nonzero shift"
        )
        for day in sorted(per_day):
            entry = per_day[day]
            print(
                f"  {day} shift={entry['regime_delta_c']:+.3f}C"
                f" tier={entry['tier']} run={entry['run_length']}"
                f" signal_day={entry['signal_event_date']}"
            )
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
