#!/usr/bin/env python3
"""PROTOCOL.md: the pre-band P0 vector on the market ladder, today's arithmetic.

``consensus_bridge_recheck_20260925/capture_bridge.py``'s ladder replay with
the current bridge only: each update is shaped by the gate's ladder stack,
served through the width layer at lambda 0.75 (the hour's delta and station
scale, tiers off) and recorded BEFORE the band table, with the rung the day
settled in.  score_band.py fits and applies the band tables walk-forward.

    python3 capture_band.py --stations EGLC,KLGA
"""
from __future__ import annotations

import argparse
import dataclasses
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
    "capture_bridge", ROOT / "docs" / "research" / "consensus_bridge_recheck_20260925" / "capture_bridge.py")
B = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(B)
X, C, oos, replay, HIST, CC = B.X, B.C, B.oos, B.replay, B.HIST, B.CC
from bayes_basket_target.served_shape import apply_served_table_layers  # noqa: E402

WIDTH = 0.75
OUT = HERE / "pass1"


def capture_station(conn, station, tz):
    replay.FIRST_EVENT_DATE = X.TRAIN_FLOOR
    natives = set(oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS)
    systems = oos._load_systems(conn)
    family = {s: str(systems[s]["independence_family_id"]) for s in natives if s in systems}
    cases = X._cases(conn, station, family, contaminated=False)
    first_commit = replay._first_truth_commit(conn, station)
    fetched = replay._arrivals(conn, station)
    ladders = {}

    def ladder_for(day):
        if day not in ladders:
            ladders[day] = oos._station_day_ladder(conn, station_id=station, event_date=day,
                                                   as_of_utc=X.CUTOFF_UTC)
        return ladders[day]

    def arrival_of(scope, case):
        init = replay._run_init(case)
        key = (scope[0], case["event_date"], str(case.get("archive_lead_bucket") or ""), scope[3], init)
        arrived = fetched.get(key) or replay._utc(case["aggregator_snapshot_at_utc"])
        if init and scope[0] in X.AI_SYSTEMS:
            run = replay._utc(init)
            if arrived - run > HIST.BACKFILL_LAG:
                if scope[0] == "NOAA_AIGEFS_025":
                    return run + HIST.AIGEFS_ARRIVAL_AFTER_INIT
                twin = fetched.get(("ECMWF_IFS_ENS_OPEN_PF50",) + key[1:])
                return twin if twin is not None and twin - run <= HIST.BACKFILL_LAG else run + HIST.AIFS_FALLBACK_AFTER_INIT
        return arrived

    memo = {}

    def forecast(scope, case, knowable):
        ladder = ladder_for(case["event_date"])
        if ladder is None:
            return None
        start = (date.fromisoformat(case["event_date"]) - timedelta(days=oos.ROLLING_TRAIN_WINDOW_DAYS)).isoformat()
        train = [c for c in cases.get(scope, [])
                 if start <= c["event_date"] < case["event_date"] and arrival_of(scope, c) < knowable
                 and c["event_date"] in first_commit and first_commit[c["event_date"]] < knowable]
        key = (scope, case["event_date"], case["member_set_sha256"], tuple((c["event_date"], c["member_set_sha256"]) for c in train))
        if key in memo:
            return memo[key]
        if len(train) < replay.MIN_TRAIN:
            memo[key] = None
            return None
        p = oos._fit_parameters(train)
        if float(p.get("reliability_weight", 1.0)) != 1.0:
            raise RuntimeError("climate blend in play")
        values = [float(v) for v in case["values"]]
        memo[key] = {
            "probability": oos._apply_parameters_on_ladder(values, p, ladder["intervals"]),
            "calibrated_center_c": oos._corrected_center(sum(values) / len(values), p),
            "actual_tmax_c": float(case["actual_tmax_c"]),
            "error_scale": float(p.get("train_residual_sigma_c") or p["train_rmse_c"]),
            "train_n": len(train),
            "residuals": {c["event_date"]: float(c["actual_tmax_c"]) - sum(float(v) for v in c["values"]) / len(c["values"]) for c in train},
        }
        return memo[key]

    try:
        latitude = float(C.station_coordinates(station)["latitude"])
    except Exception:  # noqa: BLE001
        latitude = None
    arrivals = defaultdict(list)
    for scope, rows in cases.items():
        for case in rows:
            if X.SCORED(case["event_date"]):
                arrivals[case["event_date"]].append((arrival_of(scope, case), replay._run_init(case), scope[0], scope, case))
    zone = ZoneInfo(tz)
    tables_memo, out = {}, []
    for event_date in sorted(arrivals):
        if event_date not in first_commit:
            continue
        ladder = ladder_for(event_date)
        if ladder is None:
            continue
        keys, key_centers, open_edges = ladder["ordered"], ladder["centers_c"], ladder["open_edge_bounds_c"]
        day_end = datetime.fromisoformat(event_date).replace(tzinfo=zone) + timedelta(days=1)
        events = sorted(arrivals[event_date], key=lambda e: (e[0], e[1], e[2]))
        packet, previous, day_rows_out = {}, None, []
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
            day_rows = {s: r for s, (_a, _i, s_scope, s_case) in packet.items()
                        if (r := forecast(s_scope, s_case, at)) is not None}
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
                radiation_shift_sigmas=C.radiation_shift_sigmas_for_lead(newest[2]))
            cooling = (signals.get(event_date) or {}).get("synoptic_cooling_shift_sigma")
            skill, _rule = oos._seat_weights(
                residuals_by_system={s: day_rows[s]["residuals"] for s in pool},
                train_error_scale_by_system={s: day_rows[s]["error_scale"] for s in pool},
                family_by_system={s: family[s] for s in pool}, family_caps=oos.DEFAULT_FAMILY_WEIGHT_CAPS,
                train_n_by_system={s: day_rows[s]["train_n"] for s in pool})
            skill = {s: float(skill.get(s, 0.0)) for s in pool}
            total = sum(skill.values())
            seat = C.stable_pool_weights({s: v / total for s, v in skill.items()}, {s: family[s] for s in pool}, kappa=C.STABLE_POOL_KAPPA)
            seat = {s: v for s, v in seat.items() if v > 0.0}
            seated = sorted(seat)
            centers = {s: float(day_rows[s]["calibrated_center_c"]) for s in seated}
            blended, _receipt = CC.bridged_cross_probability(
                system_probabilities={s: day_rows[s]["probability"] for s in seated}, system_weights=seat,
                system_centers_c=centers, ordered_keys=keys, key_centers_c=key_centers)
            pre = oos._ladder_shape_stack(
                blended, ladder=ladder, cooling_shift_sigma=cooling, station_latitude=latitude,
                neighbour_center_c=sum(seat[s] * centers[s] for s in seated),
                center_width_layer=None, band_layer=None, skip=None)
            layer = dataclasses.replace(tables["center_width_layer"] or replay.CenterWidthLayer(),
                                        width_lambda=WIDTH, width_tier_lambdas=None, width_tier_boundaries=None)
            vec, _cw, _band = apply_served_table_layers(
                pre, ordered_keys=keys, key_centers_c=key_centers, center_width=layer, band=None,
                open_edge_bounds_c=open_edges)
            day_rows_out.append({"at": C._iso(at), "lead": newest[2], "pool": len(seated),
                                 "vec": [round(float(vec[key]), 9) for key in keys]})
        if day_rows_out:
            out.append({"station": station, "event_date": event_date, "keys": list(keys),
                        "hit": keys.index(oos._ladder_bucket(ladder, float(actual))),
                        "actual": actual, "updates": day_rows_out})
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stations", required=True)
    args = parser.parse_args()
    OUT.mkdir(exist_ok=True)
    conn = sqlite3.connect(f"file:{C.SSOT}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA query_only=ON")
    zones = dict(conn.execute("SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding").fetchall())
    for station in [s.strip().upper() for s in args.stations.split(",") if s.strip()]:
        path = OUT / f"{station}.jsonl.gz"
        if path.exists():
            continue
        t0 = time.time()
        rows = capture_station(conn, station, zones[station])
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt") as handle:
            for row in rows:
                handle.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n")
        tmp.rename(path)
        print(f"{station}: {len(rows)} station-days, {sum(len(r['updates']) for r in rows)} updates, {time.time() - t0:.0f}s", flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
