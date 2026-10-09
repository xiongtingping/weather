#!/usr/bin/env python3
"""Two single-system arms in one walk-forward, scored on the market ladder.

CUR: production's single-system fit (charter 216: moments, then the decayed
whole-ladder log loss of a centre shift and the spread factor, half-life 15,
half-way back to the moments).  M3: the same moments and the same leash, the
objective swapped for the binary log loss of the market's modal bucket and
its two neighbours on each training case's own ladder (PROTOCOL.md).

The walk-forward is ``single_system_logloss_reg_20260924/capture_reg.py``'s
(modelled arrivals, production ``_fit_parameters``, stable-pool seats); every
update is served through production's own ladder functions -- the per-system
ladder vector, the consensus bridge, the shape stack with the width slice /
F far tail / peak ceiling resolved at the decision instant -- and scored with
``_ladder_metrics`` (charter 224 floor).  Read-only; resumable per station.

    python3 capture.py --stations KLAX,EGLC
"""
from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import math
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from scipy.optimize import minimize
from scipy.special import ndtr

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


C = _load("capture_ll", ROOT / "docs/research/single_system_logloss_20260924/capture_ll.py")
HIST = _load("hist_capture", ROOT / "docs/research/fusion_blend_historical_20260923/capture.py")
oos, replay = C.oos, C.replay
from bayes_basket_target.cross_consensus import bridged_cross_probability  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402
from scripts.strict_replay_clock import training_fingerprint  # noqa: E402

if oos.SINGLE_SYSTEM_FIT_RULE != oos.SINGLE_SYSTEM_FIT_RULE_LOGLOSS:
    sys.exit("unset PM_SINGLE_SYSTEM_FIT_RULE: CUR must be production's rule")

CUTOFF_UTC = "2026-10-09T12:00:00Z"
FIRST_SCORED, LAST_SCORED = "2026-09-01", "2026-10-07"
OUT = HERE / "pass1"
MODES = HERE / "market_modes"
KAPPA = oos.LOGLOSS_REFIT_KAPPA
HALF_LIFE = oos.LOGLOSS_REFIT_HALF_LIFE_DAYS
P_FLOOR = oos.LOGLOSS_REFIT_P_FLOOR
FIT_RULE_M3 = "mode3_binary_logloss_shrunk_research"
replay.LAMBDA_GRID = ()
replay.FIRST_EVENT_DATE = FIRST_SCORED


def _utc(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(timezone.utc)


# ------------------------------------------------------------------ market modes

class Modes:
    """Hourly market mode per station-day (build_market_modes.py)."""

    def __init__(self, station):
        path = MODES / f"{station}.json.gz"
        raw = json.loads(gzip.open(path, "rt").read()) if path.exists() else {}
        self.days = {day: [(_utc(h), mode, q) for h, mode, q, _seen in rows] for day, rows in raw.items()}

    def at(self, day, moment):
        """The mode as of the top of the hour at or before ``moment`` (None if the
        book was incomplete then or the day was not listed yet)."""
        rows = self.days.get(day) or []
        found = None
        for hour, mode, q in rows:
            if hour <= moment:
                found = (mode, q)
            else:
                break
        return found if found and found[0] else None

    def for_training(self, day, moment):
        """At the run's arrival; before the listing, the first complete book after it."""
        rows = self.days.get(day) or []
        if not rows:
            return None
        if moment < rows[0][0]:
            return next(((mode, q) for _h, mode, q in rows if mode), None)
        return self.at(day, moment)


# ------------------------------------------------------------------ the M3 objective

def _interval_probs(arrays, lo, hi, delta, spread):
    """Mass of each case's window rungs [lo, hi) under the dressed members --
    ``_truth_bucket_probabilities``' kernel and shrink, read on ladder
    intervals the way ``_interval_probability`` integrates them (open edges
    take their whole tails, no truncation)."""
    mask, counts = arrays["mask"], arrays["counts"]
    if arrays["kernel_rule"] == oos.SINGLE_SYSTEM_KERNEL_RULE_DRESS and float(spread) > 1.0:
        bandwidth = np.maximum(arrays["floors"], math.sqrt(float(spread) ** 2 - 1.0) * arrays["spreads"])
    else:
        bandwidth = arrays["floors"]
    dressed = (arrays["centers"] + float(delta))[:, None] + float(spread) * arrays["deviations"]
    middle = np.where(mask, dressed, 0.0).sum(axis=1) / counts
    variance = np.where(mask, (dressed - middle[:, None]) ** 2, 0.0).sum(axis=1) / counts
    std = np.sqrt(variance)
    shrink = np.where(std > bandwidth,
                      np.sqrt(np.clip(1.0 - (bandwidth / np.where(std > 0, std, 1.0)) ** 2, 0.0, 1.0)), 1.0)
    dressed = middle[:, None] + shrink[:, None] * (dressed - middle[:, None])
    out = np.zeros(lo.shape)
    for k in range(lo.shape[1]):
        upper = ndtr((hi[:, k][:, None] - dressed) / bandwidth[:, None])
        lower = ndtr((lo[:, k][:, None] - dressed) / bandwidth[:, None])
        out[:, k] = np.where(mask, upper - lower, 0.0).sum(axis=1) / counts
    return out


def _moment_parameters(train):
    oos.SINGLE_SYSTEM_FIT_RULE = oos.SINGLE_SYSTEM_FIT_RULE_MOMENT
    try:
        return oos._fit_parameters(train)
    finally:
        oos.SINGLE_SYSTEM_FIT_RULE = oos.SINGLE_SYSTEM_FIT_RULE_LOGLOSS


def _shifted(moment, shift, spread):
    out = dict(moment)
    out["bias_c"] = float(moment["bias_c"]) + shift
    if moment.get("bias_bin_shifts_c") is not None:
        out["bias_bin_shifts_c"] = [float(v) + shift for v in moment["bias_bin_shifts_c"]]
    out["spread_factor"] = spread
    out["logloss_shift_c"] = shift
    out["fit_rule"] = FIT_RULE_M3
    return out


# ------------------------------------------------------------------ forecasts

class M3Forecasts(replay.Forecasts):
    def __init__(self, cases_by_scope, first_commit, arrival_of, *, ladder_of, modes, stats):
        super().__init__(cases_by_scope, first_commit, arrival_of)
        self.ladder_of, self.modes, self.stats = ladder_of, modes, stats
        self.parity_checked = 0

    def _window(self, scope, case):
        day = case["event_date"]
        ladder = self.ladder_of(day)
        if ladder is None:
            return None
        found = self.modes.for_training(day, self.arrival_of(scope, case))
        if found is None or found[0] not in ladder["ordered"]:
            return None
        ordered = ladder["ordered"]
        i = ordered.index(found[0])
        window = ordered[max(0, i - 1): i + 2]
        hit = oos._ladder_bucket(ladder, float(case["actual_tmax_c"]))
        bounds = []
        for b in window:
            lower, upper = ladder["intervals"][b]
            bounds.append((-math.inf if lower is None else float(lower), math.inf if upper is None else float(upper)))
        return window, bounds, [1.0 if b == hit else 0.0 for b in window], ladder

    def _fit_m3(self, scope, train, moment):
        rows = []
        for case in train:
            w = self._window(scope, case)
            if w is not None:
                rows.append((case, w))
        self.stats["train_cases"] += len(train)
        self.stats["train_cases_in_window"] += len(rows)
        if len(rows) < replay.MIN_TRAIN:
            self.stats["m3_fallback_moments"] += 1
            return _shifted(moment, 0.0, float(moment["spread_factor"])), {"n_m3": len(rows), "fallback": True}
        cases = [c for c, _w in rows]
        arrays = oos._refit_case_arrays(cases, moment)
        n = len(rows)
        lo = np.full((n, 3), np.nan); hi = np.full((n, 3), np.nan); y = np.zeros((n, 3)); valid = np.zeros((n, 3), bool)
        for r, (_c, (window, bounds, flags, _ladder)) in enumerate(rows):
            for k, ((a, b), f) in enumerate(zip(bounds, flags)):
                lo[r, k], hi[r, k], y[r, k], valid[r, k] = a, b, f, True
        lo = np.where(valid, lo, 0.0); hi = np.where(valid, hi, 0.0)
        anchor = max(date.fromisoformat(str(c["event_date"])[:10]) for c in train)
        weights = np.array([0.5 ** ((anchor - date.fromisoformat(str(c["event_date"])[:10])).days / HALF_LIFE)
                            for c in cases])

        def objective(x):
            p = np.clip(_interval_probs(arrays, lo, hi, x[0], x[1]), P_FLOOR, 1.0 - P_FLOOR)
            loss = -(y * np.log(p) + (1.0 - y) * np.log(1.0 - p))
            return float((weights * np.where(valid, loss, 0.0).sum(axis=1)).sum() / weights.sum())

        fit = minimize(objective, x0=np.array([0.0, float(moment["spread_factor"])]), method="L-BFGS-B",
                       bounds=[oos.LOGLOSS_REFIT_DELTA_BOUNDS_C, oos.LOGLOSS_REFIT_SPREAD_BOUNDS],
                       options={"maxiter": oos.LOGLOSS_REFIT_MAXITER})
        delta, spread_fit = float(fit.x[0]), float(fit.x[1])
        shift = (1.0 - KAPPA) * delta
        spread = KAPPA * float(moment["spread_factor"]) + (1.0 - KAPPA) * spread_fit
        params = _shifted(moment, shift, spread)
        if self.parity_checked < 25:
            # the vectorised window mass equals production's ladder serving function
            probe = _interval_probs(arrays, lo, hi, shift, spread)
            for r in range(min(3, n)):
                case, (window, _b, _f, ladder) = rows[r]
                served = oos._apply_parameters_on_ladder([float(v) for v in case["values"]], params, ladder["intervals"])
                for k, b in enumerate(window):
                    assert abs(served[b] - probe[r, k]) <= 1e-9, (served[b], probe[r, k])
            self.parity_checked += 1
        return params, {"n_m3": n, "fallback": False, "delta": delta, "s_fit": spread_fit}

    def forecast(self, scope, case, knowable):
        train = self.train(scope, case["event_date"], knowable)
        key = (scope, case["event_date"], case["member_set_sha256"], training_fingerprint(train))
        if key in self.memo:
            return self.memo[key]
        ladder = self.ladder_of(case["event_date"])
        if len(train) < replay.MIN_TRAIN or ladder is None:
            self.memo[key] = None
            return None
        cur = oos._fit_parameters(train)
        info = {"rule": cur.get("fit_rule"), "cur_shift": cur.get("logloss_shift_c"), "cur_s": cur["spread_factor"]}
        if cur.get("fit_rule") == oos.SINGLE_SYSTEM_FIT_RULE_LOGLOSS:
            moment = _moment_parameters(train)
            assert moment["bias_c"] == cur["moment_bias_c"] and moment["spread_factor"] == cur["moment_spread_factor"]
            m3, extra = self._fit_m3(scope, train, moment)
            info.update(extra, m3_shift=m3["logloss_shift_c"], m3_s=m3["spread_factor"], moment_s=moment["spread_factor"])
            self.stats["refits"] += 1
        else:
            m3 = cur
            self.stats["not_refitted"] += 1
        values = [float(v) for v in case["values"]]
        mean = sum(values) / len(values)
        row = {
            "event_date": case["event_date"],
            "actual_tmax_c": float(case["actual_tmax_c"]),
            "error_scale": float(cur.get("train_residual_sigma_c") or cur["train_rmse_c"]),
            "train_n": len(train),
            "residuals": {c["event_date"]: float(c["actual_tmax_c"]) - sum(float(v) for v in c["values"]) / len(c["values"])
                          for c in train},
            "arms": {arm: {"probability": oos._apply_parameters_on_ladder(values, p, ladder["intervals"]),
                           "calibrated_center_c": oos._corrected_center(mean, p)}
                     for arm, p in (("cur", cur), ("m3", m3))},
            "fit": info,
        }
        self.memo[key] = row
        return row


# ------------------------------------------------------------------ arrivals (capture_long's clock)

def live_lag(conn):
    rows = conn.execute(
        "SELECT ensemble_system_id, run_cycle, native_run_init_utc, MIN(archive_query_at_utc)"
        " FROM calibration_native_ensemble_historical_member_run"
        " WHERE ensemble_system_id IN ('ECMWF_IFS_ENS_OPEN_PF50','NOAA_GEFS_05') AND target_local_date BETWEEN '2026-08-15' AND '2026-09-30'"
        " AND qc_status='ACCEPTED' GROUP BY 1, 2, 3, station_id, target_local_date, archive_lead_bucket").fetchall()
    lags = {}
    for system, cycle, init, got in rows:
        if not init or not got:
            continue
        h = (_utc(got) - _utc(init)).total_seconds() / 3600
        if 0 < h < 24:
            lags.setdefault(system, {}).setdefault(cycle, []).append(h)
            lags[system].setdefault("*", []).append(h)
    return {s: {c: timedelta(hours=statistics.median(v)) for c, v in by.items()} for s, by in lags.items()}


def capture_station(conn, station, tz, long_lag):
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
        if init and scope[0] in long_lag:
            run = replay._utc(init)
            if arrived - run > HIST.BACKFILL_LAG:
                return run + long_lag[scope[0]].get(scope[3], long_lag[scope[0]]["*"])
        if init and scope[0] in ("ECMWF_AIFS_ENS_GLOBAL", "NOAA_AIGEFS_025"):
            run = replay._utc(init)
            if arrived - run > HIST.BACKFILL_LAG:
                if scope[0] == "NOAA_AIGEFS_025":
                    return run + HIST.AIGEFS_ARRIVAL_AFTER_INIT
                twin = fetched.get(("ECMWF_IFS_ENS_OPEN_PF50",) + key[1:])
                return twin if twin is not None and twin - run <= HIST.BACKFILL_LAG else run + HIST.AIFS_FALLBACK_AFTER_INIT
        return arrived

    ladders = {}

    def ladder_of(day):
        if day not in ladders:
            ladders[day] = oos._station_day_ladder(conn, station_id=station, event_date=day, as_of_utc=CUTOFF_UTC)
        return ladders[day]

    stats = Counter()
    modes = Modes(station)
    forecasts = M3Forecasts(cases_by_scope, first_commit, arrival_of, ladder_of=ladder_of, modes=modes, stats=stats)
    try:
        latitude = float(C.station_coordinates(station)["latitude"])
    except Exception:  # noqa: BLE001
        latitude = None
    tables = oos.p0_gate_tables(conn, station_id=station)     # the tables serving now (both arms)
    layer, slices = tables["center_width_layer"], tables.get("center_width_slices")
    arrivals = defaultdict(list)
    for scope, rows in cases_by_scope.items():
        for case in rows:
            if FIRST_SCORED <= case["event_date"] <= LAST_SCORED:
                arrivals[case["event_date"]].append((arrival_of(scope, case), replay._run_init(case), scope[0], scope, case))
    zone = ZoneInfo(tz)
    out = []
    for event_date in sorted(arrivals):
        if event_date not in first_commit or ladder_of(event_date) is None:
            stats["scored_day_skipped"] += 1
            continue
        ladder = ladder_of(event_date)
        ordered, centers_c = list(ladder["ordered"]), list(ladder["centers_c"])
        day_end = datetime.fromisoformat(event_date).replace(tzinfo=zone) + timedelta(days=1)
        events = sorted(arrivals[event_date], key=lambda e: (e[0], e[1], e[2]))
        packet, previous = {}, None
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
            if at >= day_end:
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
            newest = max(packet.values(), key=lambda v: (v[1], v[0]))[2]
            decided = C._iso(at)
            signals = oos._holdout_conditioning_signals(
                conn, station_id=station, holdout_days=[event_date],
                predictions_by_system={s: {event_date: {"calibrated_center_c": day_rows[s]["arms"]["cur"]["calibrated_center_c"],
                                                        "decision_at_utc": decided}} for s in pool},
                radiation_shift_sigmas=C.radiation_shift_sigmas_for_lead(newest[2]),
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
            day_layer = oos._day_center_width_layer(
                layer, slices, predictions_by_system={s: {event_date: {"decision_at_utc": decided}} for s in seat},
                weights=seat, day=event_date, settlement_timezone=tz, f_ladder=oos.is_f_ladder(ordered))
            scored = {}
            for arm in ("cur", "m3"):
                probs = {s: day_rows[s]["arms"][arm]["probability"] for s in seat}
                centers = {s: float(day_rows[s]["arms"][arm]["calibrated_center_c"]) for s in seat}
                base, _receipt = bridged_cross_probability(
                    system_probabilities=probs, system_weights=seat, system_centers_c=centers,
                    ordered_keys=ordered, key_centers_c=centers_c)
                vec = oos._holdout_shape_stack(
                    base, grid=ordered, cooling_shift_sigma=cooling, station_latitude=latitude,
                    neighbour_center_c=oos.served_model_center_c(seat, centers), center_width_layer=day_layer,
                    ladder=ladder)
                metrics = oos._ladder_metrics(vec, actual, ladder)
                floored, _lifted = floor_probabilities({k: float(vec.get(k, 0.0)) for k in ordered})
                scored[arm] = {"m": metrics, "p": {k: round(floored[k], 7) for k in ordered},
                               "c": oos.served_model_center_c(seat, centers)}
            out.append({
                "station": station, "event_date": event_date, "tz": tz, "at": decided,
                "hours": (day_end - at).total_seconds() / 3600, "lead": newest[2], "actual": actual,
                "hit": oos._ladder_bucket(ladder, actual), "ordered": ordered,
                "seat": {s: round(v, 5) for s, v in seat.items()},
                "fit": {s: {k: (round(v, 5) if isinstance(v, float) else v) for k, v in day_rows[s]["fit"].items()} for s in seat},
                **scored,
            })
    return out, dict(stats)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stations", required=True)
    args = parser.parse_args()
    OUT.mkdir(exist_ok=True)
    conn = sqlite3.connect(f"file:{C.SSOT}?mode=ro", uri=True, timeout=30)
    zones = dict(conn.execute(
        "SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding").fetchall())
    long_lag = live_lag(conn)
    for station in [s.strip().upper() for s in args.stations.split(",") if s.strip()]:
        path = OUT / f"{station}.jsonl.gz"
        if path.exists():
            continue
        t0 = time.time()
        rows, stats = capture_station(conn, station, zones[station], long_lag)
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        tmp.rename(path)
        (OUT / f"{station}.stats.json").write_text(json.dumps(stats))
        print(f"{station}: {len(rows)} updates, {time.time() - t0:.0f}s, {stats}", flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
