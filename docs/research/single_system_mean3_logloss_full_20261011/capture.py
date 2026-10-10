#!/usr/bin/env python3
"""Four single-system arms in one walk-forward, scored on the market mode +-1
and the market mean +-1.

CUR: production's single-system fit (charter 216, kappa 0.5).  FULL: the same
whole-ladder log-loss refit (production ``_logloss_refit``) served as fitted,
kappa 0.  MODE3 / MEAN3: the binary log loss of the market's modal / mean
bucket and its two neighbours on each training case's own ladder, kappa 0
(PROTOCOL.md; the user's 2026-10-11 revisions: the mode, then both).

The walk-forward, arrivals, seats and serving chain are the 10-09 study's
(``single_system_mode3_logloss_20261009/capture.py``), imported, not copied
where it can be: the hourly market vectors are its ``market_modes/``.  Every
decision stores, for each arm, the served P0 vector on the ladder (charter 224
floor) and each seated system's own calibrated ladder vector, beside the
market vector of that instant.  Read-only; resumable per station.

    python3 capture.py --stations ZUUU,KLAX
"""
from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import math
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from scipy.optimize import minimize

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


M = _load("mode3_capture", ROOT / "docs/research/single_system_mode3_logloss_20261009/capture.py")
C, oos, replay = M.C, M.oos, M.replay
from bayes_basket_target.cross_consensus import bridged_cross_probability  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402
from bayes_basket_target.p1_consensus_lane import mean_bucket  # noqa: E402
from scripts.strict_replay_clock import training_fingerprint  # noqa: E402

OUT = HERE / "pass1"
ARMS = ("cur", "full", "mode3", "mean3")
WINDOWS = ("mode", "mean")
HALF_LIFE = oos.LOGLOSS_REFIT_HALF_LIFE_DAYS
P_FLOOR = oos.LOGLOSS_REFIT_P_FLOOR
FIRST_SCORED, LAST_SCORED = M.FIRST_SCORED, M.LAST_SCORED


def market_window(kind, ladder, found):
    """(window rungs, centre rung) from the 10-09 hourly row ``(mode, q)``.

    ``mode``: production ``market_mode_bucket``'s rung (stored in the row).
    ``mean``: production ``p1_consensus_lane.mean_bucket`` on ``q``
    renormalised over the ladder's rungs, ordinals = ladder order.
    Either way the centre rung and its ladder neighbours."""
    if not found:
        return None
    ordered = list(ladder["ordered"])
    if kind == "mode":
        rung = found[0]
        if rung not in ordered:
            return None
    else:
        q = found[1] or {}
        shared = {b: float(q[b]) for b in ordered if b in q}
        total = sum(shared.values())
        if total <= 0 or len(shared) < len(ordered):
            return None
        rung, _mu = mean_bucket({b: v / total for b, v in shared.items()}, {b: i for i, b in enumerate(ordered)})
    i = ordered.index(rung)
    return ordered[max(0, i - 1): i + 2], rung


def _shifted(moment, shift, spread, rule):
    out = dict(moment)
    out["bias_c"] = float(moment["bias_c"]) + shift
    if moment.get("bias_bin_shifts_c") is not None:
        out["bias_bin_shifts_c"] = [float(v) + shift for v in moment["bias_bin_shifts_c"]]
    out["spread_factor"] = spread
    out["logloss_shift_c"] = shift
    out["fit_rule"] = rule
    return out


class Forecasts(replay.Forecasts):
    def __init__(self, cases_by_scope, first_commit, arrival_of, *, ladder_of, means, stats):
        super().__init__(cases_by_scope, first_commit, arrival_of)
        self.ladder_of, self.means, self.stats = ladder_of, means, stats
        self.parity_checked = 0

    def _window(self, kind, scope, case):
        day = case["event_date"]
        ladder = self.ladder_of(day)
        if ladder is None:
            return None
        found = market_window(kind, ladder, self.means.for_training(day, self.arrival_of(scope, case)))
        if found is None:
            return None
        window, _rung = found
        hit = oos._ladder_bucket(ladder, float(case["actual_tmax_c"]))
        bounds = []
        for b in window:
            lower, upper = ladder["intervals"][b]
            bounds.append((-math.inf if lower is None else float(lower), math.inf if upper is None else float(upper)))
        return window, bounds, [1.0 if b == hit else 0.0 for b in window], ladder

    def _fit_window(self, kind, scope, train, moment, full):
        rows = []
        for case in train:
            w = self._window(kind, scope, case)
            if w is not None:
                rows.append((case, w))
        self.stats[f"{kind}3_train_cases"] += len(train)
        self.stats[f"{kind}3_train_cases_in_window"] += len(rows)
        if len(rows) < replay.MIN_TRAIN:
            self.stats[f"{kind}3_fallback_full"] += 1
            return dict(full, fit_rule=f"{kind}3_fallback_full"), {"n_w": len(rows), "fallback": True}
        cases = [c for c, _w in rows]
        arrays = oos._refit_case_arrays(cases, moment)
        n = len(rows)
        lo = np.zeros((n, 3)); hi = np.zeros((n, 3)); y = np.zeros((n, 3)); valid = np.zeros((n, 3), bool)
        for r, (_c, (window, bounds, flags, _ladder)) in enumerate(rows):
            for k, ((a, b), f) in enumerate(zip(bounds, flags)):
                lo[r, k], hi[r, k], y[r, k], valid[r, k] = a, b, f, True
        anchor = max(date.fromisoformat(str(c["event_date"])[:10]) for c in train)
        weights = np.array([0.5 ** ((anchor - date.fromisoformat(str(c["event_date"])[:10])).days / HALF_LIFE)
                            for c in cases])

        def objective(x):
            p = np.clip(M._interval_probs(arrays, lo, hi, x[0], x[1]), P_FLOOR, 1.0 - P_FLOOR)
            loss = -(y * np.log(p) + (1.0 - y) * np.log(1.0 - p))
            return float((weights * np.where(valid, loss, 0.0).sum(axis=1)).sum() / weights.sum())

        fit = minimize(objective, x0=np.array([0.0, float(moment["spread_factor"])]), method="L-BFGS-B",
                       bounds=[oos.LOGLOSS_REFIT_DELTA_BOUNDS_C, oos.LOGLOSS_REFIT_SPREAD_BOUNDS],
                       options={"maxiter": oos.LOGLOSS_REFIT_MAXITER})
        delta, spread = float(fit.x[0]), float(fit.x[1])
        params = _shifted(moment, delta, spread, f"{kind}3_binary_logloss_full_research")
        if self.parity_checked < 25:
            probe = M._interval_probs(arrays, lo, hi, delta, spread)
            for r in range(min(3, n)):
                case, (window, _b, _f, ladder) = rows[r]
                served = oos._apply_parameters_on_ladder([float(v) for v in case["values"]], params, ladder["intervals"])
                for k, b in enumerate(window):
                    assert abs(served[b] - probe[r, k]) <= 1e-9, (served[b], probe[r, k])
            self.parity_checked += 1
        return params, {"n_w": n, "fallback": False, "delta": delta, "s": spread}

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
            moment = M._moment_parameters(train)
            assert moment["bias_c"] == cur["moment_bias_c"] and moment["spread_factor"] == cur["moment_spread_factor"]
            delta, spread = oos._logloss_refit(train, moment)
            full = _shifted(moment, float(delta), float(spread), "logloss_full_research")
            # CUR is moment + half the same delta: the two arms share the fit.
            assert abs(float(cur["logloss_shift_c"]) - (1.0 - oos.LOGLOSS_REFIT_KAPPA) * float(delta)) <= 1e-9
            mode3, extra_mode = self._fit_window("mode", scope, train, moment, full)
            mean3, extra_mean = self._fit_window("mean", scope, train, moment, full)
            info.update(full_shift=float(delta), full_s=float(spread), moment_s=moment["spread_factor"],
                        mode3_shift=mode3["logloss_shift_c"], mode3_s=mode3["spread_factor"],
                        mode3_fallback=extra_mode["fallback"], mean3_shift=mean3["logloss_shift_c"],
                        mean3_s=mean3["spread_factor"], mean3_fallback=extra_mean["fallback"])
            self.stats["refits"] += 1
        else:
            full = mode3 = mean3 = cur
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
                     for arm, p in (("cur", cur), ("full", full), ("mode3", mode3), ("mean3", mean3))},
            "fit": info,
        }
        self.memo[key] = row
        return row


def capture_station(conn, station, tz, long_lag):
    natives = set(oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS)
    systems = oos._load_systems(conn)
    family = {s: str(systems[s]["independence_family_id"]) for s in natives if s in systems}
    all_cases = oos._load_cases(conn, station_id=station, as_of_utc=M.CUTOFF_UTC)
    cases_by_scope = {
        scope: sorted(rows, key=lambda c: (c["event_date"], c["aggregator_snapshot_at_utc"]))
        for scope, rows in all_cases.items()
        if scope[0] in family and all(c.get("transport_provider_id") != "OPEN_METEO" for c in rows)
    }
    first_commit = replay._first_truth_commit(conn, station)
    fetched = replay._arrivals(conn, station)
    HIST = M.HIST

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
            ladders[day] = oos._station_day_ladder(conn, station_id=station, event_date=day, as_of_utc=M.CUTOFF_UTC)
        return ladders[day]

    stats = Counter()
    means = M.Modes(station)
    forecasts = Forecasts(cases_by_scope, first_commit, arrival_of, ladder_of=ladder_of, means=means, stats=stats)
    try:
        latitude = float(C.station_coordinates(station)["latitude"])
    except Exception:  # noqa: BLE001
        latitude = None
    tables = oos.p0_gate_tables(conn, station_id=station)
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
            at_market = means.at(event_date, at)
            windows = {kind: market_window(kind, ladder, at_market) for kind in WINDOWS}
            if any(w is None for w in windows.values()):
                stats["decision_without_market"] += 1
                continue
            q = at_market[1]
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
            for arm in ARMS:
                probs = {s: day_rows[s]["arms"][arm]["probability"] for s in seat}
                centers = {s: float(day_rows[s]["arms"][arm]["calibrated_center_c"]) for s in seat}
                base, _receipt = bridged_cross_probability(
                    system_probabilities=probs, system_weights=seat, system_centers_c=centers,
                    ordered_keys=ordered, key_centers_c=centers_c)
                vec = oos._holdout_shape_stack(
                    base, grid=ordered, cooling_shift_sigma=cooling, station_latitude=latitude,
                    neighbour_center_c=oos.served_model_center_c(seat, centers), center_width_layer=day_layer,
                    ladder=ladder)
                floored, _lifted = floor_probabilities({k: float(vec.get(k, 0.0)) for k in ordered})
                scored[arm] = {
                    "p": {k: round(floored[k], 7) for k in ordered},
                    "sys": {s: {k: round(float(probs[s].get(k, 0.0)), 7) for k in ordered} for s in seat},
                    "c": oos.served_model_center_c(seat, centers),
                }
            out.append({
                "station": station, "event_date": event_date, "tz": tz, "at": decided,
                "hours": (day_end - at).total_seconds() / 3600, "lead": newest[2], "actual": actual,
                "hit": oos._ladder_bucket(ladder, actual), "ordered": ordered,
                "windows": {kind: {"rungs": w[0], "centre": w[1]} for kind, w in windows.items()},
                "q": {k: round(float(q.get(k, 0.0)), 7) for k in ordered},
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
    long_lag = M.live_lag(conn)
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
        print(f"{station}: {len(rows)} decisions, {time.time() - t0:.0f}s, {stats}", flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
