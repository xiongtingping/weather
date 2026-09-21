#!/usr/bin/env python3
"""Charter 149 replay: a split rider calibrated under the packet lead vs its own.

Built on charter 134's arrival-clock replay (``replay_p0_stable_pool``): every
station-day is walked forward in archive-arrival order and each UPDATE is the
packet a P0 compile would have seen then.  At each update the same stable pool
(kappa ``STABLE_POOL_KAPPA``, skill weights from the members' own-scope
residuals) is blended twice, and only the per-system calibration of a member
riding another lead than the packet's differs:

  NEW (charter 149)  own lead, own cycle -> own lead, another cycle (borrow)
                     -> packet lead, own cycle -> packet lead, another cycle
  OLD (before)       packet lead, own cycle -> packet lead, another cycle

"Exact" is a walk-forward ``_fit_parameters`` over at least MIN_TRAIN settled
days knowable at the update; a borrow serves only the donor's bias_c and
max(1, spread_factor) at reliability 1, as ``compile_native_ensemble_p0``
does.  A member on the packet's lead is calibrated identically in both arms.
Both arms are scored under the band and centre/width tables active NOW, so
the centre/width lane's own ``_search`` can be run on either arm's pre-band
served vectors and read as "the residual the incumbent layer would need".

Read-only.  One gzip JSONL per station, resumable under a time budget.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import replay_p0_stable_pool as base  # noqa: E402
from bayes_basket_target import native_ensemble_oos as oos  # noqa: E402
from bayes_basket_target.cross_consensus import (  # noqa: E402
    radiation_shift_sigma_for_lead,
)
from bayes_basket_target.stable_pool import (  # noqa: E402
    STABLE_POOL_KAPPA,
    stable_pool_weights,
)
from bayes_basket_target.station_registry import station_coordinates  # noqa: E402

SCHEMA = "p0_member_own_lead_replay_update_v1"
CUTOFF_UTC = "2026-09-18T09:30:00Z"
TABLES_AS_OF_UTC = "2026-09-18T09:30:00Z"
FIRST_EVENT_DATE = "2026-08-20"
LAST_EVENT_DATE = "2026-09-17"
MIN_TRAIN = base.MIN_TRAIN
OUT = ROOT / "tmp" / "member_own_lead_replay_20260918" / "updates"


def _borrow(forecasts: base.Forecasts, donor_scope, case, knowable):
    """Charter 101's borrow: the donor's bias_c and spread only."""

    train = forecasts.train(donor_scope, case["event_date"], knowable)
    if len(train) < MIN_TRAIN:
        return None
    fitted = oos._fit_parameters(train)
    values = [float(v) for v in case["values"]]
    params = {
        "bias_c": float(fitted["bias_c"]),
        "correction_form": "constant",
        "spread_factor": max(1.0, float(fitted["spread_factor"])),
        "reliability_weight": 1.0,
        "climate_samples_c": values,
        "train_rmse_c": fitted["train_rmse_c"],
        "train_residual_sigma_c": fitted.get("train_residual_sigma_c"),
    }
    return {
        "event_date": case["event_date"],
        "probability": oos._apply_parameters(values, params),
        "calibrated_center_c": oos._corrected_center(
            sum(values) / len(values), params
        ),
        "actual_tmax_c": float(case["actual_tmax_c"]),
        "error_scale": float(
            fitted.get("train_residual_sigma_c") or fitted["train_rmse_c"]
        ),
        "train_n": len(train),
        "residuals": {
            c["event_date"]: float(c["actual_tmax_c"])
            - sum(float(v) for v in c["values"]) / len(c["values"])
            for c in train
        },
    }


def _other_cycle_scopes(cases_by_scope, system, season, lead, cycle):
    return sorted(
        (
            scope for scope in cases_by_scope
            if scope[0] == system and scope[1] == season
            and scope[2] == lead and scope[3] != cycle
        ),
        key=lambda scope: -len(cases_by_scope[scope]),
    )


def _chain(forecasts, cases_by_scope, own_scope, case, knowable, leads):
    """First row the lead order yields: exact, then another-cycle borrow."""

    system, season, _own_lead, cycle = own_scope
    for lead in leads:
        scope = (system, season, lead, cycle)
        if scope in cases_by_scope:
            row = forecasts.forecast(scope, case, knowable)
            if row is not None:
                return row, f"exact@{lead}"
        for donor in _other_cycle_scopes(
            cases_by_scope, system, season, lead, cycle
        ):
            row = _borrow(forecasts, donor, case, knowable)
            if row is not None:
                return row, f"borrow@{lead}:{donor[3]}"
    return None, "none"


def replay_station(conn: sqlite3.Connection, station: str) -> list[dict[str, Any]]:
    base.FIRST_EVENT_DATE = FIRST_EVENT_DATE
    natives = set(oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS)
    systems = oos._load_systems(conn)
    family = {s: str(systems[s]["independence_family_id"]) for s in natives if s in systems}
    all_cases = oos._load_cases(conn, station_id=station, as_of_utc=CUTOFF_UTC)
    cases_by_scope = {
        scope: sorted(rows, key=lambda c: (c["event_date"], c["aggregator_snapshot_at_utc"]))
        for scope, rows in all_cases.items()
        if scope[0] in family and all(c.get("transport_provider_id") != "OPEN_METEO" for c in rows)
    }
    first_commit = base._first_truth_commit(conn, station)
    fetched = base._arrivals(conn, station)

    def arrival_of(scope, case) -> datetime:
        key = (scope[0], case["event_date"], str(case.get("archive_lead_bucket") or ""), scope[3], base._run_init(case))
        return fetched.get(key) or base._utc(case["aggregator_snapshot_at_utc"])

    forecasts = base.Forecasts(cases_by_scope, first_commit, arrival_of)
    try:
        latitude = float(station_coordinates(station)["latitude"])
    except Exception:  # noqa: BLE001
        latitude = None
    tables = oos.p0_gate_tables(conn, station_id=station, tables_as_of_utc=TABLES_AS_OF_UTC)
    arrivals: dict[str, list] = defaultdict(list)
    for scope, rows in cases_by_scope.items():
        for case in rows:
            if FIRST_EVENT_DATE <= case["event_date"] <= LAST_EVENT_DATE:
                arrivals[case["event_date"]].append(
                    (arrival_of(scope, case), base._run_init(case), scope[0], scope, case)
                )
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
            packet_lead = max(packet.values(), key=lambda v: (v[1], v[0]))[2][2]
            new_rows: dict[str, dict] = {}
            old_rows: dict[str, dict] = {}
            paths: dict[str, list[str]] = {}
            for s, (_at, _init, s_scope, s_case) in packet.items():
                own_lead = s_scope[2]
                if own_lead == packet_lead:
                    row, how = _chain(forecasts, cases_by_scope, s_scope, s_case, at, (own_lead,))
                    if row is not None:
                        new_rows[s] = old_rows[s] = row
                        paths[s] = [how, how]
                    continue
                new, how_new = _chain(forecasts, cases_by_scope, s_scope, s_case, at, (own_lead, packet_lead))
                if new is None:
                    continue
                old, how_old = _chain(forecasts, cases_by_scope, s_scope, s_case, at, (packet_lead,))
                if old is None:
                    # Production's peer path; too rare to model, left equal.
                    old, how_old = new, "peer_path_left_equal"
                new_rows[s], old_rows[s] = new, old
                paths[s] = [how_new, how_old]
            pool = sorted(new_rows)
            if len({family[s] for s in pool}) < 2:
                continue
            actual = new_rows[pool[0]]["actual_tmax_c"]
            skill, _rule = oos._seat_weights(
                residuals_by_system={s: new_rows[s]["residuals"] for s in pool},
                train_error_scale_by_system={s: new_rows[s]["error_scale"] for s in pool},
                family_by_system={s: family[s] for s in pool},
                family_caps=oos.DEFAULT_FAMILY_WEIGHT_CAPS,
                train_n_by_system={s: new_rows[s]["train_n"] for s in pool},
            )
            skill = {s: float(skill.get(s, 0.0)) for s in pool}
            total = sum(skill.values())
            skill = {s: v / total for s, v in skill.items()}
            weights = stable_pool_weights(skill, {s: family[s] for s in pool}, kappa=STABLE_POOL_KAPPA)
            weights = {s: v for s, v in weights.items() if v > 0.0}
            sigma_per_unit = radiation_shift_sigma_for_lead(packet_lead)
            differs = any(new_rows[s] is not old_rows[s] for s in weights)
            record = {
                "schema": SCHEMA, "station": station, "event_date": event_date,
                "at": at.isoformat().replace("+00:00", "Z"), "lead": packet_lead,
                "actual": actual, "pool": pool,
                "own_lead": {s: packet[s][2][2] for s in pool},
                "cycle": {s: packet[s][2][3] for s in pool},
                "paths": {s: paths[s] for s in pool},
                "w": {s: round(v, 5) for s, v in weights.items()},
                "cw_sha": tables["center_width_sha256"], "band_sha": tables["band_table_sha256"],
                "differs": differs, "arms": {},
            }
            for arm, rows in (("NEW", new_rows), ("OLD", old_rows)):
                if arm == "OLD" and not differs:
                    break
                signals = oos._holdout_conditioning_signals(
                    conn, station_id=station, holdout_days=[event_date],
                    predictions_by_system={s: {event_date: rows[s]} for s in pool},
                    radiation_shift_sigma=sigma_per_unit,
                )
                cooling = (signals.get(event_date) or {}).get("synoptic_cooling_shift_sigma")
                record["arms"][arm] = base._blend_and_score(
                    weights=weights, day_rows=rows, cooling_shift_sigma=cooling,
                    latitude=latitude, tables=tables, actual=actual,
                )
                record["arms"][arm]["centers"] = {
                    s: round(float(rows[s]["calibrated_center_c"]), 4) for s in weights
                }
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
            " WHERE stage='P0' AND state='COMMITTED' AND decision_at_utc>='2026-09-15T00:00:00Z'"
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
        print(station, len(records), sum(1 for r in records if r["differs"]), round(time.monotonic() - t0, 1), "s", flush=True)
    remaining = [s for s in stations if not (OUT / f"{s}.jsonl.gz").exists()]
    print("remaining", len(remaining), "of", len(stations))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
