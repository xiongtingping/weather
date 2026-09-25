#!/usr/bin/env python3
"""Pass 1: production's stable-pool replay, seat arm only, per-system rows kept.

The same functions ``scripts/replay_p0_stable_pool.py`` runs -- walk-forward
per-system fits, the production bridge, shape stack and the tables served at
each update -- over 2026-07-20..09-22 with the five native families.  Only the
kappa=0.5 seat is scored (the contest is retired); every update keeps each
system's calibrated distribution and centre so pass 2 can re-blend them, and
the tables active at each update hour are pickled beside it.  Read-only;
resumable per station within a time budget.  See PROTOCOL.md.
"""
from __future__ import annotations

import argparse
import gzip
import json
import pickle
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


# Backfilled history carries its backfill date as the archive query: AIFS ENS
# rows were loaded 2026-09-22/23 and AIGEFS rows 2026-09-23, 700+ hours after
# their runs, so on the recorded clock neither system's history ever arrived
# in time to serve.  With ``--model-backfill-arrivals`` a row that "arrived"
# more than a day after its run is stamped with a modelled arrival instead:
# AIFS at the same run's IFS PF50 arrival (both from ECMWF's open-data bucket,
# the AIFS research replay's own rule), AIGEFS at init + 8h11m (charter 212's
# latest measured arrival).  Forward-captured rows keep their real arrival.
MODEL_BACKFILL_ARRIVALS = False
BACKFILL_LAG = timedelta(hours=24)
AIGEFS_ARRIVAL_AFTER_INIT = timedelta(hours=8, minutes=11)
AIFS_FALLBACK_AFTER_INIT = timedelta(hours=8)


def capture_station(conn: sqlite3.Connection, station: str, tz: str):
    replay.FIRST_EVENT_DATE = FIRST_EVENT_DATE  # read by ``_arrivals``' floor
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
            if arrived - run > BACKFILL_LAG:
                if scope[0] == "NOAA_AIGEFS_025":
                    return run + AIGEFS_ARRIVAL_AFTER_INIT
                twin = fetched.get(("ECMWF_IFS_ENS_OPEN_PF50",) + key[1:])
                return twin if twin is not None and twin - run <= BACKFILL_LAG else run + AIFS_FALLBACK_AFTER_INIT
        return arrived

    forecasts = replay.Forecasts(cases_by_scope, first_commit, arrival_of)
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
    tables_memo: dict[str, dict] = {}
    out = []
    for event_date in sorted(arrivals):
        if event_date not in first_commit:
            continue
        day_end = datetime.fromisoformat(event_date).replace(tzinfo=zone) + timedelta(days=1)
        known = max(first_commit[event_date], day_end)
        events = sorted(arrivals[event_date], key=lambda e: (e[0], e[1], e[2]))
        packet: dict[str, tuple] = {}
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
            sigma_per_unit = radiation_shift_sigmas_for_lead(newest[2])
            signals = oos._holdout_conditioning_signals(
                conn, station_id=station, holdout_days=[event_date],
                predictions_by_system={s: {event_date: day_rows[s]} for s in pool},
                radiation_shift_sigmas=sigma_per_unit,
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
            scored = replay._blend_and_score(
                weights=seat, day_rows=day_rows, cooling_shift_sigma=cooling,
                latitude=latitude, tables=tables, actual=actual,
            )
            out.append({
                "station": station, "event_date": event_date, "tz": tz,
                "at": _iso(at), "truth_known_at": _iso(known), "lead": newest[2],
                "actual": actual, "pool": pool,
                "family": {s: family[s] for s in pool},
                "scopes": {s: [packet[s][2][2], packet[s][2][3]] for s in pool},
                "day_rows": {
                    s: {"c": round(float(day_rows[s]["calibrated_center_c"]), 6),
                        "p": {k: round(float(v), 8) for k, v in day_rows[s]["probability"].items() if v > 0.0}}
                    for s in pool
                },
                "cooling": cooling, "latitude": latitude, "hour_key": hour_key,
                "skill_rule": rule,
                "seat": {"w": scored["w"], "mean": scored["mean"], "served": scored["served"], "vec": scored["vec"],
                         "lam": scored["lam"]},
            })
    return out, tables_memo


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget-seconds", type=float, default=540.0)
    parser.add_argument("--stations", nargs="*", default=None)
    parser.add_argument("--out", type=Path, default=OUT,
                        help="pass-1 directory; the families seated are the contracts' as of the run")
    parser.add_argument("--model-backfill-arrivals", action="store_true")
    args = parser.parse_args()
    global MODEL_BACKFILL_ARRIVALS
    MODEL_BACKFILL_ARRIVALS = bool(args.model_backfill_arrivals)
    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    zones = dict(conn.execute(
        "SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding"
    ).fetchall())
    from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: PLC0415
    fleet = sorted(str(s) for s in coordinate_registry_manifest()["station_ids"])
    stations = args.stations or fleet
    started = time.time()
    done = skipped = 0
    for station in stations:
        target = out_dir / f"{station}.jsonl.gz"
        if target.exists():
            skipped += 1
            continue
        if time.time() - started > args.budget_seconds:
            break
        t0 = time.time()
        rows, tables = capture_station(conn, station, zones[station])
        tmp = target.with_suffix(".tmp")
        with gzip.open(tmp, "wt") as handle:
            for row in rows:
                handle.write(json.dumps(row, separators=(",", ":")) + "\n")
        with open(out_dir / f"{station}.tables.pkl", "wb") as handle:
            pickle.dump(tables, handle)
        tmp.replace(target)
        done += 1
        print(f"{station}: {len(rows)} updates, {len(tables)} table hours, {time.time() - t0:.0f}s", flush=True)
    remaining = sum(1 for s in stations if not (out_dir / f"{s}.jsonl.gz").exists())
    print(f"done {done}, already {skipped}, remaining {remaining}, {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
