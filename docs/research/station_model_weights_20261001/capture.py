#!/usr/bin/env python3
"""Pass 1: per station x system x decision, the production single-system
distribution (charter 216 rule, walk-forward, modelled AIFS/AIGEFS arrivals),
plus the charter-134 seat weights and the width layer serving would apply.

The replay is ``single_system_logloss_reg_20260924/capture_reg.py`` loaded from
source with three asserted text patches: production's own forecaster
(``replay.Forecasts`` -> ``oos._fit_parameters``, no research refits), event days
2026-07-20..2026-09-30, and the scoring block replaced by a dump.  Read-only.

    python3 capture.py --stations EGLC,KLGA
"""
from __future__ import annotations
import dataclasses, os, sys, types
from pathlib import Path
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if os.environ.get("PM_SINGLE_SYSTEM_FIT_RULE"):
    sys.exit("unset PM_SINGLE_SYSTEM_FIT_RULE: the capture must use production's single-system rule")
SOURCE = ROOT / "docs" / "research" / "single_system_logloss_reg_20260924" / "capture_reg.py"
text = SOURCE.read_text(encoding="utf-8")

def patch(old, new):
    global text
    assert text.count(old) == 1, old[:60]
    text = text.replace(old, new)

patch("forecasts = RegForecasts(cases_by_scope, first_commit, arrival_of)",
      "forecasts = replay.Forecasts(cases_by_scope, first_commit, arrival_of)")
start = text.index("            scored = {}\n")
end = text.index("    return out\n", start)
text = text[:start] + '''            layer = tables["center_width_layer"]
            out.append({
                "station": station, "event_date": event_date, "tz": tz,
                "at": C._iso(at), "truth_known_at": C._iso(known), "lead": newest[2],
                "actual": actual, "lat": latitude, "cooling": cooling,
                "family": {s: family[s] for s in pool},
                "seat": {s: round(v, 6) for s, v in seat.items()},
                "sys": {s: {
                    "p": {k: round(float(v), 7) for k, v in day_rows[s]["probability"].items() if float(v) >= 1e-7},
                    "c": round(float(day_rows[s]["calibrated_center_c"]), 4),
                    "lead": packet[s][2][2], "run": packet[s][1], "n": day_rows[s]["train_n"],
                    "es": round(float(day_rows[s]["error_scale"]), 4),
                } for s in pool},
                "cw": None if layer is None else dataclasses.asdict(layer),
            })
''' + text[end:]
R = types.ModuleType("capture_reg_station_model")
R.__file__ = str(SOURCE)
R.__dict__["dataclasses"] = dataclasses
exec(compile(text, str(SOURCE), "exec"), R.__dict__)
assert R.oos.SINGLE_SYSTEM_FIT_RULE == R.oos.SINGLE_SYSTEM_FIT_RULE_LOGLOSS
R.CUTOFF_UTC = "2026-10-01T09:00:00Z"
R.PERIODS["smw"] = lambda day: "2026-07-20" <= day <= "2026-09-30"
R.HERE = HERE                         # main() writes HERE / "pass1_smw"

if __name__ == "__main__":
    sys.argv += ["--period", "smw", "--model-backfill-arrivals", "--fast"]
    sys.exit(R.main())
