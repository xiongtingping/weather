"""The board's composite ΔT card (user 2026-10-21 「用分站的合成 ΔT 卡……分项显示
气温差、湿度、日照等贡献，只上看板」, then 「别替换，增加」): arm ⑥ of the M3
protocol (A+B per station, k=100) refitted once on the whole store, one line
per last-measured hour 9..17 (the judgement read the odd hours; the even
ones are the same fit).  Writes config/ui/tmax_composite_shift_v1.json.
Display only -- never P0/P0-M/P1, a lane or an order.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import evaluate as e  # noqa: E402
import m4_eval  # noqa: E402
from m8_eval import rmax_column  # noqa: E402

# M8 (reading_m8_20261021T024257Z): after 17 h the day's running maximum
# beat arm 6 at every hour (MAE 0.90-1.03 -> 0.12-0.15 C); M9
# (reading_m9_20261021T025156Z) at 12..17 h as well (MAE 0.84 -> 0.75 at 12 h,
# 0.82 -> 0.18 at 17 h).
RMAX_HOURS = tuple(range(12, 24))

OUT = e.ROOT / "config" / "ui" / "tmax_composite_shift_v1.json"
RUNTIME_DIR = e.ROOT / "data" / "runtime" / "tmax_composite"
# 9..17 from M3; 0..8 from M6 (reading_m6_20261021T022437Z): every hour but 6
# beat the station-bias mean out of sample, so 6 h is left off.
# 18..23 from M7 (reading_m7_20261021T023523Z): every hour passed.
# 6 h from M10 (reading_m10_20261021T030008Z): as night (sw = phys = 0) it passed.
HOURS = tuple(range(0, 9)) + tuple(range(9, 18)) + tuple(range(18, 24))


def build(rows: list[dict]) -> dict:
    """One fit of the card on ``rows`` (the store, or the store less the gate's held-out days)."""

    e.CHECK_HOURS = HOURS
    D = e.Data(rows)
    m = np.ones(D.n, bool)
    cphys = e.fit_phys(D, m)
    by_hour = {}
    for H in HOURS:
        base = D.F[H]
        if H in RMAX_HOURS:
            D.F[H] = np.column_stack([base, rmax_column(rows, H)])
        fm = e.fit_linear(D, m, H, cphys, True)
        # M4 (reading_m4_20261021T020156Z): Student-t beat the normal at every
        # checkpoint; its degrees of freedom on this fit's standardised residuals.
        X = np.column_stack([D.F[H], cphys[D.st] * D.G[H]])
        mu = fm["icpt"][D.st] + np.einsum("ij,ij->i", X, fm["coef"][D.st])
        t_df = m4_eval.fit_df((D.y - mu) / fm["sd"][D.st])
        fleet_coef = e.fit_linear(D, m, H, cphys, False)["coef"][0]
        D.F[H] = base
        stations = {}
        for i, s in enumerate(D.stations):
            stations[s] = {"icpt_c": round(float(fm["icpt"][i]), 4),
                           "coef": [round(float(c), 6) for c in fm["coef"][i]],
                           "resid_sd_c": round(float(fm["sd"][i]), 3)}
        by_hour[str(H)] = {"stations": stations, "fleet_coef": [round(float(c), 6) for c in fleet_coef],
                           "fleet_resid_sd_c": round(float(fm["sd"][-1]), 3), "t_df": t_df,
                           **({"features": list(e.B_FEATS) + ["rmax", "phys"]} if H in RMAX_HOURS else {})}
    out = {
        "schema": "tmax_composite_shift_v1",
        "fitted_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": [D.dates[0], D.dates[-1]], "station_days": D.n,
        "lineage": "docs/research/tmax_physics_m3_eval_20261021 (protocol c30cb32a+40f14290, judgement d344a979); "
                   "IEM/Open-Meteo aggregator rows, display only",
        "y": "settled Tmax - day-ahead forecast Tmax (Open-Meteo previous_day1), per station intercept included",
        "features": list(e.B_FEATS) + ["phys"],
        "feature_defs": {
            "sw": "mean over stamps 06..H of (measured - day-ahead) shortwave, W/m2, clipped +-300",
            "rh": "report - day-ahead humidity at the newest hour <= H with both, percentage points, clipped +-20",
            "dew": "report - day-ahead dew point, newest hour <= H with both, C",
            "wind": "report - day-ahead 10 m wind, newest hour <= H with both, m/s",
            "tdev": "report - day-ahead temperature, newest hour <= H with both, C",
            "rmax": "12..23 h only (M8, M9): the max of the hourly reports at local 00..H minus the day-ahead maximum, C",
            "rain": "hours 06..H whose report carries precipitation",
            "phys": "c_station * (sqrt(Q measured 06..H + day-ahead H+1..15) - sqrt(Q day-ahead 06..15)), Q in MJ/m2",
        },
        "phys": {"c": {s: round(float(cphys[i]), 4) for i, s in enumerate(D.stations)},
                 "fleet_c": round(float(cphys[-1]), 4), "first_hour": 6, "peak_hour": 15},
        "verdict": {"versus_sun_line_logscore_13h": -0.337, "versus_rh_line_logscore_13h": -0.255,
                    "mae_13h": {"mean_only": 1.207, "sun_line": 1.149, "composite": 0.788},
                    "note": "judged at 9/11/13/15/17 h; physics core adds ~0 before 15 h; the gain is tdev"},
        "distribution": {"shape": "student_t", "scale": "sd*sqrt((df-2)/df) so the variance is resid_sd^2",
                         "verdict": "M4 reading_m4_20261021T020156Z: t beat normal at 9-17 h (bucket LogScore -0.008..-0.025); "
                                    "t intervals cover 47.5-49.2/77.7-79.1/93.9-94.6% of 50/80/95%",
                         "buckets": "C stations 1 C rounded; F stations 2 F from an even lower bound"},
        "by_hour": by_hour,
    }
    out["version"] = out["fitted_at_utc"].replace(":", "").replace("-", "")
    return out


def main() -> int:
    import argparse  # noqa: PLC0415

    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", action="store_true",
                    help="weekly refit: write data/runtime/tmax_composite/ (the board reads it first) and keep a copy per version")
    args = ap.parse_args()
    rows = json.loads(e.DAYS.read_text())
    out = build(rows)
    if args.runtime:
        # Admission (user 2026-10-28 「加准入门」): the new fit replaces the card only when the
        # procedure, fitted without the newest days, is not worse on them than the card in use.
        import refit_gate  # noqa: PLC0415

        verdict = refit_gate.judge(rows, out, build)
        if not verdict["admitted"]:
            print("refit not admitted:", verdict["reason"], "-- the card in use is kept")
            return 0
        out["admission"] = verdict["summary"]
    text = json.dumps(out, ensure_ascii=False, indent=1)
    if args.runtime:
        (RUNTIME_DIR / "versions").mkdir(parents=True, exist_ok=True)
        (RUNTIME_DIR / "versions" / f"tmax_composite_shift_{out['version']}.json").write_text(text)
        tmp = RUNTIME_DIR / "tmax_composite_shift_v1.json.tmp"
        tmp.write_text(text)
        tmp.replace(RUNTIME_DIR / "tmax_composite_shift_v1.json")
        print("written", RUNTIME_DIR, out["version"], len(out["phys"]["c"]), "stations")
    else:
        OUT.write_text(text)
        print("written", OUT, len(out["phys"]["c"]), "stations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
