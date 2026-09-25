#!/usr/bin/env python3
"""PROTOCOL.md: the consensus bridge on the market ladder, today's arithmetic.

The six-family replay of ``p0_replay_219x216_20260925/capture_2x2.py`` reduced
to its ``ll_on`` arm (corrected training set, charter 216's production fit),
moved onto the station-day's market ladder: every system's calibrated
distribution is the serving function's on that ladder, the blend is shaped by
the gate's ladder stack and scored by the gate's floored ladder metrics.  Per
update, with the pool and seats fixed, the bridge is varied -- a parameter grid
under the point rule, and the current parameters under charter 190's tail
transport -- and each blend is served under five width lambdas.  The current
point arm is asserted equal to production's ``bridged_cross_probability``.

    python3 capture_bridge.py --stations EGLC,KLGA
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
    "capture_2x2", ROOT / "docs" / "research" / "p0_replay_219x216_20260925" / "capture_2x2.py")
X = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(X)
C, oos, replay, HIST = X.C, X.oos, X.replay, X.HIST
from bayes_basket_target import center_width_calibration as CW  # noqa: E402
from bayes_basket_target import cross_consensus as CC  # noqa: E402
from bayes_basket_target.served_shape import apply_served_table_layers  # noqa: E402

CURRENT = (CC.CONSENSUS_BRIDGE_GAP_START_C, CC.CONSENSUS_BRIDGE_GAP_FULL_C,
           CC.CONSENSUS_BRIDGE_LAMBDA_MAX)
assert CURRENT == (1.5, 2.0, 0.4), CURRENT
VARIANTS = (("off", None),) + tuple(
    (f"s{start:.1f}_m{lmax:.1f}", (start, start + 0.5, lmax))
    for start in (0.5, 1.0, 1.5, 2.0) for lmax in (0.2, 0.4, 0.6, 0.8))
WIDTHS = (0.70, 0.75, 0.80, 0.85, 0.90)
OUT = HERE / "pass1"


def bridge_lambda(gap: float, params) -> float:
    """``consensus_bridge_lambda`` with the variant's constants, same arithmetic."""

    if params is None:
        return 0.0
    start, full, lmax = params
    if gap <= start:
        return 0.0
    if gap >= full:
        return lmax
    return lmax * ((gap - start) / (full - start))


def shift_tail(probability, *, delta_c, keys, centers, open_edges):
    """Charter 190's patched ``shift_probability``: an open end bucket moves as a tail."""

    lower, upper = CW._open_edges(open_edges, centers)
    if lower is None and upper is None:
        return CC.shift_probability(probability, delta_c=delta_c, ordered_keys=keys,
                                    key_centers_c=centers)
    edges_c = CW._bucket_edges_c(centers, lower, upper)
    rung_c = CW.bucket_rung_width_c(centers)
    adjacent = (float(probability.get(keys[1], 0.0)), float(probability.get(keys[-2], 0.0)))
    shifted = {key: 0.0 for key in keys}
    for key, mass in probability.items():
        weight = float(mass)
        if weight <= 0.0:
            continue
        index = keys.index(str(key))
        side = ("lower" if lower is not None and index == 0
                else "upper" if upper is not None and index == len(keys) - 1 else None)
        if side is not None:
            landing, _scale = CW._open_edge_tail_landing(
                weight, side=side, bound_c=lower if side == "lower" else upper,
                adjacent_weight=adjacent[0 if side == "lower" else 1], rung_c=rung_c,
                edges_c=edges_c, mean_c=0.0, lam=1.0, shift=float(delta_c))
            for position, share in enumerate(landing):
                shifted[keys[position]] += share
            continue
        position = centers[index] + float(delta_c)
        if position <= centers[0]:
            shifted[keys[0]] += weight
            continue
        if position >= centers[-1]:
            shifted[keys[-1]] += weight
            continue
        for low in range(len(centers) - 1):
            a, b = centers[low], centers[low + 1]
            if a <= position <= b:
                span = b - a
                share = (position - a) / span if span > 0 else 0.0
                shifted[keys[low]] += weight * (1.0 - share)
                shifted[keys[low + 1]] += weight * share
                break
    return shifted


def blend(probs, weights, centers, keys, key_centers, lam, open_edges=None):
    """``bridged_cross_probability``'s arithmetic at a given blend weight."""

    mixture = {key: sum(float(weights[s]) * float(probs[s].get(key, 0.0)) for s in weights) for key in keys}
    total = sum(mixture.values())
    mixture = {key: value / total for key, value in mixture.items()}
    if lam <= 0.0:
        return mixture
    consensus_center = sum(float(weights[s]) * centers[s] for s in weights)
    consensus = {key: 0.0 for key in keys}
    for s, weight in weights.items():
        if open_edges is None:
            moved = CC.shift_probability(probs[s], delta_c=consensus_center - centers[s],
                                         ordered_keys=keys, key_centers_c=key_centers)
        else:
            moved = shift_tail(probs[s], delta_c=consensus_center - centers[s], keys=keys,
                               centers=key_centers, open_edges=open_edges)
        for key in keys:
            consensus[key] += float(weight) * moved[key]
    total = sum(consensus.values())
    consensus = {key: value / total for key, value in consensus.items()}
    return {key: (1.0 - lam) * mixture[key] + lam * consensus[key] for key in keys}


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
        p = oos._fit_parameters(train)                      # production default: charter 216
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
    tables_memo, out, skipped = {}, [], {"no_ladder_days": 0}
    for event_date in sorted(arrivals):
        if event_date not in first_commit:
            continue
        ladder = ladder_for(event_date)
        if ladder is None:
            skipped["no_ladder_days"] += 1
            continue
        keys, key_centers, open_edges = ladder["ordered"], ladder["centers_c"], ladder["open_edge_bounds_c"]
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
            probs = {s: day_rows[s]["probability"] for s in seated}
            centers = {s: float(day_rows[s]["calibrated_center_c"]) for s in seated}
            gap = max(centers.values()) - min(centers.values())
            model_center = sum(seat[s] * centers[s] for s in seated)
            # The production bridge, for the replica check and the served arm.
            produced, receipt = CC.bridged_cross_probability(
                system_probabilities=probs, system_weights=seat, system_centers_c=centers,
                ordered_keys=keys, key_centers_c=key_centers)
            lam_current = bridge_lambda(gap, CURRENT)
            replica = blend(probs, seat, centers, keys, key_centers, lam_current)
            if replica != produced or abs(float(receipt["lambda"]) - round(lam_current, 6)) > 1e-12:
                raise RuntimeError(f"bridge replica differs: {station} {event_date} {C._iso(at)}")
            base_layer = tables["center_width_layer"] or replay.CenterWidthLayer()

            def serve(blended):
                pre = oos._ladder_shape_stack(
                    blended, ladder=ladder, cooling_shift_sigma=cooling, station_latitude=latitude,
                    neighbour_center_c=model_center, center_width_layer=None, band_layer=None, skip=None)
                scored = []
                for lam_w in WIDTHS:
                    layer = dataclasses.replace(base_layer, width_lambda=lam_w,
                                                width_tier_lambdas=None, width_tier_boundaries=None)
                    vec, _cw, _band = apply_served_table_layers(
                        pre, ordered_keys=keys, key_centers_c=key_centers, center_width=layer,
                        band=tables["band_layer"], open_edge_bounds_c=open_edges)
                    metrics = oos._ladder_metrics(vec, actual, ladder)
                    scored.append([round(metrics["brier"], 6), round(metrics["nll"], 6)])
                return scored

            lam_keys, point = [], {}
            for _name, params in VARIANTS:
                lam = bridge_lambda(gap, params)
                key = f"{lam:.9f}"
                lam_keys.append(key)
                if key not in point:
                    point[key] = serve(produced if lam == lam_current else blend(probs, seat, centers, keys, key_centers, lam))
            served_vec = oos._ladder_shape_stack(
                produced, ladder=ladder, cooling_shift_sigma=cooling, station_latitude=latitude,
                neighbour_center_c=model_center, center_width_layer=tables["center_width_layer"],
                band_layer=tables["band_layer"], skip=None)
            served_metrics = oos._ladder_metrics(served_vec, actual, ladder)
            record = {
                "station": station, "event_date": event_date, "at": C._iso(at), "lead": newest[2],
                "pool": len(seated), "gap": round(gap, 4), "lam_current": round(lam_current, 6),
                "has_width_table": tables["center_width_layer"] is not None,
                "served": [round(served_metrics["brier"], 6), round(served_metrics["nll"], 6)],
                "lam_keys": lam_keys, "point": point,
            }
            if lam_current > 0.0 and (open_edges[0] is not None or open_edges[1] is not None):
                tail = blend(probs, seat, centers, keys, key_centers, lam_current, open_edges=open_edges)
                consensus_center = sum(float(seat[s]) * centers[s] for s in seat)
                inward_open = []
                for s in seated:
                    delta = consensus_center - centers[s]
                    if open_edges[0] is not None and delta > 0:
                        inward_open.append((float(probs[s].get(keys[0], 0.0)), float(probs[s].get(keys[1], 0.0))))
                    if open_edges[1] is not None and delta < 0:
                        inward_open.append((float(probs[s].get(keys[-1], 0.0)), float(probs[s].get(keys[-2], 0.0))))
                record["tail_differs"] = tail != produced
                record["open_mass_inward"] = round(max((m for m, _n in inward_open), default=0.0), 4)
                record["mode_in_open"] = any(m >= 0.05 and m > n for m, n in inward_open)
                record["tail"] = serve(tail) if record["tail_differs"] else point[f"{lam_current:.9f}"]
            out.append(record)
    return out, skipped


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
        rows, skipped = capture_station(conn, station, zones[station])
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt") as handle:
            for row in rows:
                handle.write(json.dumps(row, separators=(",", ":")) + "\n")
        tmp.rename(path)
        print(f"{station}: {len(rows)} updates, skipped {skipped}, "
              f"bridge on {sum(1 for r in rows if r['lam_current'] > 0)}, "
              f"tail differs {sum(1 for r in rows if r.get('tail_differs'))}, {time.time() - t0:.0f}s", flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
