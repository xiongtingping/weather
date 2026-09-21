#!/usr/bin/env python3
"""Research only: which daily-high estimator to fix for AIFS ENS six-hourly 2t.

AIFS ENS open data has no period maximum, so production must turn at most
four instantaneous samples inside the local day into a daily high -- and the
extraction version will freeze that choice, so it is compared here first, on
the instants ``research_aifs_ens_history.py`` keeps:

  E0  the largest in-day instant (what the backfill's member rows hold);
  E1  a 24-hour harmonic through the in-day instants, phase fixed at the
      station's own typical peak hour (from its METARs), peak = a + b;
  E2  the same harmonic fitted to the in-day instants plus the one either
      side of the day, which constrains the amplitude better.

E1 and E2 never go below E0: a member's continuous curve cannot peak below
one of its own samples.  Each is debiased per station and lead bucket
leave-one-date-out (the per-system fit production applies does at least
that), then scored by CRPS against settled truth; intervals resample whole
dates.  Same-day runs with fewer than four in-day instants are left out --
there the estimators differ only in what the run could not see.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
import statistics
import sys
import zlib
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "data" / "aifs_ens_research"
DB = RESEARCH / "aifs_ens_members.db"
SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
PEAKS = RESEARCH / "station_peak_hour_local.json"
KELVIN = 273.15


def harmonic_peak(hours: np.ndarray, values: np.ndarray, phase: float) -> np.ndarray:
    """Per-member a + b of T = a + b cos(2 pi (h - phase) / 24); values (n, m)."""

    x = np.cos(2 * math.pi * (hours - phase) / 24.0)
    design = np.stack([np.ones_like(x), x], axis=1)
    coef, *_ = np.linalg.lstsq(design, values, rcond=None)
    a, b = coef[0], coef[1]
    return a + np.maximum(b, 0.0)


def crps(members: np.ndarray, y: float) -> float:
    m = np.sort(members)
    n = len(m)
    term1 = np.mean(np.abs(m - y))
    # E|X - X'| for the empirical ensemble, via the sorted form.
    term2 = np.sum((2 * np.arange(1, n + 1) - n - 1) * m) * 2.0 / (n * n)
    return float(term1 - 0.5 * term2)


def main() -> int:
    peaks = {k: v["peak_hour_local"] for k, v in json.loads(PEAKS.read_text()).items()}
    s = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=60)
    tz = dict(s.execute("SELECT station_id,settlement_timezone FROM calibration_station_market_timezone_binding"))
    truth: dict[tuple[str, str], tuple[float, int]] = {}
    for st, d, t, rev in s.execute(
        "SELECT station_id,local_date,official_tmax_c,revision_no FROM calibration_station_daily_high_truth "
        "WHERE qc_status='ACCEPTED' AND official_tmax_c IS NOT NULL"
    ):
        if (st, d) not in truth or rev > truth[(st, d)][1]:
            truth[(st, d)] = (float(t), int(rev))
    r = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    pairs = defaultdict(set)
    buckets = {}
    for st, d, run, bucket in r.execute(
        "SELECT DISTINCT station_id,target_local_date,native_run_init_utc,archive_lead_bucket "
        "FROM calibration_native_ensemble_historical_member_run"
    ):
        pairs[run].add((st, d))
        buckets[(st, d, run)] = bucket
    cases = []
    for run, steps_json, members_json, stations_json, blob in r.execute(
        "SELECT run_init_utc,steps_json,members_json,stations_json,kelvin_f32_zlib FROM research_aifs_run_samples"
    ):
        steps = json.loads(steps_json)
        members = json.loads(members_json)
        stations = json.loads(stations_json)
        cube = np.frombuffer(zlib.decompress(blob), dtype=np.float32).reshape(
            len(steps), len(members), len(stations)
        ).astype(np.float64) - KELVIN
        init = datetime.fromisoformat(run.replace("Z", "+00:00"))
        for st, d in pairs.get(run, ()):
            if st not in tz or st not in peaks or (st, d) not in truth or st not in stations:
                continue
            zone = ZoneInfo(tz[st])
            day = date.fromisoformat(d)
            start = datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(timezone.utc)
            end = start + timedelta(days=1)
            inside = [i for i, h in enumerate(steps) if start <= init + timedelta(hours=h) < end]
            if len(inside) != 4:
                continue
            around = [
                i for i, h in enumerate(steps)
                if start - timedelta(hours=6) <= init + timedelta(hours=h) < end + timedelta(hours=6)
            ]
            j = stations.index(st)
            local_hours = np.array([
                (init + timedelta(hours=steps[i])).astimezone(zone).hour
                + (init + timedelta(hours=steps[i])).astimezone(zone).minute / 60.0
                + (24.0 if (init + timedelta(hours=steps[i])).astimezone(zone).date() > day else 0.0)
                - (24.0 if (init + timedelta(hours=steps[i])).astimezone(zone).date() < day else 0.0)
                for i in around
            ])
            values_around = cube[around, :, j]
            values_inside = cube[inside, :, j]
            e0 = values_inside.max(axis=0)
            in_mask = np.isin(around, inside)
            e1 = np.maximum(e0, harmonic_peak(local_hours[in_mask], values_around[in_mask], peaks[st]))
            e2 = np.maximum(e0, harmonic_peak(local_hours, values_around, peaks[st]))
            cases.append({
                "st": st, "d": d, "bucket": buckets.get((st, d, run), ""), "y": truth[(st, d)][0],
                "E0": e0, "E1": e1, "E2": e2,
            })
    names = ("E0", "E1", "E2")
    by_scope = defaultdict(list)
    for c in cases:
        for k in names:
            by_scope[(c["st"], c["bucket"], k)].append((c["d"], float(np.mean(c[k])) - c["y"]))
    rows = []
    for c in cases:
        row = {"d": c["d"], "bucket": c["bucket"], "st": c["st"]}
        ok = True
        for k in names:
            others = [e for dd, e in by_scope[(c["st"], c["bucket"], k)] if dd != c["d"]]
            if len(others) < 5:
                ok = False
                break
            row[k] = crps(c[k] - statistics.fmean(others), c["y"])
            row[f"raw_{k}"] = float(np.mean(c[k])) - c["y"]
        if ok:
            rows.append(row)
    rng = random.Random(5)
    by_date = defaultdict(list)
    for row in rows:
        by_date[row["d"]].append(row)
    dates = sorted(by_date)

    def boot(f):
        point = statistics.fmean(f(x) for x in rows)
        draws = sorted(
            statistics.fmean(f(x) for d in rng.choices(dates, k=len(dates)) for x in by_date[d])
            for _ in range(2000)
        )
        return round(point, 4), round(draws[50], 4), round(draws[1949], 4)

    report = {
        "cases": len(rows), "dates": len(dates), "stations": len({x["st"] for x in rows}),
        "crps_debiased": {k: round(statistics.fmean(x[k] for x in rows), 4) for k in names},
        "raw_mean_error": {k: round(statistics.fmean(x[f"raw_{k}"] for x in rows), 3) for k in names},
        "E1_minus_E0": boot(lambda x: x["E1"] - x["E0"]),
        "E2_minus_E0": boot(lambda x: x["E2"] - x["E0"]),
    }
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
