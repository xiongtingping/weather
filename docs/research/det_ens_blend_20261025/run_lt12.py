#!/usr/bin/env python3
"""PROTOCOL 补记一: A/B on the P0 decisions within 12 h of settlement (records_lt12.pkl).

    python3 docs/research/det_ens_blend_20261025/run_lt12.py
"""

from __future__ import annotations

import json
import statistics

import run as R

ARMS = ("A", "B")


def main() -> int:
    recs = R.load(R.HERE / "records_lt12.pkl")
    print("records", len(recs), "days", len({r["day"] for r in recs}), flush=True)
    days = sorted({r["day"] for r in recs})
    rows: dict[str, list[dict]] = {}
    path, warm = [], None
    for day in [d for d in days if d >= R.SCORE_FROM]:
        train = [r for r in recs if r["day"] < day]
        if len({r["day"] for r in train}) < R.MIN_TRAIN_DAYS:
            continue
        test = [r for r in recs if r["day"] == day and r["decision_at_utc"] >= R.SCORE_FROM]
        if not test:
            continue
        bias = R.fit_bias(train)
        Btr, Bte = R.Batch(train), R.Batch(test)
        Ftr, Ztr = R.det_features(train, bias)
        Fte, Zte = R.det_features(test, bias)
        theta = R.fit_arm("B", Btr, Ftr, Ztr, warm)
        warm = theta
        path.append({"day": day, "theta": [round(float(t), 4) for t in theta]})
        sa = Bte.scores(R.arm_vector("A", None, Bte, Fte, Zte))
        sb = Bte.scores(R.arm_vector("B", theta, Bte, Fte, Zte))
        for i, r in enumerate(test):
            row = {"day": day, "station": r["station"], "family": r["family"], "hours": r["hours"]}
            for sc in ("logscore", "brier", "crps"):
                row[f"A_{sc}"], row[f"B_{sc}"] = float(sa[sc][i]), float(sb[sc][i])
            rows.setdefault(day, []).append(row)
        print(day, len(test), round(float(sa["logscore"].mean()), 4), round(float(sb["logscore"].mean()), 4),
              path[-1]["theta"], flush=True)
    half = sorted(rows)[len(rows) // 2]
    slices = {
        "all": lambda r: True,
        "lt12_first": lambda r: r["family"] == "lt12_first",
        "lt12_last": lambda r: r["family"] == "lt12_last",
        "6_12h": lambda r: r["hours"] >= 6.0,
        "lt6h": lambda r: r["hours"] < 6.0,
        "first_half": lambda r: r["day"] < half,
        "second_half": lambda r: r["day"] >= half,
    }
    out = {"split_day": half, "B-A": {k: R.compare(rows, "B", "A", f) for k, f in slices.items()}}
    flat = [r for rs in rows.values() for r in rs]
    by_station: dict[str, list] = {}
    for r in flat:
        by_station.setdefault(r["station"], []).append(r["B_logscore"] - r["A_logscore"])
    out["stations_better"] = sum(1 for v in by_station.values() if statistics.mean(v) < 0)
    out["n_stations"] = len(by_station)
    out["hours_by_slice"] = {"6_12h": sum(1 for r in flat if r["hours"] >= 6), "lt6h": sum(1 for r in flat if r["hours"] < 6)}
    out["params_path"] = path
    (R.HERE / "results_lt12.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    for k, v in out["B-A"].items():
        print(k, "LS", v["logscore"], "CRPS", v["crps"]["delta"], v["crps"]["ci"], "Br", v["brier"]["delta"],
              v["brier"]["ci"], v["n_days"], v["days_better"], v["n"], v["verdict"])
    print("stations", out["stations_better"], "/", out["n_stations"], out["hours_by_slice"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
