#!/usr/bin/env python3
"""Charter 199 x charter 216 on six families: one day-ahead root per station-day.

Per station-day, the replay's packet at the last update in the 24 hours
before the local midnight (the root the fusion training table uses), with
each system's walk-forward forecast under two single-system fits:

  prod   production's moment fit (``oos._fit_parameters``)
  reg    charter 216's regularised bucket log-loss fit: half-life 15 days,
         half-way back to production -- ``single_system_logloss_reg_20260924/
         capture_reg.py``'s own ``_fit_both`` and ``C._shifted`` (imported)

Seat weights are production's (moment-fit residuals) for both, as in the
charter 216 study.  AIFS ENS / AIGEFS backfilled rows get modelled arrivals
(``fusion_blend_historical_20260923/capture.py``'s rule).  Each root is
scored with the replay's ``_blend_and_score`` under the tables active that
hour; tables pickled for pass 2.  Read-only.
"""
from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import pickle
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REG = _load("capture_reg", ROOT / "docs/research/single_system_logloss_reg_20260924/capture_reg.py")
CAP = _load("capture_fusion", ROOT / "docs/research/fusion_blend_historical_20260923/capture.py")
C, oos, replay = REG.C, REG.oos, REG.replay
from bayes_basket_target.cross_consensus import radiation_shift_sigma_for_lead  # noqa: E402
from bayes_basket_target.stable_pool import STABLE_POOL_KAPPA, stable_pool_weights  # noqa: E402
from bayes_basket_target.station_registry import station_coordinates  # noqa: E402

SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
OUT = HERE / "pass1"
CUTOFF_UTC = "2026-09-24T12:00:00Z"
FIRST_EVENT_DATE = "2026-07-20"
LAST_EVENT_DATE = "2026-09-22"
ARMS = ("prod", "reg")


class TwoArmForecasts(replay.Forecasts):
    """capture_reg.RegForecasts without the unregularised arm."""

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
        d15, s15 = REG._fit_both(train, params, REG.REG_HALF_LIFE_DAYS, s_prod)
        d_reg = REG.REG_KAPPA * 0.0 + (1 - REG.REG_KAPPA) * d15
        s_reg = REG.REG_KAPPA * s_prod + (1 - REG.REG_KAPPA) * s15
        values = [float(v) for v in case["values"]]
        mean = sum(values) / len(values)
        arms = {"prod": params, "reg": C._shifted(params, d_reg, s_reg)}
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
            "fit": {"s_prod": s_prod, "delta_reg": d_reg, "s_reg": s_reg},
        }
        self.memo[key] = row
        return row


def capture_station(conn, station, tz):
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
        init = replay._run_init(case)
        key = (scope[0], case["event_date"], str(case.get("archive_lead_bucket") or ""), scope[3], init)
        arrived = fetched.get(key) or replay._utc(case["aggregator_snapshot_at_utc"])
        if init and scope[0] in ("ECMWF_AIFS_ENS_GLOBAL", "NOAA_AIGEFS_025"):
            run = replay._utc(init)
            if arrived - run > CAP.BACKFILL_LAG:
                if scope[0] == "NOAA_AIGEFS_025":
                    return run + CAP.AIGEFS_ARRIVAL_AFTER_INIT
                twin = fetched.get(("ECMWF_IFS_ENS_OPEN_PF50",) + key[1:])
                return twin if twin is not None and twin - run <= CAP.BACKFILL_LAG else run + CAP.AIFS_FALLBACK_AFTER_INIT
        return arrived

    forecasts = TwoArmForecasts(cases_by_scope, first_commit, arrival_of)
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
        midnight = datetime.fromisoformat(event_date).replace(tzinfo=zone)
        known = max(first_commit[event_date], midnight + timedelta(days=1))
        events = sorted(arrivals[event_date], key=lambda e: (e[0], e[1], e[2]))
        packet = {}
        root_at = None
        for at, run_init, system, scope, case in events:
            if at >= midnight:
                break
            current = packet.get(system)
            if current is None or (run_init, at) >= (current[1], current[0]):
                packet[system] = (at, run_init, scope, case)
            root_at = at
        if root_at is None or root_at < midnight - timedelta(days=1):
            continue
        day_rows = {}
        for s, (_at, _init, s_scope, s_case) in packet.items():
            row = forecasts.forecast(s_scope, s_case, root_at)
            if row is not None:
                day_rows[s] = row
        pool = sorted(day_rows)
        if len({family[s] for s in pool}) < 2:
            continue
        actual = day_rows[pool[0]]["actual_tmax_c"]
        hour_key = root_at.strftime("%Y-%m-%dT%H")
        if hour_key not in tables_memo:
            tables_memo[hour_key] = oos.p0_gate_tables(conn, station_id=station, tables_as_of_utc=CAP._iso(root_at))
        tables = tables_memo[hour_key]
        newest = max(packet.values(), key=lambda v: (v[1], v[0]))[2]
        cooling_by_arm = {}
        seat = None
        for arm in ARMS:
            arm_rows = {s: {**day_rows[s], **day_rows[s]["arms"][arm]} for s in pool}
            signals = oos._holdout_conditioning_signals(
                conn, station_id=station, holdout_days=[event_date],
                predictions_by_system={s: {event_date: arm_rows[s]} for s in pool},
                radiation_shift_sigma=radiation_shift_sigma_for_lead(newest[2]),
            )
            cooling_by_arm[arm] = (signals.get(event_date) or {}).get("synoptic_cooling_shift_sigma")
        skill, _rule = oos._seat_weights(
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
        record = {
            "station": station, "event_date": event_date, "tz": tz,
            "at": CAP._iso(root_at), "truth_known_at": CAP._iso(known), "lead": newest[2],
            "actual": actual, "pool": pool, "family": {s: family[s] for s in pool},
            "scopes": {s: [packet[s][2][2], packet[s][2][3]] for s in pool},
            "latitude": latitude, "hour_key": hour_key,
            "fit": {s: {k: round(v, 5) for k, v in day_rows[s]["fit"].items()} for s in pool},
            "arms": {},
        }
        for arm in ARMS:
            arm_rows = {s: day_rows[s]["arms"][arm] for s in pool}
            scored = replay._blend_and_score(
                weights=seat, day_rows=arm_rows, cooling_shift_sigma=cooling_by_arm[arm],
                latitude=latitude, tables=tables, actual=actual,
            )
            record["arms"][arm] = {
                "cooling": cooling_by_arm[arm],
                "day_rows": {s: {"c": round(float(arm_rows[s]["calibrated_center_c"]), 6),
                                 "p": {k: round(float(v), 8) for k, v in arm_rows[s]["probability"].items() if v > 0.0}}
                             for s in pool},
                "seat": {"w": scored["w"], "mean": scored["mean"], "served": scored["served"], "vec": scored["vec"]},
            }
        out.append(record)
    return out, tables_memo


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget-seconds", type=float, default=540.0)
    parser.add_argument("--stations", nargs="*", default=None)
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    zones = dict(conn.execute(
        "SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding"
    ).fetchall())
    from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: PLC0415
    stations = args.stations or sorted(str(s) for s in coordinate_registry_manifest()["station_ids"])
    started = time.time()
    done = 0
    for station in stations:
        target = OUT / f"{station}.jsonl.gz"
        if target.exists():
            continue
        if time.time() - started > args.budget_seconds:
            break
        t0 = time.time()
        rows, tables = capture_station(conn, station, zones[station])
        tmp = target.with_suffix(".tmp")
        with gzip.open(tmp, "wt") as handle:
            for row in rows:
                handle.write(json.dumps(row, separators=(",", ":")) + "\n")
        with open(OUT / f"{station}.tables.pkl", "wb") as handle:
            pickle.dump(tables, handle)
        tmp.replace(target)
        done += 1
        print(f"{station}: {len(rows)} roots, {time.time() - t0:.0f}s", flush=True)
    remaining = sum(1 for s in stations if not (OUT / f"{s}.jsonl.gz").exists())
    print(f"done {done}, remaining {remaining}, {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
