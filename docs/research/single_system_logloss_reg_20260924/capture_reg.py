#!/usr/bin/env python3
"""Unseen days only: production, unregularised and regularised log-loss arms.

The machinery is ``single_system_logloss_20260924/capture_ll.py`` (imported,
not copied): the vectorised truth probability checked against production on
every fit, the replay's walk-forward forecasts and scorer.  What changes is
the arm set and the scored event days (07-01..07-19 and 09-23, none of which
the previous round looked at).  See PROTOCOL.md.

    python3 capture_reg.py --stations EGLC,KLGA
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
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from scipy.optimize import minimize

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
spec = importlib.util.spec_from_file_location(
    "capture_ll", ROOT / "docs" / "research" / "single_system_logloss_20260924" / "capture_ll.py")
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)
oos, replay = C.oos, C.replay

CUTOFF_UTC = "2026-09-24T12:00:00Z"
TRAIN_FLOOR = "2026-06-01"
PERIODS = {
    # The days the previous round never looked at (PROTOCOL.md).
    "unseen": lambda day: ("2026-07-01" <= day <= "2026-07-19") or day == "2026-09-23",
    # The previous round's own days: the regularised arm's hyperparameters
    # are production constants, so this is a consistency read, not a fit.
    "seen": lambda day: "2026-07-20" <= day <= "2026-09-22",
    # Six families with modelled arrivals (PROTOCOL_six_families.md).
    "six": lambda day: "2026-07-20" <= day <= "2026-09-23",
}
# fusion_blend_historical_20260923/capture.py's --model-backfill-arrivals
# rule, read from that module so the two replays share one arrival clock.
_H = importlib.util.spec_from_file_location(
    "hist_capture", ROOT / "docs" / "research" / "fusion_blend_historical_20260923" / "capture.py")
HIST = importlib.util.module_from_spec(_H)
_H.loader.exec_module(HIST)
MODEL_BACKFILL_ARRIVALS = False
SCORED = PERIODS["unseen"]
ARMS = ("prod", "ll_both", "ll_both_reg")
REG_HALF_LIFE_DAYS = oos.RELIABILITY_HALF_LIFE_DAYS      # 15, production's own constant
REG_KAPPA = 0.5                                          # stable pool's shrinkage
OUT = HERE / "pass1"


def _decay(train, half_life):
    from datetime import date
    days = [date.fromisoformat(str(c["event_date"])[:10]) for c in train]
    anchor = max(days)
    return np.array([0.5 ** ((anchor - d).days / half_life) for d in days])


def _fit_both(train, params, half_life, x0_spread):
    weights = _decay(train, half_life)
    fit = minimize(
        lambda x: C._objective(train, weights, params, x[0], x[1]),
        x0=np.array([0.0, x0_spread]), method="L-BFGS-B",
        bounds=[C.DELTA_BOUNDS, C.SPREAD_BOUNDS], options={"maxiter": 60},
    )
    return float(fit.x[0]), float(fit.x[1])


FAST = False          # --fast: vectorised loss, reference arm dropped, day-end cut


def _case_arrays(train, params):
    width = max(len(c["values"]) for c in train)
    dev = np.zeros((len(train), width)); mask = np.zeros((len(train), width), dtype=bool)
    centers = np.zeros(len(train)); buckets = np.zeros(len(train))
    for row, case in enumerate(train):
        current = [float(v) for v in case["values"]]
        mean = sum(current) / len(current)
        centers[row] = mean + oos._resolved_bias_c(mean, params)
        dev[row, :len(current)] = [v - mean for v in current]
        mask[row, :len(current)] = True
        buckets[row] = int(oos._temperature_bucket(float(case["actual_tmax_c"])))
    return dev, mask, centers, buckets, mask.sum(axis=1)


def _probs_fast(arrays, delta, spread):
    from scipy.special import ndtr
    dev, mask, centers, buckets, counts = arrays
    bw = oos.KERNEL_BANDWIDTH_C
    dressed = (centers + delta)[:, None] + spread * dev
    middle = np.where(mask, dressed, 0.0).sum(axis=1) / counts
    std = np.sqrt(np.where(mask, (dressed - middle[:, None]) ** 2, 0.0).sum(axis=1) / counts)
    shrink = np.where(std > bw, np.sqrt(np.clip(1.0 - (bw / np.where(std > 0, std, 1.0)) ** 2, 0.0, 1.0)), 1.0)
    dressed = middle[:, None] + shrink[:, None] * (dressed - middle[:, None])
    low = np.floor(np.where(mask, dressed, np.inf).min(axis=1) - 4.0 * bw)
    high = np.ceil(np.where(mask, dressed, -np.inf).max(axis=1) + 4.0 * bw)

    def _mean(hi, lo):
        v = ndtr((hi[:, None] - dressed) / bw) - ndtr((lo[:, None] - dressed) / bw)
        return np.where(mask, v, 0.0).sum(axis=1) / counts

    mass = _mean(buckets + 0.5, buckets - 0.5)
    total = _mean(high + 0.5, low - 0.5)
    inside = (buckets >= low) & (buckets <= high) & (total > 0.0)
    return np.where(inside, mass / np.where(total > 0.0, total, 1.0), 0.0)


def _fit_both_fast(train, params, half_life, x0_spread):
    weights = _decay(train, half_life)
    arrays = _case_arrays(train, params)

    def objective(x):
        p = _probs_fast(arrays, float(x[0]), float(x[1]))
        return float(-(weights * np.log(np.maximum(p, C.P_FLOOR))).sum() / weights.sum())

    fit = minimize(objective, x0=np.array([0.0, x0_spread]), method="L-BFGS-B",
                   bounds=[C.DELTA_BOUNDS, C.SPREAD_BOUNDS], options={"maxiter": 60})
    return float(fit.x[0]), float(fit.x[1])


class RegForecasts(replay.Forecasts):
    def forecast(self, scope, case, knowable):
        train = self.train(scope, case["event_date"], knowable)
        key = (scope, case["event_date"], case["member_set_sha256"], tuple(c["event_date"] for c in train))
        if key in self.memo:
            return self.memo[key]
        if len(train) < replay.MIN_TRAIN:
            self.memo[key] = None
            return None
        params = oos._fit_parameters(train)
        if float(params.get("reliability_weight", 1.0)) != 1.0:
            raise RuntimeError("climate blend in play; the vectorised fit assumes none")
        C._parity(train, params)
        s_prod = float(params["spread_factor"])
        # Unregularised: the previous round exactly (its spread-only step
        # seeds the joint fit).
        if FAST:
            d_ll, s_ll = 0.0, s_prod     # reference arm not computed
            d15, s15 = _fit_both_fast(train, params, REG_HALF_LIFE_DAYS, s_prod)
        else:
            base = C.fit_logloss(train, params)
            d_ll, s_ll = base["delta_ll_both"], base["s_ll_both"]
            # Regularised: half-life 15, then half-way back to production.
            d15, s15 = _fit_both(train, params, REG_HALF_LIFE_DAYS, s_prod)
        d_reg = REG_KAPPA * 0.0 + (1 - REG_KAPPA) * d15
        s_reg = REG_KAPPA * s_prod + (1 - REG_KAPPA) * s15
        values = [float(v) for v in case["values"]]
        mean = sum(values) / len(values)
        arms = {
            "prod": params,
            "ll_both": C._shifted(params, d_ll, s_ll),
            "ll_both_reg": C._shifted(params, d_reg, s_reg),
        }
        row = {
            "event_date": case["event_date"],
            "actual_tmax_c": float(case["actual_tmax_c"]),
            "error_scale": float(params.get("train_residual_sigma_c") or params["train_rmse_c"]),
            "train_n": len(train),
            "residuals": {
                c["event_date"]: float(c["actual_tmax_c"]) - sum(float(v) for v in c["values"]) / len(c["values"])
                for c in train
            },
            "arms": {arm: {"probability": oos._apply_parameters(values, p),
                           "calibrated_center_c": oos._corrected_center(mean, p)}
                     for arm, p in arms.items()},
            "fit": {"s_prod": s_prod, "delta_ll": d_ll, "s_ll": s_ll,
                    "delta_15": d15, "s_15": s15, "delta_reg": d_reg, "s_reg": s_reg},
        }
        row["probability"] = row["arms"]["prod"]["probability"]
        row["calibrated_center_c"] = row["arms"]["prod"]["calibrated_center_c"]
        self.memo[key] = row
        return row


def capture_station(conn, station, tz):
    """capture_ll's loop, scoring only the unseen event days."""

    replay.FIRST_EVENT_DATE = TRAIN_FLOOR
    natives = set(oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS)
    systems = oos._load_systems(conn)
    family = {s: str(systems[s]["independence_family_id"]) for s in natives if s in systems}
    all_cases = oos._load_cases(conn, station_id=station, as_of_utc=CUTOFF_UTC)
    cases_by_scope = {
        scope: sorted(rows, key=lambda c: (c["event_date"], c["aggregator_snapshot_at_utc"]))
        for scope, rows in all_cases.items()
        if scope[0] in family and all(c.get("transport_provider_id") != "OPEN_METEO" for c in rows)
    }
    first_commit = replay._first_truth_commit(conn, station)
    fetched = replay._arrivals(conn, station)

    def arrival_of(scope, case):
        init = replay._run_init(case)
        key = (scope[0], case["event_date"], str(case.get("archive_lead_bucket") or ""), scope[3], init)
        arrived = fetched.get(key) or replay._utc(case["aggregator_snapshot_at_utc"])
        if MODEL_BACKFILL_ARRIVALS and init and scope[0] in ("ECMWF_AIFS_ENS_GLOBAL", "NOAA_AIGEFS_025"):
            run = replay._utc(init)
            if arrived - run > HIST.BACKFILL_LAG:
                if scope[0] == "NOAA_AIGEFS_025":
                    return run + HIST.AIGEFS_ARRIVAL_AFTER_INIT
                twin = fetched.get(("ECMWF_IFS_ENS_OPEN_PF50",) + key[1:])
                return twin if twin is not None and twin - run <= HIST.BACKFILL_LAG else run + HIST.AIFS_FALLBACK_AFTER_INIT
        return arrived

    forecasts = RegForecasts(cases_by_scope, first_commit, arrival_of)
    try:
        latitude = float(C.station_coordinates(station)["latitude"])
    except Exception:  # noqa: BLE001
        latitude = None
    arrivals = defaultdict(list)
    for scope, rows in cases_by_scope.items():
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
            if FAST and at >= day_end:
                continue
            day_rows = {}
            for s, (_at, _init, s_scope, s_case) in packet.items():
                row = forecasts.forecast(s_scope, s_case, at)
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
            signals = oos._holdout_conditioning_signals(
                conn, station_id=station, holdout_days=[event_date],
                predictions_by_system={s: {event_date: day_rows[s]} for s in pool},
                radiation_shift_sigma=C.radiation_shift_sigma_for_lead(newest[2]),
            )
            cooling = (signals.get(event_date) or {}).get("synoptic_cooling_shift_sigma")
            skill, _rule = oos._seat_weights(
                residuals_by_system={s: day_rows[s]["residuals"] for s in pool},
                train_error_scale_by_system={s: day_rows[s]["error_scale"] for s in pool},
                family_by_system={s: family[s] for s in pool}, family_caps=oos.DEFAULT_FAMILY_WEIGHT_CAPS,
                train_n_by_system={s: day_rows[s]["train_n"] for s in pool},
            )
            skill = {s: float(skill.get(s, 0.0)) for s in pool}
            total = sum(skill.values())
            skill = {s: v / total for s, v in skill.items()}
            seat = C.stable_pool_weights(skill, {s: family[s] for s in pool}, kappa=C.STABLE_POOL_KAPPA)
            seat = {s: v for s, v in seat.items() if v > 0.0}
            scored = {}
            for arm in (("prod", "ll_both_reg") if FAST else ARMS):
                result = replay._blend_and_score(
                    weights=seat, day_rows={s: day_rows[s]["arms"][arm] for s in pool},
                    cooling_shift_sigma=cooling, latitude=latitude, tables=tables, actual=actual,
                )
                scored[arm] = {"mean": result["mean"], "served": result["served"], "vec": result["vec"]}
            out.append({
                "station": station, "event_date": event_date, "tz": tz,
                "at": C._iso(at), "truth_known_at": C._iso(known), "lead": newest[2],
                "actual": actual, "pool": pool, "w": {s: round(v, 5) for s, v in seat.items()},
                "fit": {s: {k: round(v, 5) for k, v in day_rows[s]["fit"].items()} for s in pool},
                **scored,
            })
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stations", required=True)
    parser.add_argument("--period", choices=tuple(PERIODS), default="unseen")
    parser.add_argument("--model-backfill-arrivals", action="store_true")
    parser.add_argument("--fast", action="store_true")
    args = parser.parse_args()
    global SCORED, OUT, MODEL_BACKFILL_ARRIVALS, FAST
    MODEL_BACKFILL_ARRIVALS = bool(args.model_backfill_arrivals)
    FAST = bool(args.fast)
    SCORED = PERIODS[args.period]
    if args.period != "unseen":
        OUT = HERE / f"pass1_{args.period}"
    OUT.mkdir(exist_ok=True)
    conn = sqlite3.connect(f"file:{C.SSOT}?mode=ro", uri=True, timeout=30)
    zones = dict(conn.execute(
        "SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding"
    ).fetchall())
    for station in [s.strip().upper() for s in args.stations.split(",") if s.strip()]:
        path = OUT / f"{station}.jsonl.gz"
        if path.exists():
            continue
        t0 = time.time()
        rows = capture_station(conn, station, zones[station])
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        tmp.rename(path)
        print(f"{station}: {len(rows)} updates, {time.time() - t0:.0f}s", flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
