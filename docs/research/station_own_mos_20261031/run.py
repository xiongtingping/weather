"""Per-station MOS (user 2026-10-31 「所有模型，按照单站点重新优化，重新拟合参数，也就是每个站点一套自己的」, MOS after F and P0).

The board's composite ΔT card (``docs/research/tmax_physics_m3_eval_20261021``,
arm ⑥ ``AB_station``) already fits per station: its own intercept, and its
coefficients, physics constant and residual sd each shrunk to the fleet's with
weight n / (n + K), K = 100 station-days.  This walks M3's own walk-forward
(120-day warm-up, refit weekly, every fit on the days before) with K replaced:
K = 0 is each station alone, larger K leans on the fleet; ``AB`` (one fleet
set, own intercept) is kept for reference.  Scores are M3's (Gaussian LogScore,
CRPS, MAE on Tmax − day-ahead), at the five checked hours.

    .venv/bin/python -I docs/research/station_own_mos_20261031/run.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
M3 = ROOT / "docs" / "research" / "tmax_physics_m3_eval_20261021"
sys.path.insert(0, str(M3))
import evaluate as e  # noqa: E402

KS = (0.0, 10.0, 30.0, 100.0, 300.0)
BASE_K = 100.0
DRAWS, SEED = 2000, 20261031


def boot(diff_by_day: dict[int, list[float]]) -> tuple[float, float]:
    days = sorted(diff_by_day)
    sums = np.array([sum(diff_by_day[d]) for d in days])
    cnts = np.array([len(diff_by_day[d]) for d in days])
    rng = np.random.default_rng(SEED)
    picks = rng.integers(0, len(days), (DRAWS, len(days)))
    stats = sums[picks].sum(1) / cnts[picks].sum(1)
    return float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5))


def main() -> None:
    rows = json.loads(e.DAYS.read_text())
    D = e.Data(rows)
    print(f"store {D.n} station-days, {len(D.stations)} stations, {D.dates[0]}..{D.dates[-1]}", flush=True)
    runs = {}
    for k in KS:
        e.K = k
        res = e.walk_forward(D)
        runs[f"K{int(k)}"] = {H: (res[H]["rows"], res[H]["AB_station"]) for H in e.CHECK_HOURS}
        if k == BASE_K:
            runs["fleet_AB"] = {H: (res[H]["rows"], res[H]["AB"]) for H in e.CHECK_HOURS}
        print("walked K", k, flush=True)
    base = f"K{int(BASE_K)}"
    out = {"store": [D.dates[0], D.dates[-1], D.n], "hours": {}}
    for H in e.CHECK_HOURS:
        rows_h = runs[base][H][0]
        y = D.y[rows_h]
        day = D.day[rows_h]
        sc = {name: e.scores(y, *r[H][1]) for name, r in runs.items()}
        print(f"\n{H} h  ({rows_h.size} station-days, {len(set(day))} days)")
        print(f"  {'arm':9s} {'LogScore':>8s} {'CRPS':>6s} {'MAE':>6s}  ΔLogScore vs {base} [95%] | ΔCRPS [95%]")
        out["hours"][H] = {}
        for name, s in sc.items():
            line = f"  {name:9s} {s[:, 0].mean():8.4f} {s[:, 1].mean():6.4f} {s[:, 2].mean():6.4f}"
            entry = {"logscore": float(s[:, 0].mean()), "crps": float(s[:, 1].mean()), "mae": float(s[:, 2].mean())}
            if name != base:
                for j, label in ((0, "logscore"), (1, "crps")):
                    d = s[:, j] - sc[base][:, j]
                    per: dict[int, list[float]] = {}
                    for dd, v in zip(day, d):
                        per.setdefault(int(dd), []).append(float(v))
                    lo, hi = boot(per)
                    entry[f"d_{label}"] = [float(d.mean()), lo, hi]
                    line += f"  {d.mean():+.4f} [{lo:+.4f}, {hi:+.4f}]" if j == 0 else f" | {d.mean():+.4f} [{lo:+.4f}, {hi:+.4f}]"
                # Stations better than the base on LogScore.
                st = D.st[rows_h]
                better = sum(1 for i in np.unique(st) if s[st == i, 0].mean() < sc[base][st == i, 0].mean())
                entry["stations_better"] = [better, int(np.unique(st).size)]
                line += f"  better at {better}/{np.unique(st).size} stations"
            out["hours"][H][name] = entry
            print(line)
    (HERE / "reading.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
