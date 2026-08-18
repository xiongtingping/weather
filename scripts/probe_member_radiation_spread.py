#!/usr/bin/env python3
"""Does the ensemble's *disagreement* about radiation predict temperature error?

The served distribution already leans on radiation, but only on its level:
one deterministic figure per station-day, which says how bright the day will
be and nothing about how sure anyone is of that.  Measured over 376 settled
station-days, that level is a centre signal and not a width one -- signed
residual correlates +0.237 with it, absolute residual +0.027, and the
quadratic term that a "both extremes are easy" story would need is +0.018.

That is the expected result for a first moment, and it says nothing about the
second.  A day everyone agrees will be sunny and a day the members split over
carry the same level and should not carry the same uncertainty.  The quantity
that would separate them is the spread of radiation across members, which the
capture never extracted: the lane pulls ``TMP:2 m above ground`` out of each
GEFS message and leaves the rest.

``DSWRF:surface`` sits in the same pgrb2a object, in the same index, reachable
by the same byte range.  So this is cheap to answer before anything is
engineered: pull per-member radiation for a handful of settled station-days
that already have a served envelope and a truth, and ask whether the member
spread of radiation correlates with the size of the standardised residual.

Read-only.  Downloads bounded index and message ranges, writes nothing but
its report, and places no orders.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.noaa_gefs_archive import (  # noqa: E402
    BASE_URL,
    ECCODES_GRIB_GET,
    GRID,
    MAX_INDEX_BYTES,
    MAX_MESSAGE_BYTES,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    station_coordinates,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
RADIATION_INDEX_KEY = ":DSWRF:surface:"
# The control plus the first twenty perturbed members.  Enough to estimate a
# spread; the point is a correlation, not a calibrated parameter.
MEMBERS = ["gec00"] + [f"gep{index:02d}" for index in range(1, 21)]


def _index_range(index_text: str, key: str) -> tuple[int, int, str]:
    lines = index_text.splitlines()
    hits = [
        (position, line)
        for position, line in enumerate(lines)
        if key in line
    ]
    if len(hits) != 1:
        raise RuntimeError(f"index_key_not_unique:{key}:{len(hits)}")
    position, line = hits[0]
    if position + 1 >= len(lines):
        raise RuntimeError("index_range_incomplete")
    start = int(line.split(":", 2)[1])
    end = int(lines[position + 1].split(":", 2)[1]) - 1
    if start < 0 or end < start or end - start + 1 > MAX_MESSAGE_BYTES:
        raise RuntimeError("index_range_invalid")
    return start, end, line


def _decode_at(message: bytes, *, latitude: float, longitude: float) -> float:
    if not ECCODES_GRIB_GET.is_file():
        raise RuntimeError("eccodes_grib_get_missing")
    with tempfile.NamedTemporaryFile(suffix=".grib2") as handle:
        handle.write(message)
        handle.flush()
        result = subprocess.run(
            [
                str(ECCODES_GRIB_GET),
                "-l", f"{latitude},{longitude},1",
                handle.name,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60.0,
        )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"grib_get_failed:{result.returncode}")
    return float(result.stdout.split()[0])


def member_radiation(
    client: httpx.Client,
    *,
    run_day: str,
    cycle: str,
    forecast_hour: int,
    latitude: float,
    longitude: float,
    members: list[str],
) -> list[float]:
    values: list[float] = []
    for member in members:
        key = (
            f"gefs.{run_day}/{cycle}/atmos/pgrb2ap5/"
            f"{member}.t{cycle}z.pgrb2a.{GRID}.f{forecast_hour:03d}"
        )
        url = f"{BASE_URL}/{key}"
        index = client.get(url + ".idx", timeout=30.0)
        if index.status_code != 200 or len(index.content) > MAX_INDEX_BYTES:
            continue
        try:
            start, end, _line = _index_range(index.text, RADIATION_INDEX_KEY)
        except RuntimeError:
            continue
        body = client.get(
            url, headers={"Range": f"bytes={start}-{end}"}, timeout=60.0
        )
        if body.status_code not in (200, 206):
            continue
        try:
            values.append(
                _decode_at(
                    body.content, latitude=latitude, longitude=longitude
                )
            )
        except RuntimeError:
            continue
    return values


def _served(db: Path, *, limit: int) -> list[dict[str, Any]]:
    """Settled station-days with a served P0 envelope, newest first."""

    import sqlite3

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT e.station_id, e.event_date, e.bucket_ordinals_json,"
        " e.probabilities_json, t.official_tmax_c"
        " FROM basket_probability_envelope e"
        " JOIN calibration_station_daily_high_truth t"
        "  ON t.station_id=e.station_id AND t.local_date=e.event_date"
        "  AND t.qc_status='ACCEPTED'"
        " WHERE e.stage='P0' AND e.state='COMMITTED'"
        " GROUP BY e.station_id, e.event_date"
        " ORDER BY e.event_date DESC"
    ).fetchall()
    conn.close()
    out: list[dict[str, Any]] = []
    for station, day, ordinals_json, probabilities_json, actual in rows:
        ordinals = json.loads(str(ordinals_json))
        keys = sorted(ordinals, key=lambda key: ordinals[key])
        fahrenheit = any("°F" in key for key in keys)
        centres = []
        for key in keys:
            digits = [float(v) for v in re.findall(r"-?\d+", key)]
            if not digits:
                centres = []
                break
            value = sum(digits) / len(digits)
            centres.append(
                (value - 32.0) * 5.0 / 9.0 if fahrenheit else value
            )
        if len(centres) != len(keys) or len(centres) < 4:
            continue
        step = statistics.median(
            centres[i + 1] - centres[i] for i in range(1, len(centres) - 2)
        )
        centres[0] = centres[1] - step
        centres[-1] = centres[-2] + step
        probabilities = json.loads(str(probabilities_json))
        pairs = [
            (centres[i], float(probabilities.get(key, 0.0)))
            for i, key in enumerate(keys)
        ]
        total = sum(p for _c, p in pairs)
        if total <= 0.0:
            continue
        pairs = [(c, p / total) for c, p in pairs]
        mean = sum(c * p for c, p in pairs)
        sigma = math.sqrt(sum(p * (c - mean) ** 2 for c, p in pairs))
        if sigma < 0.2:
            continue
        out.append({
            "station_id": str(station),
            "event_date": str(day),
            "z": (float(actual) - mean) / sigma,
        })
        if len(out) >= limit:
            break
    return out


def _correlation(left: list[float], right: list[float]) -> float:
    if len(left) < 3:
        return 0.0
    mean_l = statistics.mean(left)
    mean_r = statistics.mean(right)
    numerator = sum(
        (a - mean_l) * (b - mean_r) for a, b in zip(left, right)
    )
    scale = math.sqrt(
        sum((a - mean_l) ** 2 for a in left)
        * sum((b - mean_r) ** 2 for b in right)
    )
    return numerator / scale if scale else 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--station-days", type=int, default=40)
    parser.add_argument("--members", type=int, default=11)
    parser.add_argument("--cycle", default="00")
    parser.add_argument(
        "--forecast-hour",
        type=int,
        default=24,
        help="Lead in hours from the run to the target day.",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    targets = _served(args.db, limit=args.station_days)
    members = MEMBERS[: max(3, args.members)]
    records: list[dict[str, Any]] = []
    with httpx.Client(follow_redirects=True) as client:
        for target in targets:
            try:
                coordinates = station_coordinates(target["station_id"])
            except Exception:  # noqa: BLE001
                continue
            day = target["event_date"].replace("-", "")
            run_day = (
                f"{day[:4]}{day[4:6]}"
                f"{int(day[6:]) - args.forecast_hour // 24:02d}"
            )
            values = member_radiation(
                client,
                run_day=run_day,
                cycle=args.cycle,
                forecast_hour=args.forecast_hour,
                latitude=float(coordinates["latitude"]),
                longitude=float(coordinates["longitude"]),
                members=members,
            )
            if len(values) < 3:
                continue
            records.append({
                **target,
                "member_count": len(values),
                "radiation_mean": statistics.mean(values),
                "radiation_spread": statistics.pstdev(values),
            })

    report: dict[str, Any] = {
        "schema": "member_radiation_spread_probe_v1",
        "station_day_count": len(records),
        "members_requested": len(members),
        "forecast_hour": args.forecast_hour,
        "calls_order_gateway": False,
        "writes_target_database": False,
    }
    if len(records) >= 8:
        # Spread standardised per station: absolute radiation is mostly
        # latitude and season, and the question is whether *this* day's
        # members disagree unusually much for *this* station.
        by_station: dict[str, list[float]] = defaultdict(list)
        for record in records:
            by_station[record["station_id"]].append(record["radiation_spread"])
        standardised = []
        for record in records:
            series = by_station[record["station_id"]]
            if len(series) < 2:
                continue
            centre = statistics.mean(series)
            scale = statistics.pstdev(series) or 1.0
            standardised.append({
                **record,
                "z_spread": (record["radiation_spread"] - centre) / scale,
            })
        if len(standardised) >= 8:
            spread = [r["z_spread"] for r in standardised]
            report["standardised_count"] = len(standardised)
            report["corr_abs_residual_vs_member_spread"] = _correlation(
                spread, [abs(r["z"]) for r in standardised]
            )
            report["corr_signed_residual_vs_member_spread"] = _correlation(
                spread, [r["z"] for r in standardised]
            )
            report["median_member_spread"] = statistics.median(
                r["radiation_spread"] for r in records
            )
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
