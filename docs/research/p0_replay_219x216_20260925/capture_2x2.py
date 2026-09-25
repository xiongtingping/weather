#!/usr/bin/env python3
"""Six-family full-chain replay, 2x2: training set (219 off/on) x fit rule (moment/216).

The machinery is ``single_system_logloss_reg_20260924/capture_reg.py``'s
(imported, not copied): its modelled backfill arrivals, the replay's
walk-forward training window, production's seat weights, bridge, shape stack
and served tables.  What changes (PROTOCOL.md):

* every fit is production's ``_fit_parameters`` with ``SINGLE_SYSTEM_FIT_RULE``
  set per arm -- no research re-implementation of charter 216;
* served runs come from the corrected (charter 219) reader in every arm; the
  contaminated reader (``live_never_seats`` forced false) only supplies the
  ``*_off`` arms' training cases;
* seats are computed per training set.

    python3 capture_2x2.py --stations EGLC,KLGA
"""
from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
_spec = importlib.util.spec_from_file_location(
    "capture_reg", ROOT / "docs" / "research" / "single_system_logloss_reg_20260924" / "capture_reg.py")
REG = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(REG)
C, oos, replay, HIST = REG.C, REG.oos, REG.replay, REG.HIST

CUTOFF_UTC = REG.CUTOFF_UTC                  # 2026-09-24T12:00:00Z, as the six-family run
TRAIN_FLOOR = REG.TRAIN_FLOOR
SCORED = REG.PERIODS["six"]                  # 2026-07-20 .. 2026-09-23
RULES = {"mom": oos.SINGLE_SYSTEM_FIT_RULE_MOMENT, "ll": oos.SINGLE_SYSTEM_FIT_RULE_LOGLOSS}
VARIANTS = ("off", "on")
ARMS = tuple(f"{r}_{v}" for v in VARIANTS for r in RULES)
OUT = HERE / "pass1"
AI_SYSTEMS = ("ECMWF_AIFS_ENS_GLOBAL", "NOAA_AIGEFS_025")


def _cases(conn, station, family, *, contaminated):
    original = oos.live_never_seats
    if contaminated:
        oos.live_never_seats = lambda *_a, **_k: False
    try:
        all_cases = oos._load_cases(conn, station_id=station, as_of_utc=CUTOFF_UTC)
    finally:
        oos.live_never_seats = original
    return {
        scope: sorted(rows, key=lambda c: (c["event_date"], c["aggregator_snapshot_at_utc"]))
        for scope, rows in all_cases.items()
        if scope[0] in family and all(c.get("transport_provider_id") != "OPEN_METEO" for c in rows)
    }


class Fits:
    """Production fits memoised on (rule, scope, training signature)."""

    def __init__(self):
        self.memo = {}
        self.calls = 0

    def params(self, rule, scope, train):
        key = (rule, scope, tuple((c["event_date"], c["member_set_sha256"]) for c in train))
        if key not in self.memo:
            oos.SINGLE_SYSTEM_FIT_RULE = RULES[rule]
            try:
                self.memo[key] = oos._fit_parameters(train)
            finally:
                oos.SINGLE_SYSTEM_FIT_RULE = RULES["ll"]
            self.calls += 1
        return self.memo[key]


def capture_station(conn, station, tz):
    replay.FIRST_EVENT_DATE = TRAIN_FLOOR
    natives = set(oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS)
    systems = oos._load_systems(conn)
    family = {s: str(systems[s]["independence_family_id"]) for s in natives if s in systems}
    by_variant = {"on": _cases(conn, station, family, contaminated=False),
                  "off": _cases(conn, station, family, contaminated=True)}
    served_cases = by_variant["on"]
    first_commit = replay._first_truth_commit(conn, station)
    fetched = replay._arrivals(conn, station)

    def arrival_of(scope, case):
        # capture_reg's --model-backfill-arrivals rule, verbatim.
        init = replay._run_init(case)
        key = (scope[0], case["event_date"], str(case.get("archive_lead_bucket") or ""), scope[3], init)
        arrived = fetched.get(key) or replay._utc(case["aggregator_snapshot_at_utc"])
        if init and scope[0] in AI_SYSTEMS:
            run = replay._utc(init)
            if arrived - run > HIST.BACKFILL_LAG:
                if scope[0] == "NOAA_AIGEFS_025":
                    return run + HIST.AIGEFS_ARRIVAL_AFTER_INIT
                twin = fetched.get(("ECMWF_IFS_ENS_OPEN_PF50",) + key[1:])
                return twin if twin is not None and twin - run <= HIST.BACKFILL_LAG else run + HIST.AIFS_FALLBACK_AFTER_INIT
        return arrived

    def train(variant, scope, event_date, knowable):
        start = (date.fromisoformat(event_date) - timedelta(days=oos.ROLLING_TRAIN_WINDOW_DAYS)).isoformat()
        return [
            c for c in by_variant[variant].get(scope, [])
            if start <= c["event_date"] < event_date
            and arrival_of(scope, c) < knowable
            and c["event_date"] in first_commit and first_commit[c["event_date"]] < knowable
        ]

    fits = Fits()
    memo = {}

    def forecast(scope, case, knowable):
        trains = {v: train(v, scope, case["event_date"], knowable) for v in VARIANTS}
        key = (scope, case["event_date"], case["member_set_sha256"],
               tuple(tuple((c["event_date"], c["member_set_sha256"]) for c in trains[v]) for v in VARIANTS))
        if key in memo:
            return memo[key]
        if any(len(trains[v]) < replay.MIN_TRAIN for v in VARIANTS):
            memo[key] = None
            return None
        values = [float(v) for v in case["values"]]
        mean = sum(values) / len(values)
        row = {"actual_tmax_c": float(case["actual_tmax_c"]), "arms": {}, "seat_inputs": {},
               "changed": trains["on"] != trains["off"]}
        for variant in VARIANTS:
            for rule in RULES:
                p = fits.params(rule, scope, trains[variant])
                if float(p.get("reliability_weight", 1.0)) != 1.0:
                    raise RuntimeError("climate blend in play")
                row["arms"][f"{rule}_{variant}"] = {
                    "probability": oos._apply_parameters(values, p),
                    "calibrated_center_c": oos._corrected_center(mean, p),
                    "spread_factor": float(p["spread_factor"]),
                }
            moment = fits.params("mom", scope, trains[variant])
            row["seat_inputs"][variant] = {
                "error_scale": float(moment.get("train_residual_sigma_c") or moment["train_rmse_c"]),
                "train_n": len(trains[variant]),
                "residuals": {c["event_date"]: float(c["actual_tmax_c"]) - sum(float(v) for v in c["values"]) / len(c["values"])
                              for c in trains[variant]},
            }
        memo[key] = row
        return row

    try:
        latitude = float(C.station_coordinates(station)["latitude"])
    except Exception:  # noqa: BLE001
        latitude = None
    arrivals = defaultdict(list)
    for scope, rows in served_cases.items():
        for case in rows:
            if SCORED(case["event_date"]):
                arrivals[case["event_date"]].append((arrival_of(scope, case), replay._run_init(case), scope[0], scope, case))
    zone = ZoneInfo(tz)
    tables_memo = {}
    out = []
    for event_date in sorted(arrivals):
        if event_date not in first_commit:
            continue
        day_end = datetime.fromisoformat(event_date).replace(tzinfo=zone) + timedelta(days=1)
        known = max(first_commit[event_date], day_end)
        events = sorted(arrivals[event_date], key=lambda e: (e[0], e[1], e[2]))
        packet = {}
        previous = None
        for index, (at, run_init, system, scope, case) in enumerate(events):
            current = packet.get(system)
            if current is None or (run_init, at) >= (current[1], current[0]):
                packet[system] = (at, run_init, scope, case)
            if index + 1 < len(events) and events[index + 1][0] == at:
                continue
            signature = tuple(sorted((s, v[2], v[3]["member_set_sha256"]) for s, v in packet.items()))
            if signature == previous:
                continue
            previous = signature
            if at >= day_end:          # decisions before the local day's end only
                continue
            day_rows = {}
            for s, (_at, _init, s_scope, s_case) in packet.items():
                row = forecast(s_scope, s_case, at)
                if row is not None:
                    day_rows[s] = row
            pool = sorted(day_rows)
            if len({family[s] for s in pool}) < 2:
                continue
            actual = day_rows[pool[0]]["actual_tmax_c"]
            hour_key = at.strftime("%Y-%m-%dT%H")
            if hour_key not in tables_memo:
                tables_memo[hour_key] = oos.p0_gate_tables(conn, station_id=station, tables_as_of_utc=C._iso(at))
            tables = tables_memo[hour_key]
            newest = max(packet.values(), key=lambda v: (v[1], v[0]))[2]
            base = {s: {"probability": day_rows[s]["arms"]["ll_on"]["probability"],
                        "calibrated_center_c": day_rows[s]["arms"]["ll_on"]["calibrated_center_c"]} for s in pool}
            signals = oos._holdout_conditioning_signals(
                conn, station_id=station, holdout_days=[event_date],
                predictions_by_system={s: {event_date: base[s]} for s in pool},
                radiation_shift_sigma=C.radiation_shift_sigma_for_lead(newest[2]),
            )
            cooling = (signals.get(event_date) or {}).get("synoptic_cooling_shift_sigma")
            seats = {}
            for variant in VARIANTS:
                inputs = {s: day_rows[s]["seat_inputs"][variant] for s in pool}
                skill, _rule = oos._seat_weights(
                    residuals_by_system={s: inputs[s]["residuals"] for s in pool},
                    train_error_scale_by_system={s: inputs[s]["error_scale"] for s in pool},
                    family_by_system={s: family[s] for s in pool}, family_caps=oos.DEFAULT_FAMILY_WEIGHT_CAPS,
                    train_n_by_system={s: inputs[s]["train_n"] for s in pool},
                )
                skill = {s: float(skill.get(s, 0.0)) for s in pool}
                total = sum(skill.values())
                skill = {s: v / total for s, v in skill.items()}
                seat = C.stable_pool_weights(skill, {s: family[s] for s in pool}, kappa=C.STABLE_POOL_KAPPA)
                seats[variant] = {s: v for s, v in seat.items() if v > 0.0}
            scored = {}
            for arm in ARMS:
                variant = arm.split("_")[1]
                result = replay._blend_and_score(
                    weights=seats[variant],
                    day_rows={s: day_rows[s]["arms"][arm] for s in pool if s in seats[variant]},
                    cooling_shift_sigma=cooling, latitude=latitude, tables=tables, actual=actual,
                )
                scored[arm] = {"mean": result["mean"], "served": result["served"], "vec": result["vec"]}
            out.append({
                "station": station, "event_date": event_date, "tz": tz,
                "at": C._iso(at), "truth_known_at": C._iso(known), "lead": newest[2],
                "actual": actual, "pool": pool,
                "changed": sorted(s for s in pool if day_rows[s]["changed"]),
                "w_on": {s: round(v, 5) for s, v in seats["on"].items()},
                "w_off": {s: round(v, 5) for s, v in seats["off"].items()},
                "spread": {s: {arm: round(day_rows[s]["arms"][arm]["spread_factor"], 4) for arm in ARMS} for s in pool},
                **scored,
            })
    return out, fits.calls


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stations", required=True)
    args = parser.parse_args()
    OUT.mkdir(exist_ok=True)
    conn = sqlite3.connect(f"file:{C.SSOT}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA query_only=ON")
    zones = dict(conn.execute(
        "SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding"
    ).fetchall())
    for station in [s.strip().upper() for s in args.stations.split(",") if s.strip()]:
        path = OUT / f"{station}.jsonl.gz"
        if path.exists():
            continue
        t0 = time.time()
        rows, calls = capture_station(conn, station, zones[station])
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        tmp.rename(path)
        print(f"{station}: {len(rows)} updates, {calls} fits, {time.time() - t0:.0f}s", flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
