#!/usr/bin/env python3
"""Fit the cooling-side ceiling: how much of the day is still ahead of the high.

The hard bound only ever cuts from below -- an observation says the max has
reached X and every bucket under X dies.  Nothing cuts from above, so the mass
that says "it will get hotter still" survives the sunset that ends it, and the
renormalisation after the lower cut scales that dead mass up rather than down.

What is missing is not a stronger likelihood but a different observation: the
clock.  This fits P(the day's max ends above the bucket already holding it |
hours left until local settlement), which is the one number the posterior gets
wrong, and it is fitted on whole station-days rather than on the advancing-high
folds the trajectory artifacts use -- the reason those cannot supply it is that
a fold only exists where the high advanced, and the fact to be learned here is
that it usually does not.

``trained_through`` is the last event date any sample came from; a serve-side
use on that date or earlier would be reading its own answer.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from bayes_basket_target.metar_ceiling import (  # noqa: E402
    CEILING_GAP_DIMENSION_VERSION,
    GAP_EDGES_C,
    SCHEMA,
    gap_bin,
    gap_cell_bounds,
)
# Formal report types: the running high is taken from these, so the "current
# temperature" it is compared against comes from the same series.
FORMAL_REPORT_TYPES = ("METAR", "SPECI")
# Hours left until local settlement.  Fine where the curve moves (the collapse
# runs from roughly ten hours out to zero) and coarse where it is flat.
HOURS_EDGES = (0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 13.0, 16.0,
               20.0, 24.0, 1e9)
MIN_BIN_SAMPLES = 40


def hours_bin(hours: float) -> int:
    for index in range(len(HOURS_EDGES) - 1):
        if HOURS_EDGES[index] <= hours < HOURS_EDGES[index + 1]:
            return index
    return len(HOURS_EDGES) - 2


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))


def _holding(intervals, value):
    for bucket, (low, high) in intervals.items():
        if (low is None or value >= low) and (high is None or value < high):
            return bucket
    return None


def _latest_formal_temperature(
    points: list[tuple[datetime, float]], start: datetime, until: datetime
) -> float | None:
    """The newest formal report in [start, until], or None."""

    import bisect  # noqa: PLC0415

    stamps = [stamp for stamp, _ in points]
    index = bisect.bisect_right(stamps, until) - 1
    if index < 0 or stamps[index] < start:
        return None
    return points[index][1]


def collect(target_db: Path, archive_db: Path) -> list[dict]:
    target = sqlite3.connect(f"file:{target_db}?mode=ro", uri=True, timeout=120.0)
    archive = sqlite3.connect(f"file:{archive_db}?mode=ro", uri=True, timeout=120.0)
    truth = {
        (str(r[0]), str(r[1])): float(r[2])
        for r in archive.execute(
            "SELECT station_id,event_date,observed_max_c FROM"
            " reported_trajectory_canonical_station_day_bounds"
            " WHERE observed_max_c IS NOT NULL")
    }
    # The formal report series per station, for the gap axis (v2): how far
    # the newest report at decision time sat below the running high.
    formal_points: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for r in archive.execute(
        "SELECT station_id,valid_at,reported_temperature_c"
        " FROM reported_trajectory_observations"
        " WHERE reported_temperature_c IS NOT NULL AND report_type IN (%s)"
        % ",".join("?" * len(FORMAL_REPORT_TYPES)),
        FORMAL_REPORT_TYPES,
    ):
        try:
            formal_points[str(r[0])].append((_parse(r[1]), float(r[2])))
        except (TypeError, ValueError):
            continue
    for points in formal_points.values():
        points.sort()
    zones = {
        (str(r[0]), str(r[1])): str(r[2])
        for r in target.execute(
            "SELECT DISTINCT station_id,event_date,settlement_timezone"
            " FROM basket_official_ws_snapshot")
    }
    # One belief per prefix burst: the chain writes a row per prefix step and
    # only the one it walked up to was ever believed.
    burst: dict[tuple, tuple] = {}
    from bayes_basket_target.probability_bridge import (  # noqa: PLC0415
        history_envelope_payload,
    )
    for row in target.execute(
        "SELECT station_id,event_date,decision_at_utc,bucket_intervals_json,"
        "envelope_json,checkpoint_id,envelope_content_sha256"
        " FROM basket_probability_bridge_history"
        " WHERE state='COMMITTED' AND stage='P1' ORDER BY decision_at_utc, rowid"
    ):
        key = (str(row[0]), str(row[1]))
        if key not in truth or key not in zones:
            continue
        envelope = history_envelope_payload(
            target,
            checkpoint_id=str(row[5]),
            envelope_content_sha256=str(row[6]),
            stored=str(row[4]) if row[4] else None,
        )
        running = (envelope.get("lineage") or {}).get(
            "metar_trajectory_running_high_c")
        if running in (None, ""):
            continue
        slot = (key[0], key[1], str(row[2])[:19])
        held = burst.get(slot)
        if held is None or float(running) >= held[0]:
            burst[slot] = (float(running), row[3], envelope, str(row[2]))
    samples = []
    for (station, day, _), (running, intervals_json, envelope, at) in burst.items():
        zone = ZoneInfo(zones[(station, day)])
        close = datetime.fromisoformat(day).replace(tzinfo=zone) + timedelta(days=1)
        hours = (close - _parse(at)).total_seconds() / 3600.0
        if hours < 0:
            continue
        intervals = json.loads(intervals_json)
        ordinals = envelope["bucket_ordinals"]
        running_bucket = _holding(intervals, running)
        truth_bucket = _holding(intervals, truth[(station, day)])
        if running_bucket is None or truth_bucket is None:
            continue
        if running_bucket not in ordinals or truth_bucket not in ordinals:
            continue
        latest = _latest_formal_temperature(
            formal_points.get(station, []), close - timedelta(days=1), _parse(at)
        )
        samples.append({
            "station_id": station,
            "event_date": day,
            "hours_left": hours,
            "still_rose": int(ordinals[truth_bucket] > ordinals[running_bucket]),
            # Degrees below the running high at decision time; None when the
            # day has no formal report yet (the sample still feeds the hour
            # marginal, just not a gap cell).
            "gap_c": None if latest is None else max(0.0, float(running) - latest),
        })
    return samples


def fit(samples: list[dict]) -> dict:
    per_bin = defaultdict(list)
    per_cell = defaultdict(list)
    for sample in samples:
        index = hours_bin(sample["hours_left"])
        per_bin[index].append(sample["still_rose"])
        gap = sample.get("gap_c")
        if gap is not None:
            per_cell[(index, gap_bin(float(gap)))].append(sample["still_rose"])
    pooled = (sum(v for values in per_bin.values() for v in values)
              / max(sum(len(v) for v in per_bin.values()), 1))
    table = {}
    for index in range(len(HOURS_EDGES) - 1):
        values = per_bin.get(index, [])
        # A bin under the floor describes its own thinness, not the hour.
        marginal = (
            (sum(values) / len(values)) if len(values) >= MIN_BIN_SAMPLES else pooled
        )
        cells = {}
        for gap_index in range(len(GAP_EDGES_C) + 1):
            cell_values = per_cell.get((index, gap_index), [])
            gap_from, gap_to = gap_cell_bounds(gap_index)
            # A thin cell serves its hour's marginal, never the pooled rate:
            # the hour is still the fact; only the gap split is unproven.
            cells[str(gap_index)] = {
                "gap_from": -1e9 if gap_from == float("-inf") else gap_from,
                "gap_to": gap_to,
                "p_still_rose": (
                    (sum(cell_values) / len(cell_values))
                    if len(cell_values) >= MIN_BIN_SAMPLES else marginal
                ),
                "sample_n": len(cell_values),
                "backed_off": len(cell_values) < MIN_BIN_SAMPLES,
            }
        table[str(index)] = {
            "hours_from": HOURS_EDGES[index],
            "hours_to": HOURS_EDGES[index + 1],
            "p_still_rose": marginal,
            "sample_n": len(values),
            "backed_off": len(values) < MIN_BIN_SAMPLES,
            "by_gap": cells,
        }
    return {
        "schema": SCHEMA,
        "pooled_p_still_rose": pooled,
        "sample_n": len(samples),
        "gap_sample_n": sum(1 for s in samples if s.get("gap_c") is not None),
        "station_n": len({s["station_id"] for s in samples}),
        "station_day_n": len({(s["station_id"], s["event_date"]) for s in samples}),
        "trained_through": max(s["event_date"] for s in samples),
        "trained_from": min(s["event_date"] for s in samples),
        "hours_edges": list(HOURS_EDGES),
        "gap_edges_c": list(GAP_EDGES_C),
        "gap_dimension": CEILING_GAP_DIMENSION_VERSION,
        "minimum_bin_samples": MIN_BIN_SAMPLES,
        "bins": table,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path,
                        default=ROOT / "data" / "bayes_basket_target_ssot.db")
    parser.add_argument("--archive", type=Path,
                        default=ROOT / "data" / "archive.db")
    parser.add_argument(
        "--through", type=str, default=None,
        help="Last event date to train on.  Serving refuses dates at or before"
             " it, so capping this below today is what makes the curve usable"
             " today instead of tomorrow.")
    parser.add_argument(
        "--replay-table", action="store_true",
        help="Emit one curve per event date, each fitted only on strictly"
             " earlier dates.  A replay that scored a day with a curve its own"
             " day helped fit would be reading its answer, and the single-cutoff"
             " artifact can only serve dates after its cutoff.")
    parser.add_argument("--out", type=Path,
                        default=ROOT / "data" / "runtime" /
                        "bayes_basket_target_nwp" / "metar_ceiling_artifact.json")
    args = parser.parse_args()
    samples = collect(args.db, args.archive)
    if args.through:
        samples = [s for s in samples if s["event_date"] <= args.through]
    if not samples:
        raise SystemExit("no samples")
    if args.replay_table:
        dates = sorted({s["event_date"] for s in samples})
        table = {}
        for date in dates:
            earlier = [s for s in samples if s["event_date"] < date]
            # A curve under this many samples is describing its own thinness:
            # at 440 samples the 2026-08-13 fit put 74.8% on "still rising"
            # inside the last hour, which is the opposite of the physics and
            # would have inflated the very mass this layer exists to cut.
            if len(earlier) < 2000:
                continue
            table[date] = fit(earlier)
        payload = {
            "schema": SCHEMA + "_replay",
            "dates": table,
            "covered_from": min(table) if table else None,
            "covered_through": max(table) if table else None,
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2, sort_keys=True),
                            encoding="utf-8")
        print(f"{args.out}")
        print(f"  {len(table)} 个事件日各有一条只用更早日期拟合的曲线 "
              f"({payload['covered_from']}..{payload['covered_through']})")
        for date in sorted(table)[:3] + sorted(table)[-2:]:
            bins = table[date]["bins"]
            near = bins["0"]["p_still_rose"]
            print("    %s  训练样本 %5d  0-1h 还会更热 %.1f%%"
                  % (date, table[date]["sample_n"], 100 * near))
        return 0
    artifact = fit(samples)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, indent=2, sort_keys=True),
                        encoding="utf-8")
    print(f"{args.out}")
    print(f"  样本 {artifact['sample_n']} 条 · {artifact['station_day_n']} 站日 · "
          f"{artifact['station_n']} 站 · {artifact['trained_from']}..{artifact['trained_through']}")
    for key in sorted(artifact["bins"], key=int):
        b = artifact["bins"][key]
        print("  %5.1f-%-5s h   还会更热 %5.1f%%   n=%5d%s" % (
            b["hours_from"],
            "∞" if b["hours_to"] > 1e8 else f"{b['hours_to']:.0f}",
            100 * b["p_still_rose"], b["sample_n"],
            "  (样本不足，退回池化)" if b["backed_off"] else ""))
        if b["hours_from"] < 16.0:
            for gap_key in sorted(b["by_gap"], key=int):
                cell = b["by_gap"][gap_key]
                label = (
                    "已在最高温" if cell["gap_from"] < 0 else
                    "低 %.0f°C" % (cell["gap_from"] + 0.5) if cell["gap_to"] < 1e8 else
                    "低 ≥%.0f°C" % (cell["gap_from"] + 0.5)
                )
                print("        %-10s  %5.1f%%   n=%5d%s" % (
                    label, 100 * cell["p_still_rose"], cell["sample_n"],
                    "  (退回本小时边际)" if cell["backed_off"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
