"""Selection-on-certification bias in the P0 subset contest (review R2).

For every enumerating contest a real OOS build runs, score two winners on the
certification days C (the cohort's common holdout):

  A  the winner the contest picks ON C          -- what the gate certifies today
  B  the winner the same contest picks on S, the three shared walk-forward
     days just before C, scored on C           -- a nested, honest pick

Everything else is the production code path: the build, the cohort views, the
contest function, the shape stack and the gate rule are called, never copied.
Read-only SSOT connection; one JSON file per station so the run resumes.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path("/Users/xiong/weather")
sys.path.insert(0, str(ROOT))

from bayes_basket_target import native_ensemble_oos as oos  # noqa: E402

BUDGET_SEC = float(sys.argv[1]) if len(sys.argv) > 1 else 520.0
AS_OF = sys.argv[2] if len(sys.argv) > 2 else "2026-09-13T15:00:00Z"
# The latest origin keeps its original directory; replays get one each.
OUT = Path(__file__).resolve().parent / (
    "stations" if AS_OF == "2026-09-13T15:00:00Z"
    else "stations_" + AS_OF[:13].replace("-", "").replace("T", "T")
)
OUT.mkdir(exist_ok=True)

real_contest = oos._select_minimum_crps_subset
real_view = oos._cohort_view
real_signals = oos._holdout_conditioning_signals
real_gate = oos._cross_oos_gate

state: dict = {"evaluations": {}, "signals": None, "records": [], "pending": []}


def view_spy(evaluation, common_holdout):
    state["evaluations"][str(evaluation["system_id"])] = evaluation
    return real_view(evaluation, common_holdout)


def signals_spy(conn, **kwargs):
    state["signals"] = (conn, dict(kwargs))
    return real_signals(conn, **kwargs)


def _metrics(receipt):
    winner = receipt["winner"]
    return {k: float(winner[k]) for k in ("crps", "nll", "brier")}


def contest_spy(**kwargs):
    weights, receipt = real_contest(**kwargs)
    if kwargs.get("pinned_subset") is not None:
        return weights, receipt
    holdout = [str(d) for d in kwargs["common_holdout"]]
    ready = sorted(kwargs["ready_system_ids"])
    record = {
        "holdout_c": holdout,
        "ready": ready,
        "evaluated_subset_count": receipt["evaluated_subset_count"],
        "a_ids": sorted(weights),
        "a_on_c": _metrics(receipt),
    }
    try:
        members = [state["evaluations"][s] for s in ready]
        exposed = [
            {str(r["event_date"]): r for r in (e.get("cross_predictions") or [])}
            for e in members
        ]
        common = sorted(
            set.intersection(*(set(x) for x in exposed)) if exposed else set()
        )
        before = [d for d in common if d < holdout[0]]
        need = len(holdout)
        if len(before) < need:
            record["state"] = "NO_SELECTION_WINDOW"
            record["selection_days_available"] = len(before)
        else:
            s_days = before[-need:]
            s_preds = {
                s: {d: exposed[i][d] for d in s_days}
                for i, s in enumerate(ready)
            }
            conn, sig_kwargs = state["signals"]
            s_signals = real_signals(
                conn,
                station_id=sig_kwargs["station_id"],
                holdout_days=s_days,
                predictions_by_system=s_preds,
                radiation_shift_sigma=sig_kwargs["radiation_shift_sigma"],
            )
            b_kwargs = dict(kwargs)
            b_kwargs.update(
                predictions_by_system=s_preds,
                common_holdout=s_days,
                conditioning_by_day=s_signals,
            )
            b_weights, b_receipt = real_contest(**b_kwargs)
            b_ids = sorted(b_weights)
            _w, b_on_c = real_contest(**{**kwargs, "pinned_subset": b_ids})
            record.update(
                state="SCORED",
                holdout_s=s_days,
                b_ids=b_ids,
                b_on_s=_metrics(b_receipt),
                b_on_c=_metrics(b_on_c),
            )
            full_families = {kwargs["family_by_system"][s] for s in ready}
            if len(full_families) >= 2:
                try:
                    _w, full = real_contest(**{**kwargs, "pinned_subset": ready})
                    record["full_on_c"] = _metrics(full)
                except Exception as exc:  # noqa: BLE001
                    record["full_error"] = str(exc)[:120]
    except Exception as exc:  # noqa: BLE001
        record["state"] = "ERROR"
        record["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    state["pending"].append(record)
    state["records"].append(record)
    return weights, receipt


def gate_spy(cross_metrics, climate_metrics):
    result = real_gate(cross_metrics, climate_metrics)
    for record in reversed(state["pending"]):
        if abs(record["a_on_c"]["crps"] - float(cross_metrics["crps"])) < 1e-12:
            record["climate_on_c"] = {k: float(v) for k, v in climate_metrics.items()}
            record["gate_a"] = bool(result["pass"])
            if "b_on_c" in record:
                record["gate_b"] = bool(real_gate(record["b_on_c"], climate_metrics)["pass"])
            state["pending"].remove(record)
            break
    return result


oos._select_minimum_crps_subset = contest_spy
oos._cohort_view = view_spy
oos._holdout_conditioning_signals = signals_spy
oos._cross_oos_gate = gate_spy

conn = sqlite3.connect(
    f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60
)
conn.execute("PRAGMA query_only=1")
stations = sorted({
    str(r[0]) for r in conn.execute(
        "SELECT DISTINCT station_id FROM basket_probability_envelope"
        " WHERE stage='P0' AND state='COMMITTED'"
        " AND decision_at_utc>='2026-09-12T00:00:00Z'"
    )
})
print("as_of", AS_OF, "out", OUT.name, flush=True)
started = time.time()
done = 0
for station in stations:
    path = OUT / f"{station}.json"
    if path.exists():
        continue
    if time.time() - started > BUDGET_SEC:
        break
    state.update(evaluations={}, signals=None, records=[], pending=[])
    t0 = time.time()
    try:
        # The resident's own profile (production_worker, forward
        # accumulation): 5 training days, 3 holdout days.
        oos.build_native_ensemble_oos(
            conn, station_id=station, as_of_utc=AS_OF,
            tables_as_of_utc=AS_OF,
            min_train_days=oos.FORWARD_ACCUMULATION_MIN_TRAIN_DAYS,
            min_holdout_days=oos.FORWARD_ACCUMULATION_MIN_HOLDOUT_DAYS,
        )
        error = None
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {str(exc)[:200]}"
    path.write_text(json.dumps({
        "station_id": station, "as_of_utc": AS_OF, "build_error": error,
        "seconds": round(time.time() - t0, 1), "records": state["records"],
    }, sort_keys=True))
    done += 1
    print(station, len(state["records"]), error, round(time.time() - t0, 1), flush=True)
remaining = [s for s in stations if not (OUT / f"{s}.json").exists()]
print("done_this_run", done, "remaining", len(remaining), "of", len(stations))
