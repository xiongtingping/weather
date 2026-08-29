#!/usr/bin/env python3
"""What the raw METAR carries that the temperature path never reads.

The path state parses temp/dewpoint, wind, cloud amount, precipitation
presence and pressure from the body.  The remark section carries more:
the T-group (temperature at 0.1°C where the body rounds to whole degrees)
and the synoptic 1-group (the max temperature of the last six hours, which
sees every spike between hourly reports).  Both bear directly on the layer's
core quantity -- the running high -- so their availability and their value
are measured here against settled truth.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SSOT = f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro"
ARCHIVE = f"file:{ROOT}/data/archive.db?mode=ro"
SINCE = "2026-08-10"

T_GROUP = re.compile(r"(?<![A-Z0-9])T([01])(\d{3})([01])(\d{3})(?![0-9])")
MAX6_GROUP = re.compile(r"(?<![A-Z0-9])1([01])(\d{3})(?![0-9])")


def _signed(sign: str, digits: str) -> float:
    value = int(digits) / 10.0
    return -value if sign == "1" else value


def main() -> None:
    conn = sqlite3.connect(SSOT, uri=True, timeout=120)
    conn.row_factory = sqlite3.Row
    archive = sqlite3.connect(ARCHIVE, uri=True, timeout=120)
    archive.row_factory = sqlite3.Row

    truth, timezones = {}, {}
    for row in conn.execute(
        "select station_id, local_date, official_tmax_c"
        " from calibration_station_daily_high_truth order by revision_no"
    ):
        truth[(row["station_id"], row["local_date"])] = row["official_tmax_c"]
    for row in conn.execute(
        "select distinct station_id, event_date, settlement_timezone"
        " from basket_probability_bridge_history where state='COMMITTED'"
    ):
        timezones[(row["station_id"], row["event_date"])] = (
            row["settlement_timezone"]
        )

    keys = sorted(
        k for k in truth if k in timezones and k[1] >= SINCE
    )
    report_count = 0
    with_t = 0
    with_max6 = 0
    stations_t = set()
    stations_max6 = set()
    day_rows = []
    for station, day in keys:
        try:
            tz = ZoneInfo(timezones[(station, day)])
        except Exception:
            continue
        start = datetime.fromisoformat(day).replace(tzinfo=tz)
        end = start + timedelta(days=1)
        rows = archive.execute(
            "select valid_at, reported_temperature_c, raw_text"
            " from reported_trajectory_observations"
            " where station_id=? and source_lane='awc'"
            " and julianday(valid_at)>=julianday(?)"
            " and julianday(valid_at)<julianday(?)",
            (station, start.isoformat(), end.isoformat()),
        ).fetchall()
        if not rows:
            continue
        integer_high = max(float(r["reported_temperature_c"]) for r in rows)
        t_high = None
        max6_high = None
        for r in rows:
            raw = str(r["raw_text"] or "")
            report_count += 1
            rmk = raw.split(" RMK ", 1)
            tail = rmk[1] if len(rmk) > 1 else ""
            m = T_GROUP.search(tail)
            if m:
                with_t += 1
                stations_t.add(station)
                value = _signed(m.group(1), m.group(2))
                t_high = value if t_high is None else max(t_high, value)
            m6 = MAX6_GROUP.search(tail)
            if m6:
                with_max6 += 1
                stations_max6.add(station)
                value = _signed(m6.group(1), m6.group(2))
                max6_high = (
                    value if max6_high is None else max(max6_high, value)
                )
        day_rows.append({
            "station": station, "day": day,
            "truth": truth[(station, day)],
            "integer_high": integer_high,
            "t_high": t_high,
            "max6_high": max6_high,
        })

    print(f"站日 {len(day_rows)}  报文 {report_count}")
    print(f"带 T 组(0.1°C 温度) 的报文: {with_t}"
          f" ({100.0 * with_t / max(report_count, 1):.0f}%),"
          f" 覆盖 {len(stations_t)} 站")
    print(f"带 1 组(6小时最高温) 的报文: {with_max6}"
          f" ({100.0 * with_max6 / max(report_count, 1):.0f}%),"
          f" 覆盖 {len(stations_max6)} 站")
    print()

    have_t = [r for r in day_rows if r["t_high"] is not None]
    if have_t:
        sharper = sum(
            1 for r in have_t if r["t_high"] > r["integer_high"] + 1e-9
        )
        closer = sum(
            1 for r in have_t
            if abs(r["t_high"] - r["truth"])
            < abs(r["integer_high"] - r["truth"]) - 1e-9
        )
        print(f"T 组能用的站日 {len(have_t)}:")
        print(f"  0.1°C 高温 > 整数高温(硬下界能抬高): {sharper}"
              f" ({100.0 * sharper / len(have_t):.0f}%)")
        print(f"  离结算真值更近: {closer}"
              f" ({100.0 * closer / len(have_t):.0f}%)")
    have6 = [r for r in day_rows if r["max6_high"] is not None]
    if have6:
        beats = [
            r for r in have6
            if r["max6_high"] > r["integer_high"] + 1e-9
        ]
        print(f"\n1 组(6h最高)能用的站日 {len(have6)}:")
        print(f"  6h最高 > 逐报整数高温(捕捉到报文间的尖峰): {len(beats)}"
              f" ({100.0 * len(beats) / len(have6):.0f}%)")
        if beats:
            import statistics
            gap_shrink = [
                (abs(r["integer_high"] - r["truth"])
                 - abs(r["max6_high"] - r["truth"]))
                for r in beats
            ]
            print(f"  这些站日里与真值距离的改善: 中位"
                  f" {statistics.median(gap_shrink):+.2f}°C,"
                  f" 均值 {sum(gap_shrink)/len(gap_shrink):+.2f}°C")
            worst = sorted(
                beats,
                key=lambda r: -(r["max6_high"] - r["integer_high"]),
            )[:5]
            for r in worst:
                print(f"    {r['station']:5s} {r['day']}: 逐报高"
                      f" {r['integer_high']:5.1f} → 6h组高"
                      f" {r['max6_high']:5.1f}  真值 {r['truth']:5.1f}")


if __name__ == "__main__":
    main()
