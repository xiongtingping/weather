#!/usr/bin/env python3
"""Does physical state separate "the day still rose" at a fixed hours-left?

The ceiling's rate is fitted on one covariate: hours until local settlement.
This asks whether the airport's own state at that moment splits the same
hours-left band into groups with different outcomes -- which is exactly the
signal a state covariate would buy, and nothing if the bands are flat.

The bucket rule comes from production (``_anchor_bucket_for_temperature`` plus
the bridge row's own intervals).  Momentum and drawdown are computed here as
plain descriptive statistics of the observation sequence, matching the
definitions in metar_path_state.py (last segment slope; running high minus
current temperature).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.materialization import _anchor_bucket_for_temperature

SSOT = f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro"
ARCHIVE = f"file:{ROOT}/data/archive.db?mode=ro"

# Anchors to sweep: hours before local settlement midnight.  The ceiling is
# only allowed to speak inside ten hours, but the sweep runs past that so the
# boundary itself is measured rather than assumed.
ANCHORS = (3.0, 5.0, 7.0, 9.0, 11.0, 13.0, 15.0)


def _utc(stamp: str) -> datetime:
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))


def main() -> None:
    conn = sqlite3.connect(SSOT, uri=True)
    conn.row_factory = sqlite3.Row
    archive = sqlite3.connect(ARCHIVE, uri=True)
    archive.row_factory = sqlite3.Row

    truth = {}
    for row in conn.execute(
        "select station_id, local_date, official_tmax_c"
        " from calibration_station_daily_high_truth order by revision_no"
    ):
        truth[(row["station_id"], row["local_date"])] = row["official_tmax_c"]

    geometry = {}
    for row in conn.execute(
        "select station_id, event_date, bucket_intervals_json, envelope_json,"
        " settlement_timezone"
        " from basket_probability_bridge_history where state='COMMITTED'"
        " order by decision_at_utc"
    ):
        geometry[(row["station_id"], row["event_date"])] = (
            row["bucket_intervals_json"],
            json.loads(row["envelope_json"])["bucket_ordinals"],
            row["settlement_timezone"],
        )

    def collect(hours_left: float) -> list[dict]:
      samples = []
      for (station, day), settled in sorted(truth.items()):
        if (station, day) not in geometry:
            continue
        intervals_json, ordinals, tz_name = geometry[(station, day)]
        try:
            zone = ZoneInfo(tz_name)
        except Exception:
            continue
        close = datetime.fromisoformat(day).replace(tzinfo=zone) + timedelta(
            days=1
        )
        target = close - timedelta(hours=hours_left)
        rows = archive.execute(
            "select valid_at, reported_temperature_c from"
            " reported_trajectory_observations"
            " where station_id=? and source_lane='awc'"
            " and julianday(valid_at) >= julianday(?)"
            " and julianday(valid_at) <= julianday(?)"
            " order by valid_at",
            (
                station,
                (close - timedelta(days=1)).astimezone(
                    ZoneInfo("UTC")
                ).isoformat(),
                target.astimezone(ZoneInfo("UTC")).isoformat(),
            ),
        ).fetchall()
        if len(rows) < 3:
            continue
        temps = [float(r["reported_temperature_c"]) for r in rows]
        times = [_utc(r["valid_at"]) for r in rows]
        gap = (times[-1] - times[-2]).total_seconds() / 3600.0
        if gap <= 0:
            continue
        momentum = (temps[-1] - temps[-2]) / gap
        running_high = max(temps)
        drawdown = running_high - temps[-1]
        truth_bucket = _anchor_bucket_for_temperature(
            settled,
            root_lineage={"bucket_intervals_c_json": intervals_json},
            bucket_ordinals=ordinals,
        )
        high_bucket = _anchor_bucket_for_temperature(
            running_high,
            root_lineage={"bucket_intervals_c_json": intervals_json},
            bucket_ordinals=ordinals,
        )
        if truth_bucket is None or high_bucket is None:
            continue
        samples.append({
            "station": station,
            "day": day,
            "momentum": momentum,
            "drawdown": drawdown,
            "running_high": running_high,
            "settled": settled,
            "still_rose": int(
                ordinals[truth_bucket] > ordinals[high_bucket]
            ),
        })
      return samples

    def rate(subset) -> float:
        return (
            sum(s["still_rose"] for s in subset) / len(subset)
            if subset else float("nan")
        )

    print("每行一个锚点。基准率=当前天花板在该档给出的单一数字；")
    print("后面几列=按物理状态分组后各组的真实概率。分组把基准劈开多少，")
    print("就是状态协变量能买到的信号。\n")
    header = (
        f"{'距日终':>6s} {'样本':>5s} {'基准率':>7s} | "
        f"{'仍在最高点':>10s} {'回落>4°C':>9s} | "
        f"{'急升>1°C/h':>11s} {'在降<-0.5':>10s} | {'劈开幅度':>8s}"
    )
    print(header)
    print("-" * len(header))
    best = None
    for hours in ANCHORS:
        samples = collect(hours)
        if len(samples) < 50:
            continue
        base = rate(samples)
        at_high = [s for s in samples if s["drawdown"] <= 0.01]
        fallen = [s for s in samples if s["drawdown"] >= 4.0]
        rising = [s for s in samples if s["momentum"] >= 1.0]
        falling = [s for s in samples if s["momentum"] <= -0.5]
        spread = max(
            abs(rate(group) - base)
            for group in (at_high, fallen, rising, falling)
            if group
        )
        print(
            f"{hours:5.0f}h {len(samples):5d} {base:7.3f} | "
            f"{rate(at_high):10.3f} {rate(fallen):9.3f} | "
            f"{rate(rising):11.3f} {rate(falling):10.3f} | {spread:8.3f}"
        )
        if best is None or spread > best[1]:
            best = (hours, spread, samples, base, at_high, rising)
    if best is None:
        print("无足够样本")
        return
    hours, spread, samples, base, at_high, rising = best
    print()
    print(f"劈得最开的锚点是距日终 {hours:.0f} 小时:"
          f" 基准 {base:.3f},仍在最高点组 {rate(at_high):.3f}"
          f"({len(at_high)} 例),急升组 {rate(rising):.3f}({len(rising)} 例)")
    print()
    print("该锚点真实站日举例(状态显示还会升、且当天确实升破了桶):")
    shown = 0
    for s in sorted(
        [x for x in samples if x["still_rose"] and x["momentum"] > 0.2],
        key=lambda x: -(x["settled"] - x["running_high"]),
    ):
        print(f"  {s['station']} {s['day']}: 该时刻已观测最高"
              f" {s['running_high']:4.1f}°C、动量 {s['momentum']:+.1f}°C/h"
              f" → 当日结算 {s['settled']:4.1f}°C")
        shown += 1
        if shown >= 5:
            break


if __name__ == "__main__":
    main()
