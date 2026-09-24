#!/usr/bin/env python3
"""Aggregate measure.py runs into results.json.  See PROTOCOL.md.

A: the switch-time verdicts at one as-of, both grids, as the builds
   returned them (window history = the integer day scores on record).
B: the steady state -- each grid's own day scores over the as-of runs,
   judged by the production ``_stable_pool_window_gate``.
"""
from __future__ import annotations

import gzip
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import native_ensemble_oos as O  # noqa: E402

RUNS = HERE / "runs"
F_STATIONS = {"KATL", "KAUS", "KBKF", "KDAL", "KHOU", "KLAX", "KLGA", "KMIA",
              "KORD", "KSEA", "KSFO"}
SWITCH_AS_OF = "2026-09-24T02:30:00Z"


def load(as_of: str, grid: str) -> dict[str, dict]:
    path = RUNS / f"{as_of.replace(':', '')}_{grid}.jsonl.gz"
    if not path.exists():
        return {}
    out = {}
    with gzip.open(path, "rt") as handle:
        for line in handle:
            record = json.loads(line)
            out[record["station"]] = record
    return out


def keyed(records: dict[str, dict]) -> dict[tuple, dict]:
    out = {}
    for record in records.values():
        for cand in record["candidates"]:
            key = tuple(json.dumps(part, sort_keys=True) for part in cand["key"])
            out[key] = cand
    return out


def flip_table(a: dict[tuple, dict], b: dict[tuple, dict], verdict) -> dict:
    common = sorted(set(a) & set(b))
    counts = Counter()
    by_station = defaultdict(Counter)
    by_unit = defaultdict(Counter)
    lost_scopes = defaultdict(lambda: [0, 0])  # (station, lead) -> [pass_int, pass_lad]
    lost_cycles = defaultdict(lambda: [0, 0])  # (station, lead, cycle)
    for key in common:
        va, vb = verdict(a[key]), verdict(b[key])
        station = json.loads(key[0])
        unit = "F" if station in F_STATIONS else "C"
        label = {(True, True): "pass_both", (False, False): "reject_both",
                 (True, False): "pass_to_reject", (False, True): "reject_to_pass"}[(va, vb)]
        counts[label] += 1
        by_unit[unit][label] += 1
        if va != vb:
            by_station[station][label] += 1
        scope = (station, json.loads(key[2]))
        lost_scopes[scope][0] += int(va)
        lost_scopes[scope][1] += int(vb)
        cycle = (station, json.loads(key[2]), json.loads(key[3]))
        lost_cycles[cycle][0] += int(va)
        lost_cycles[cycle][1] += int(vb)
    dark = sorted(f"{s}|{lead}" for (s, lead), (pi, pl) in lost_scopes.items() if pi > 0 and pl == 0)
    lit = sorted(f"{s}|{lead}" for (s, lead), (pi, pl) in lost_scopes.items() if pi == 0 and pl > 0)
    return {
        "keys_common": len(common),
        "keys_only_integer": len(set(a) - set(b)),
        "keys_only_ladder": len(set(b) - set(a)),
        "counts": dict(counts),
        "by_unit": {u: dict(c) for u, c in by_unit.items()},
        "flips_by_station": {s: dict(c) for s, c in sorted(by_station.items())},
        "station_lead_scopes_going_dark": dark,
        "station_lead_scopes_lighting_up": lit,
        "station_lead_cycle_scopes_going_dark": sorted(
            "|".join(k) for k, (pi, pl) in lost_cycles.items() if pi > 0 and pl == 0),
        "station_lead_cycle_scopes_lighting_up": sorted(
            "|".join(k) for k, (pi, pl) in lost_cycles.items() if pi == 0 and pl > 0),
        "station_lead_cycle_scope_count": len(lost_cycles),
    }


def mean_scores(cands: dict[tuple, dict]) -> dict:
    out = {}
    for unit in ("C", "F"):
        rows = [c for k, c in cands.items() if (json.loads(k[0]) in F_STATIONS) == (unit == "F")]
        if not rows:
            continue
        out[unit] = {
            "keys": len(rows),
            **{f"learned_{m}": sum(r["oos_metrics"][m] for r in rows) / len(rows) for m in ("brier", "nll", "crps")},
            **{f"climate_{m}": sum(r["climate_metrics"][m] for r in rows) / len(rows) for m in ("brier", "nll", "crps")},
        }
    return out


def ladder_coverage(cands: dict[tuple, dict]) -> dict:
    grids = Counter(day["grid"] for c in cands.values() for day in c["daily"])
    return dict(grids)


def steady_state(grid: str, as_ofs: list[str]) -> dict[tuple, dict]:
    """Re-judge the switch-time keys on this grid's own day scores."""

    runs = [(as_of, keyed(load(as_of, grid))) for as_of in as_ofs]
    runs = [(as_of, cands) for as_of, cands in runs if cands]
    final_as_of, final = runs[-1]
    history_runs = runs[:-1]
    out = {}
    for key, cand in final.items():
        history: dict[str, tuple] = {}
        for _as_of, cands in history_runs:  # oldest first; newer overrides
            earlier = cands.get(key)
            if earlier is None:
                continue
            for day in earlier["daily"]:
                history[day["event_date"]] = (day["learned"], day["climate"])
        own_daily = [
            {"event_date": d["event_date"], "learned": d["learned"], "climate": d["climate"]}
            for d in cand["daily"]
        ]
        gate = O._stable_pool_window_gate(
            own_learned_metrics=cand["oos_metrics"],
            own_climate_metrics=cand["climate_metrics"],
            own_daily_scores=own_daily,
            history=history,
            holdout_end_date=cand["holdout_end_date"],
        )
        out[key] = {**cand, "steady_pass": bool(gate["pass"]),
                    "steady_path": gate["path"], "steady_window_days": gate["window_day_count"]}
    return out


def main() -> None:
    results = {"protocol": "PROTOCOL.md"}
    a_int = keyed(load(SWITCH_AS_OF, O.HOLDOUT_GRID_INTEGER_C))
    a_lad = keyed(load(SWITCH_AS_OF, O.HOLDOUT_GRID_MARKET_LADDER))
    errors = {grid: [s for s, r in load(SWITCH_AS_OF, grid).items() if r["state"] != "ok"]
              for grid in O.HOLDOUT_GRIDS}
    results["A_switch_time"] = {
        "as_of": SWITCH_AS_OF,
        "stations": {grid: len(load(SWITCH_AS_OF, grid)) for grid in O.HOLDOUT_GRIDS},
        "errors": errors,
        "flips": flip_table(a_int, a_lad, lambda c: c["pass"]),
        "own_three_day_flips": flip_table(a_int, a_lad, lambda c: c["own_pass"]),
        "mean_scores_common_keys": {
            "integer_c": mean_scores({k: v for k, v in a_int.items() if k in a_lad}),
            "market_ladder": mean_scores({k: v for k, v in a_lad.items() if k in a_int}),
        },
        "ladder_day_coverage": ladder_coverage(a_lad),
    }
    as_ofs = sorted({p.name.split("_")[0] for p in RUNS.glob("*.jsonl.gz")})
    as_ofs_iso = [f"{x[:13]}:{x[13:15]}:{x[15:]}" for x in as_ofs]
    # Only as-ofs whose runs cover all 45 stations on both grids: a partial
    # run would read as a window with holes it does not have.
    complete = [a for a in as_ofs_iso
                if all(len(load(a, g)) >= 45 for g in O.HOLDOUT_GRIDS)]
    results["B_incomplete_as_ofs"] = [a for a in as_ofs_iso if a not in complete]
    as_ofs_iso = complete
    if len(as_ofs_iso) > 1:
        s_int = steady_state(O.HOLDOUT_GRID_INTEGER_C, as_ofs_iso)
        s_lad = steady_state(O.HOLDOUT_GRID_MARKET_LADDER, as_ofs_iso)
        results["B_steady_state"] = {
            "as_ofs": as_ofs_iso,
            "flips": flip_table(s_int, s_lad, lambda c: c["steady_pass"]),
            "window_days_ladder": dict(Counter(c["steady_window_days"] for c in s_lad.values())),
        }
    (HERE / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    print(json.dumps(results, indent=1, ensure_ascii=False)[:6000])


if __name__ == "__main__":
    main()
