#!/usr/bin/env python3
"""Charter 134 nested replay: the subset contest against the stable pool.

For every station-day, walk the native P0 inputs forward in arrival order.  An
UPDATE is the moment a system's freshest run for the event day changes -- the
packet a P0 compile would have seen then.  At each update, three families of
scheme blend the same per-system forecasts:

  A        the subset contest, picked on the three latest shared settled days
           knowable at that moment, served on the event day (today's rule)
  C_k      every qualified system with a run, skill weights shrunk toward the
           family baseline by kappa in STABLE_POOL_KAPPA_GRID; kappa=1 is the
           family-balanced pool, kappa=0 the unshrunk production skill weights

Every per-system forecast is a walk-forward serving fit: ``_fit_parameters``
over the rolling window of settled days whose truth was committed before the
update and whose capture preceded it.  Blends go through the production
bridge and ``_holdout_shape_stack`` under the band / centre-width tables
active at the update (``p0_gate_tables``), and each is scored four ways: no
fitted tables, the tables as served, and a width-lambda grid under the served
band.  Nothing selects kappa or lambda here; the aggregator does that on days
strictly before each outer day.

Read-only against the SSOT.  One gzip JSONL per station, written atomically,
so a run cut at its time budget resumes where it stopped.
"""
from __future__ import annotations

import argparse
import dataclasses
import gzip
import json
import math
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import native_ensemble_oos as oos  # noqa: E402
from bayes_basket_target.cross_consensus import (  # noqa: E402
    bridged_cross_probability,
    radiation_shift_sigma_for_lead,
)
from bayes_basket_target.served_shape import (  # noqa: E402
    CenterWidthLayer,
    apply_served_table_layers,
)
from bayes_basket_target.stable_pool import (  # noqa: E402
    STABLE_POOL_KAPPA_GRID,
    family_balanced_weights,
    stable_pool_weights,
)
from bayes_basket_target.station_registry import station_coordinates  # noqa: E402

SCHEMA = "p0_stable_pool_replay_update_v3"
CUTOFF_UTC = "2026-09-14T00:00:00Z"
FIRST_EVENT_DATE = "2026-08-20"
LAST_EVENT_DATE = "2026-09-12"
LAMBDA_GRID = (0.6, 0.7, 0.75, 0.8, 0.9, 1.0, 1.1, 1.2)
CONTEST_DAYS = oos.FORWARD_ACCUMULATION_MIN_HOLDOUT_DAYS
MIN_TRAIN = oos.FORWARD_ACCUMULATION_MIN_TRAIN_DAYS
OUT = ROOT / "tmp" / "stable_pool_replay_20260914" / "updates"


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _first_truth_commit(conn: sqlite3.Connection, station: str) -> dict[str, datetime]:
    """When each station-day's settled high first became knowable."""

    rows = conn.execute(
        "SELECT local_date, MIN(canonical_committed_at_utc)"
        " FROM calibration_station_daily_high_truth"
        " WHERE station_id=? AND source_lane='AWC' AND qc_status='ACCEPTED'"
        " AND authority_scope IN ('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE')"
        " GROUP BY local_date",
        (station,),
    ).fetchall()
    return {str(day): _utc(stamp) for day, stamp in rows if stamp}


def _arrivals(conn: sqlite3.Connection, station: str) -> dict[tuple, datetime]:
    """When each native run was actually fetched.

    ``_load_cases`` stamps an offline native case with its run's
    initialisation time, which is right for strict OOS ordering but is not
    when a compile could have read it: the archive query lands 5-20 hours
    later.  Updates are ordered, and knowability is judged, on arrival.
    """

    rows = conn.execute(
        "SELECT ensemble_system_id, target_local_date, archive_lead_bucket, run_cycle,"
        " native_run_init_utc, MIN(archive_query_at_utc)"
        " FROM calibration_native_ensemble_historical_member_run"
        " WHERE station_id=? AND target_local_date>=? AND qc_status='ACCEPTED'"
        " GROUP BY 1, 2, 3, 4, 5",
        (station, (date.fromisoformat(FIRST_EVENT_DATE) - timedelta(days=oos.ROLLING_TRAIN_WINDOW_DAYS + 5)).isoformat()),
    ).fetchall()
    return {
        (str(system), str(day), str(lead), str(cycle), str(init or "")): _utc(stamp)
        for system, day, lead, cycle, init, stamp in rows if stamp
    }


def _run_init(case: dict[str, Any]) -> str:
    inits = sorted({str(value or "") for value in case.get("member_native_run_init_utcs") or []})
    return inits[-1] if inits else ""


class Forecasts:
    """Walk-forward per-system forecasts, memoised on their training set."""

    def __init__(self, cases_by_scope, first_commit, arrival_of):
        self.cases_by_scope = cases_by_scope
        self.first_commit = first_commit
        self.arrival_of = arrival_of
        self.memo: dict[tuple, dict[str, Any] | None] = {}

    def train(self, scope, event_date: str, knowable: datetime):
        start = (date.fromisoformat(event_date) - timedelta(days=oos.ROLLING_TRAIN_WINDOW_DAYS)).isoformat()
        return [
            case for case in self.cases_by_scope[scope]
            if start <= case["event_date"] < event_date
            and self.arrival_of(scope, case) < knowable
            and case["event_date"] in self.first_commit
            and self.first_commit[case["event_date"]] < knowable
        ]

    def forecast(self, scope, case, knowable: datetime) -> dict[str, Any] | None:
        train = self.train(scope, case["event_date"], knowable)
        key = (scope, case["event_date"], case["member_set_sha256"], tuple(c["event_date"] for c in train))
        if key in self.memo:
            return self.memo[key]
        if len(train) < MIN_TRAIN:
            self.memo[key] = None
            return None
        params = oos._fit_parameters(train)
        values = [float(v) for v in case["values"]]
        row = {
            "event_date": case["event_date"],
            "probability": oos._apply_parameters(values, params),
            "calibrated_center_c": oos._corrected_center(sum(values) / len(values), params),
            "actual_tmax_c": float(case["actual_tmax_c"]),
            "error_scale": float(params.get("train_residual_sigma_c") or params["train_rmse_c"]),
            "train_n": len(train),
            "residuals": {
                c["event_date"]: float(c["actual_tmax_c"]) - sum(float(v) for v in c["values"]) / len(c["values"])
                for c in train
            },
        }
        self.memo[key] = row
        return row


def _grid(probabilities) -> list[str]:
    return sorted(set().union(*(set(p) for p in probabilities)), key=lambda key: int(key))


def _score(probability: dict[str, float], actual: float) -> list[float]:
    metrics = oos._metrics(probability, actual)
    bucket = oos._temperature_bucket(actual)
    below = sum(v for k, v in probability.items() if int(k) < int(bucket))
    at = float(probability.get(bucket, 0.0))
    return [round(metrics["crps"], 6), round(metrics["nll"], 6), round(metrics["brier"], 6), round(below, 6), round(at, 6)]


def _blend_and_score(*, weights, day_rows, cooling_shift_sigma, latitude, tables, actual):
    systems = sorted(weights)
    grid = _grid([day_rows[s]["probability"] for s in systems])
    centers = {s: float(day_rows[s]["calibrated_center_c"]) for s in systems}
    blended, _receipt = bridged_cross_probability(
        system_probabilities={s: day_rows[s]["probability"] for s in systems},
        system_weights=weights,
        system_centers_c=centers,
        ordered_keys=grid,
        key_centers_c=[float(int(k)) for k in grid],
    )
    model_center = sum(weights[s] * centers[s] for s in systems)
    pre_tables = oos._holdout_shape_stack(
        blended, grid=grid, cooling_shift_sigma=cooling_shift_sigma, station_latitude=latitude,
        neighbour_center_c=model_center, center_width_layer=None, band_layer=None,
    )
    served, _cw, _band = apply_served_table_layers(
        pre_tables, ordered_keys=grid, key_centers_c=[float(int(k)) for k in grid],
        center_width=tables["center_width_layer"], band=tables["band_layer"],
    )
    # The same vector with the band left out, kept whole so the aggregator
    # can refit the band on this scheme's own claims and apply it here.
    pre_band, _cw, _band = apply_served_table_layers(
        pre_tables, ordered_keys=grid, key_centers_c=[float(int(k)) for k in grid],
        center_width=tables["center_width_layer"], band=None,
    )
    base_layer = tables["center_width_layer"] or CenterWidthLayer()
    by_lambda = []
    for lam in LAMBDA_GRID:
        layer = dataclasses.replace(base_layer, width_lambda=lam, width_tier_lambdas=None, width_tier_boundaries=None)
        vector, _cw, _band = apply_served_table_layers(
            pre_tables, ordered_keys=grid, key_centers_c=[float(int(k)) for k in grid],
            center_width=layer, band=tables["band_layer"],
        )
        by_lambda.append(_score(vector, actual))
    mean = sum(float(int(k)) * v for k, v in served.items())
    sigma = math.sqrt(sum((float(int(k)) - mean) ** 2 * v for k, v in served.items()))
    return {
        "w": {s: round(weights[s], 5) for s in systems},
        "mu": round(model_center, 4),
        "mean": round(mean, 4),
        "sigma": round(sigma, 4),
        "raw": _score(pre_tables, actual),
        "noband": _score(pre_band, actual),
        "pre_band": {k: round(v, 6) for k, v in pre_band.items() if v >= 5e-7},
        "served": _score(served, actual),
        "lam": by_lambda,
        "vec": {k: round(v, 5) for k, v in served.items() if v >= 5e-6},
    }


def replay_station(conn: sqlite3.Connection, station: str) -> list[dict[str, Any]]:
    natives = set(oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS)
    systems = oos._load_systems(conn)
    family = {s: str(systems[s]["independence_family_id"]) for s in natives if s in systems}
    all_cases = oos._load_cases(conn, station_id=station, as_of_utc=CUTOFF_UTC)
    cases_by_scope = {
        scope: sorted(rows, key=lambda c: (c["event_date"], c["aggregator_snapshot_at_utc"]))
        for scope, rows in all_cases.items()
        if scope[0] in family and all(c.get("transport_provider_id") != "OPEN_METEO" for c in rows)
    }
    first_commit = _first_truth_commit(conn, station)
    fetched = _arrivals(conn, station)

    def arrival_of(scope, case) -> datetime:
        key = (scope[0], case["event_date"], str(case.get("archive_lead_bucket") or ""), scope[3], _run_init(case))
        return fetched.get(key) or _utc(case["aggregator_snapshot_at_utc"])

    forecasts = Forecasts(cases_by_scope, first_commit, arrival_of)
    try:
        latitude = float(station_coordinates(station)["latitude"])
    except Exception:  # noqa: BLE001
        latitude = None
    arrivals: dict[str, list[tuple[datetime, str, str, tuple, dict]]] = defaultdict(list)
    for scope, rows in cases_by_scope.items():
        for case in rows:
            if FIRST_EVENT_DATE <= case["event_date"] <= LAST_EVENT_DATE:
                arrivals[case["event_date"]].append(
                    (arrival_of(scope, case), _run_init(case), scope[0], scope, case)
                )
    tables_memo: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for event_date in sorted(arrivals):
        events = sorted(arrivals[event_date], key=lambda e: (e[0], e[1], e[2]))
        packet: dict[str, tuple] = {}
        previous_signature = None
        for index, (at, run_init, system, scope, case) in enumerate(events):
            current = packet.get(system)
            if current is None or (run_init, at) >= (current[1], current[0]):
                packet[system] = (at, run_init, scope, case)
            if index + 1 < len(events) and events[index + 1][0] == at:
                continue
            signature = tuple(sorted((s, v[2], v[3]["member_set_sha256"]) for s, v in packet.items()))
            if signature == previous_signature:
                continue
            previous_signature = signature
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
                tables_memo[hour_key] = oos.p0_gate_tables(conn, station_id=station, tables_as_of_utc=at.isoformat().replace("+00:00", "Z"))
            tables = tables_memo[hour_key]
            newest_scope = max(packet.values(), key=lambda v: (v[1], v[0]))[2]
            sigma_per_unit = radiation_shift_sigma_for_lead(newest_scope[2])
            signals = oos._holdout_conditioning_signals(
                conn, station_id=station, holdout_days=[event_date],
                predictions_by_system={s: {event_date: day_rows[s]} for s in pool},
                radiation_shift_sigma=sigma_per_unit,
            )
            cooling = (signals.get(event_date) or {}).get("synoptic_cooling_shift_sigma")
            common = lambda s: dict(latitude=latitude, tables=tables, actual=actual, day_rows=day_rows, cooling_shift_sigma=cooling)  # noqa: E731
            record = {
                "schema": SCHEMA, "station": station, "event_date": event_date,
                "at": at.isoformat().replace("+00:00", "Z"),
                "lead": newest_scope[2], "actual": actual,
                "pool": pool, "scopes": {s: [packet[s][2][2], packet[s][2][3]] for s in pool},
                "runs": {s: packet[s][1] for s in pool},
                "band_sha": tables["band_table_sha256"], "cw_sha": tables["center_width_sha256"],
                "schemes": {},
            }
            residuals = {s: day_rows[s]["residuals"] for s in pool}
            scale = {s: day_rows[s]["error_scale"] for s in pool}
            train_n = {s: day_rows[s]["train_n"] for s in pool}
            skill, rule = oos._seat_weights(
                residuals_by_system=residuals, train_error_scale_by_system=scale,
                family_by_system={s: family[s] for s in pool}, family_caps=oos.DEFAULT_FAMILY_WEIGHT_CAPS,
                train_n_by_system=train_n,
            )
            skill = {s: float(skill.get(s, 0.0)) for s in pool}
            total = sum(skill.values())
            skill = {s: v / total for s, v in skill.items()}
            record["skill_rule"] = rule
            for kappa in STABLE_POOL_KAPPA_GRID:
                weights = stable_pool_weights(skill, {s: family[s] for s in pool}, kappa=kappa)
                weights = {s: v for s, v in weights.items() if v > 0.0}
                record["schemes"][f"C{kappa:g}"] = _blend_and_score(weights=weights, **common(None))
            # A: the contest on the latest shared settled days knowable now.
            prior_rows: dict[str, dict[str, dict[str, Any]]] = {}
            for s in pool:
                s_scope = packet[s][2]
                rows = {}
                for prior in cases_by_scope[s_scope]:
                    x = prior["event_date"]
                    if x >= event_date or x not in first_commit or first_commit[x] >= at:
                        continue
                    prior_arrival = arrival_of(s_scope, prior)
                    if prior_arrival >= at:
                        continue
                    row = forecasts.forecast(s_scope, prior, prior_arrival)
                    if row is not None:
                        rows[x] = row
                prior_rows[s] = rows
            shared = sorted(set.intersection(*(set(r) for r in prior_rows.values())))[-CONTEST_DAYS:]
            if len(shared) == CONTEST_DAYS:
                earliest = shared[0]
                contest_residuals = {s: {d: v for d, v in residuals[s].items() if d < earliest} for s in pool}
                contest_signals = oos._holdout_conditioning_signals(
                    conn, station_id=station, holdout_days=shared,
                    predictions_by_system=prior_rows, radiation_shift_sigma=sigma_per_unit,
                )
                try:
                    weights_a, receipt = oos._select_minimum_crps_subset(
                        ready_system_ids=pool, predictions_by_system=prior_rows,
                        common_holdout=shared, family_by_system={s: family[s] for s in pool},
                        configured_caps=oos.DEFAULT_FAMILY_WEIGHT_CAPS,
                        train_error_scale={s: prior_rows[s][earliest]["error_scale"] for s in pool},
                        train_n_by_system={s: prior_rows[s][earliest]["train_n"] for s in pool},
                        residuals_by_system=contest_residuals, conditioning_by_day=contest_signals,
                        station_latitude=latitude, band_factors=tables["band_factors"],
                        band_axis=tables["band_table_axis"], center_width_layer=tables["center_width_layer"],
                    )
                    weights_a = {s: v for s, v in weights_a.items() if v > 0.0}
                    record["schemes"]["A"] = _blend_and_score(weights=weights_a, **common(None))
                    record["contest_days"] = shared
                    record["contest_subsets"] = receipt["evaluated_subset_count"]
                except oos.ContractError as exc:
                    record["contest_error"] = str(exc)
            out.append(record)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget-seconds", type=float, default=540.0)
    parser.add_argument("--stations", nargs="*", default=None)
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=1")
    stations = args.stations or sorted({
        str(row[0]) for row in conn.execute(
            "SELECT DISTINCT station_id FROM basket_probability_envelope"
            " WHERE stage='P0' AND state='COMMITTED' AND decision_at_utc>='2026-09-12T00:00:00Z'"
        )
    })
    started = time.monotonic()
    for station in stations:
        target = OUT / f"{station}.jsonl.gz"
        if target.exists():
            continue
        if time.monotonic() - started > args.budget_seconds:
            break
        t0 = time.monotonic()
        records = replay_station(conn, station)
        partial = target.with_suffix(".partial")
        with gzip.open(partial, "wt") as handle:
            for record in records:
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        partial.rename(target)
        print(station, len(records), round(time.monotonic() - t0, 1), "s", flush=True)
    remaining = [s for s in stations if not (OUT / f"{s}.jsonl.gz").exists()]
    print("remaining", len(remaining), "of", len(stations))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
