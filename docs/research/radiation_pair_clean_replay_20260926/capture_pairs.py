#!/usr/bin/env python3
"""PROTOCOL.md: the band study's current-arithmetic replay, with the radiation shift applied.

``band_layer_recheck_20260925/capture_band.py``'s station loop, unchanged up to
the blend, then per update and per coefficient pair: the day's radiation as of
the update (production's ``resolve_radiation_as_of``), the centred two-sided
shift in sigmas (production's clearness form, absolute form as fallback, the
same glue as ``_holdout_conditioning_signals``), production's
``_ladder_shape_stack`` and the width layer at 0.75.  The ``cur`` pair's shift
is asserted equal to ``_holdout_conditioning_signals`` fed the update time as
its cutoff; the ``none`` arm is the original capture's vector (checked in
score_pairs.py).

    python3 capture_pairs.py --stations ZUUU,KLGA
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
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
_spec = importlib.util.spec_from_file_location(
    "capture_band", ROOT / "docs" / "research" / "band_layer_recheck_20260925" / "capture_band.py")
CB = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CB)
X, C, oos, replay, HIST, CC = CB.X, CB.C, CB.oos, CB.replay, CB.HIST, CB.CC
from bayes_basket_target.served_shape import apply_served_table_layers  # noqa: E402
from bayes_basket_target.forecast_physics_asof import resolve_radiation_as_of  # noqa: E402
from bayes_basket_target.cross_consensus import (  # noqa: E402
    forecast_clearness_shift_sigma, forecast_radiation_shift_sigma)

WIDTH = CB.WIDTH
FIRST_DAY = "2026-08-16"
ARMS = {
    "none": (0.0, 0.0), "sym07": (0.07, 0.07), "p12_04": (0.12, 0.04), "cur": (0.18, 0.06),
    "p18_12": (0.18, 0.12), "sym18": (0.18, 0.18), "sym12": (0.12, 0.12), "p24_08": (0.24, 0.08),
}
assert C.radiation_shift_sigmas_for_lead("h24_48") == ARMS["cur"], C.radiation_shift_sigmas_for_lead("h24_48")
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
        start = (datetime.fromisoformat(case["event_date"]) - timedelta(days=oos.ROLLING_TRAIN_WINDOW_DAYS)).date().isoformat()
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
    # The same rows ``_holdout_conditioning_signals`` reads, once per station.
    radiation_by_day = {
        str(d): float(v) for d, v in conn.execute(
            "SELECT local_date,shortwave_radiation_sum FROM calibration_station_forecast_physics "
            "WHERE station_id=? AND reference_only=1 AND shortwave_radiation_sum IS NOT NULL", (station,))}
    asof_memo, check_memo = {}, {}

    def radiation_terms(day, at_iso):
        if (day, at_iso) not in asof_memo:
            asof_memo[(day, at_iso)] = resolve_radiation_as_of(conn, station_id=station, local_date=day, cutoff_utc=at_iso)
        value, asof = asof_memo[(day, at_iso)]
        out = {}
        for arm, pair in ARMS.items():
            shift, receipt = forecast_clearness_shift_sigma(
                shift_sigma_per_unit=pair[0], cold_shift_sigma_per_unit=pair[1], radiation_sum=value,
                event_date=day, latitude_deg=latitude,
                station_history=[(o, v) for o, v in radiation_by_day.items() if o < day])
            if shift is None:
                shift, receipt = forecast_radiation_shift_sigma(
                    shift_sigma_per_unit=pair[0], cold_shift_sigma_per_unit=pair[1], radiation_sum=value,
                    station_history=[v for o, v in radiation_by_day.items() if o != day])
            out[arm] = (shift, receipt)
        return out, str(asof.get("state"))

    arrivals = defaultdict(list)
    for scope, rows in cases.items():
        for case in rows:
            if X.SCORED(case["event_date"]) and case["event_date"] >= FIRST_DAY:
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
            at_iso = C._iso(at)
            terms, state = radiation_terms(event_date, at_iso)
            # Production's glue, the update time as the cutoff: must agree with ours for the served pair.
            check_key = (event_date, at_iso)
            if check_key not in check_memo:
                signals = oos._holdout_conditioning_signals(
                    conn, station_id=station, holdout_days=[event_date],
                    predictions_by_system={s: {event_date: {"calibrated_center_c": day_rows[s]["calibrated_center_c"],
                                                            "decision_at_utc": at_iso}} for s in pool},
                    radiation_shift_sigmas=ARMS["cur"])
                check_memo[check_key] = (signals.get(event_date) or {}).get("synoptic_cooling_shift_sigma")
            ours = terms["cur"][0]
            theirs = check_memo[check_key]
            if (ours is None) != (theirs is None) or (ours is not None and abs(ours - theirs) > 1e-12):
                raise RuntimeError(f"glue mismatch {station} {event_date} {at_iso}: {ours} vs {theirs}")
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
            spread_c = oos.distribution_sigma_c([float(blended.get(k, 0.0)) for k in keys], list(key_centers))
            layer = dataclasses.replace(tables["center_width_layer"] or replay.CenterWidthLayer(),
                                        width_lambda=WIDTH, width_tier_lambdas=None, width_tier_boundaries=None)
            vecs = {}
            for arm in ARMS:
                shift = terms[arm][0]
                pre = oos._ladder_shape_stack(
                    blended, ladder=ladder, cooling_shift_sigma=(None if arm == "none" else shift),
                    station_latitude=latitude, neighbour_center_c=sum(seat[s] * centers[s] for s in seated),
                    center_width_layer=None, band_layer=None, skip=None)
                vec, _cw, _band = apply_served_table_layers(
                    pre, ordered_keys=keys, key_centers_c=key_centers, center_width=layer, band=None,
                    open_edge_bounds_c=open_edges)
                vecs[arm] = [round(float(vec[key]), 9) for key in keys]
            receipt = terms["cur"][1] or {}
            day_rows_out.append({
                "at": at_iso, "lead": newest_lead(packet), "pool": len(seated), "rad_state": state,
                "z": receipt.get("standardised"), "method": receipt.get("method_tag"),
                "shift_sigma": {arm: terms[arm][0] for arm in ARMS}, "spread_c": spread_c, "vecs": vecs})
        if day_rows_out:
            out.append({"station": station, "event_date": event_date, "keys": list(keys),
                        "centers_c": list(key_centers), "hit": keys.index(oos._ladder_bucket(ladder, float(actual))),
                        "actual": actual, "updates": day_rows_out})
    return out


def newest_lead(packet):
    return max(packet.values(), key=lambda v: (v[1], v[0]))[2][2]


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
    raise SystemExit(main())
