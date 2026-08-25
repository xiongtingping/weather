"""Derive each station's season anchor from the ERA5 same-day climatology.

Charter 62 (2026-08-25): seasons are station-local, anchored on each
station's own hottest months, not on calendar quarters.  This script is the
reproducible derivation behind ``bayes_basket_target/station_season.py`` --
it reads the frozen climatology capture and prints the anchor table as
Python source.  Re-run it only after re-capturing the climatology; the map
is a static artifact, not a silently refreshing one.

Anchor rule: hottest month by the 3-month centered running mean of the
9-year monthly means (stabler than the raw argmax -- 16 of 45 stations have
a July/August argmax split inside year-to-year noise).  Stations whose
annual amplitude (max month mean - min month mean) is below 2.0C carry no
usable phase; they get anchor ``None`` and serve the single ALLYEAR bin.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "bayes_basket_target_ssot.db"
AMPLITUDE_FLOOR_C = 2.0


def main() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT station_id, substr(local_date, 6, 2), AVG(daily_high_c) "
        "FROM calibration_station_same_day_climatology "
        "GROUP BY station_id, substr(local_date, 6, 2)"
    ).fetchall()
    monthly: dict[str, dict[int, float]] = defaultdict(dict)
    for station_id, month, mean_high in rows:
        monthly[str(station_id)][int(month)] = float(mean_high)
    entries = []
    for station_id in sorted(monthly):
        means = monthly[station_id]
        if sorted(means) != list(range(1, 13)):
            raise SystemExit(f"{station_id}: incomplete monthly coverage")
        amplitude = max(means.values()) - min(means.values())
        smooth = {
            month: (
                means[(month - 2) % 12 + 1]
                + means[month]
                + means[month % 12 + 1]
            ) / 3.0
            for month in range(1, 13)
        }
        anchor = max(smooth, key=lambda month: (smooth[month], -month))
        if amplitude < AMPLITUDE_FLOOR_C:
            entries.append((station_id, None, amplitude))
        else:
            entries.append((station_id, anchor, amplitude))
    print("_SEASON_ANCHOR_MONTH: dict[str, int | None] = {")
    for station_id, anchor, amplitude in entries:
        anchor_repr = "None" if anchor is None else str(anchor)
        print(
            f'    "{station_id}": {anchor_repr},'
            f"  # amplitude {amplitude:.1f}C"
        )
    print("}")


if __name__ == "__main__":
    main()
