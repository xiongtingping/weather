#!/usr/bin/env python3
"""Pass 1 with three single-system arms: production, LogLoss spread, LogLoss both.

The fusion historical study's capture (same production replay functions,
same updates, same tables), except that each system's walk-forward fit is
also refitted by bucket log loss on the same training cases, and every
update is blended and scored three times under the one seat.  Read-only;
resumable per station.  See PROTOCOL.md.

    python3 capture_ll.py --stations EGLC,KLGA
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from scipy.optimize import minimize, minimize_scalar
from scipy.special import ndtr

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import replay_p0_stable_pool as replay  # noqa: E402
from bayes_basket_target import native_ensemble_oos as oos  # noqa: E402
from bayes_basket_target.cross_consensus import radiation_shift_sigmas_for_lead  # noqa: E402
from bayes_basket_target.stable_pool import STABLE_POOL_KAPPA, stable_pool_weights  # noqa: E402
from bayes_basket_target.station_registry import station_coordinates  # noqa: E402

SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
OUT = HERE / "pass1"
CUTOFF_UTC = "2026-09-23T12:00:00Z"
FIRST_EVENT_DATE = "2026-07-20"
LAST_EVENT_DATE = "2026-09-22"
P_FLOOR = 1e-6
SPREAD_BOUNDS = (0.5, 5.0)      # production's clamp in _fit_parameters
DELTA_BOUNDS = (-3.0, 3.0)
ARMS = ("prod", "ll_spread", "ll_both")
# The lambda sweep inside the replay scorer feeds a different study; it does
# not touch the served vector and costs eight extra layer passes a call.
replay.LAMBDA_GRID = ()


# --------------------------------------------------------------- the fit

def _truth_probability(values, actual, params, *, delta=0.0, spread=None):
    """p(settled integer bucket) under ``_apply_parameters``, vectorised.

    Same arithmetic: tercile-resolved bias, spread about the member mean,
    variance-preserving shrink onto the 0.5C kernel, truncation at 4 kernel
    widths either side, renormalised.  Checked against the production
    function before any fit is trusted (``_parity``).
    """

    current = [float(value) for value in values]
    # The production mean, summed in the same order: the tercile cuts are
    # training centres, so a case sitting exactly on a cut flips bin on the
    # last ULP of a differently-summed mean (numpy's pairwise sum did).
    mean = sum(current) / len(current)
    v = np.asarray(current, dtype=float)
    center = mean + oos._resolved_bias_c(mean, params) + delta
    s = float(params["spread_factor"] if spread is None else spread)
    cur = center + s * (v - mean)
    bw = oos.KERNEL_BANDWIDTH_C if len(v) > 1 else max(
        oos.KERNEL_BANDWIDTH_C, float(params.get("train_residual_sigma_c") or params.get("train_rmse_c") or 0.0))
    m = cur.mean()
    std = math.sqrt(((cur - m) ** 2).mean())
    if std > bw:
        cur = m + math.sqrt(1.0 - (bw / std) ** 2) * (cur - m)
    lo = math.floor(cur.min() - 4.0 * bw)
    hi = math.ceil(cur.max() + 4.0 * bw)
    y = int(oos._temperature_bucket(actual))
    if y < lo or y > hi:
        return 0.0
    mass = float((ndtr((y + 0.5 - cur) / bw) - ndtr((y - 0.5 - cur) / bw)).mean())
    total = float((ndtr((hi + 0.5 - cur) / bw) - ndtr((lo - 0.5 - cur) / bw)).mean())
    return mass / total if total > 0 else 0.0


def _decay(train):
    days = [date.fromisoformat(str(c["event_date"])[:10]) for c in train]
    anchor = max(days)
    return np.array([0.5 ** ((anchor - d).days / oos.BIAS_HALF_LIFE_DAYS) for d in days])


def _objective(train, weights, params, delta, spread):
    p = np.array([_truth_probability(c["values"], c["actual_tmax_c"], params, delta=delta, spread=spread)
                  for c in train])
    return float(-(weights * np.log(np.maximum(p, P_FLOOR))).sum() / weights.sum())


def fit_logloss(train, params):
    weights = _decay(train)
    s0 = float(params["spread_factor"])
    spread_only = minimize_scalar(
        lambda s: _objective(train, weights, params, 0.0, s),
        bounds=SPREAD_BOUNDS, method="bounded", options={"xatol": 1e-3},
    )
    s_sp = float(spread_only.x)
    both = minimize(
        lambda x: _objective(train, weights, params, x[0], x[1]),
        x0=np.array([0.0, s_sp]), method="L-BFGS-B",
        bounds=[DELTA_BOUNDS, SPREAD_BOUNDS], options={"maxiter": 60},
    )
    delta, s_both = float(both.x[0]), float(both.x[1])
    return {
        "s_prod": s0, "s_ll_spread": s_sp, "delta_ll_both": delta, "s_ll_both": s_both,
        "nll_prod": _objective(train, weights, params, 0.0, s0),
        "nll_ll_spread": float(spread_only.fun), "nll_ll_both": float(both.fun),
    }


def _shifted(params, delta, spread):
    out = dict(params)
    out["spread_factor"] = spread
    out["bias_c"] = float(params["bias_c"]) + delta
    if params.get("bias_bin_shifts_c") is not None:
        out["bias_bin_shifts_c"] = [float(x) + delta for x in params["bias_bin_shifts_c"]]
    return out


def _parity(train, params):
    for case in train[:4]:
        served = oos._apply_parameters([float(v) for v in case["values"]], params)
        key = oos._temperature_bucket(float(case["actual_tmax_c"]))
        ours = _truth_probability(case["values"], case["actual_tmax_c"], params)
        assert abs(served.get(key, 0.0) - ours) <= 1e-9, (served.get(key, 0.0), ours)


class LLForecasts(replay.Forecasts):
    """The replay's walk-forward forecasts, with the two LogLoss arms beside."""

    checked = 0

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
        _parity(train, params)
        LLForecasts.checked += 1
        fit = fit_logloss(train, params)
        values = [float(v) for v in case["values"]]
        mean = sum(values) / len(values)
        arms = {
            "prod": params,
            "ll_spread": _shifted(params, 0.0, fit["s_ll_spread"]),
            "ll_both": _shifted(params, fit["delta_ll_both"], fit["s_ll_both"]),
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
            "arms": {
                arm: {"probability": oos._apply_parameters(values, p),
                      "calibrated_center_c": oos._corrected_center(mean, p)}
                for arm, p in arms.items()
            },
            "fit": fit,
        }
        # The production arm's keys where the replay reads them.
        row["probability"] = row["arms"]["prod"]["probability"]
        row["calibrated_center_c"] = row["arms"]["prod"]["calibrated_center_c"]
        self.memo[key] = row
        return row


# ------------------------------------------------------------ the capture

def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def capture_station(conn, station, tz):
    """``fusion_blend_historical_20260923/capture.py``'s loop, three arms scored."""

    replay.FIRST_EVENT_DATE = FIRST_EVENT_DATE
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
        key = (scope[0], case["event_date"], str(case.get("archive_lead_bucket") or ""), scope[3], replay._run_init(case))
        return fetched.get(key) or replay._utc(case["aggregator_snapshot_at_utc"])

    forecasts = LLForecasts(cases_by_scope, first_commit, arrival_of)
    try:
        latitude = float(station_coordinates(station)["latitude"])
    except Exception:  # noqa: BLE001
        latitude = None
    arrivals = defaultdict(list)
    for scope, rows in cases_by_scope.items():
        for case in rows:
            if FIRST_EVENT_DATE <= case["event_date"] <= LAST_EVENT_DATE:
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
                tables_memo[hour_key] = oos.p0_gate_tables(conn, station_id=station, tables_as_of_utc=_iso(at))
            tables = tables_memo[hour_key]
            newest = max(packet.values(), key=lambda v: (v[1], v[0]))[2]
            signals = oos._holdout_conditioning_signals(
                conn, station_id=station, holdout_days=[event_date],
                predictions_by_system={s: {event_date: day_rows[s]} for s in pool},
                radiation_shift_sigmas=radiation_shift_sigmas_for_lead(newest[2]),
            )
            cooling = (signals.get(event_date) or {}).get("synoptic_cooling_shift_sigma")
            skill, rule = oos._seat_weights(
                residuals_by_system={s: day_rows[s]["residuals"] for s in pool},
                train_error_scale_by_system={s: day_rows[s]["error_scale"] for s in pool},
                family_by_system={s: family[s] for s in pool}, family_caps=oos.DEFAULT_FAMILY_WEIGHT_CAPS,
                train_n_by_system={s: day_rows[s]["train_n"] for s in pool},
            )
            skill = {s: float(skill.get(s, 0.0)) for s in pool}
            total = sum(skill.values())
            skill = {s: v / total for s, v in skill.items()}
            seat = stable_pool_weights(skill, {s: family[s] for s in pool}, kappa=STABLE_POOL_KAPPA)
            seat = {s: v for s, v in seat.items() if v > 0.0}
            scored = {}
            for arm in ARMS:
                arm_rows = {s: day_rows[s]["arms"][arm] for s in pool}
                result = replay._blend_and_score(
                    weights=seat, day_rows=arm_rows, cooling_shift_sigma=cooling,
                    latitude=latitude, tables=tables, actual=actual,
                )
                scored[arm] = {"mean": result["mean"], "served": result["served"], "vec": result["vec"]}
            out.append({
                "station": station, "event_date": event_date, "tz": tz,
                "at": _iso(at), "truth_known_at": _iso(known), "lead": newest[2],
                "actual": actual, "pool": pool, "w": {s: round(v, 5) for s, v in seat.items()},
                "fit": {s: {k: round(v, 5) for k, v in day_rows[s]["fit"].items()} for s in pool},
                **scored,
            })
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stations", required=True)
    args = parser.parse_args()
    OUT.mkdir(exist_ok=True)
    conn = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=30)
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
