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

OUT = e.ROOT / "config" / "ui" / "tmax_composite_shift_v1.json"
HOURS = tuple(range(9, 18))


def main() -> int:
    e.CHECK_HOURS = HOURS
    rows = json.loads(e.DAYS.read_text())
    D = e.Data(rows)
    m = np.ones(D.n, bool)
    cphys = e.fit_phys(D, m)
    by_hour = {}
    for H in HOURS:
        fm = e.fit_linear(D, m, H, cphys, True)
        fleet_coef = e.fit_linear(D, m, H, cphys, False)["coef"][0]
        stations = {}
        for i, s in enumerate(D.stations):
            stations[s] = {"icpt_c": round(float(fm["icpt"][i]), 4),
                           "coef": [round(float(c), 6) for c in fm["coef"][i]],
                           "resid_sd_c": round(float(fm["sd"][i]), 3)}
        by_hour[str(H)] = {"stations": stations, "fleet_coef": [round(float(c), 6) for c in fleet_coef],
                           "fleet_resid_sd_c": round(float(fm["sd"][-1]), 3)}
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
            "rain": "hours 06..H whose report carries precipitation",
            "phys": "c_station * (sqrt(Q measured 06..H + day-ahead H+1..15) - sqrt(Q day-ahead 06..15)), Q in MJ/m2",
        },
        "phys": {"c": {s: round(float(cphys[i]), 4) for i, s in enumerate(D.stations)},
                 "fleet_c": round(float(cphys[-1]), 4), "first_hour": 6, "peak_hour": 15},
        "verdict": {"versus_sun_line_logscore_13h": -0.337, "versus_rh_line_logscore_13h": -0.255,
                    "mae_13h": {"mean_only": 1.207, "sun_line": 1.149, "composite": 0.788},
                    "note": "judged at 9/11/13/15/17 h; physics core adds ~0 before 15 h; the gain is tdev"},
        "by_hour": by_hour,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print("written", OUT, len(D.stations), "stations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
