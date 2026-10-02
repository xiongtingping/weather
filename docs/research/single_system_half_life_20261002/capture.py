#!/usr/bin/env python3
"""ARM=CUR|B3|B8|B12|L8|L30 : six-system walk-forward, event days 2026-07-15..09-30,
production single-system rule with one constant changed, physics signal passed the decision time."""
import os, sys, types
from pathlib import Path
HERE = Path(__file__).resolve().parent; ROOT = HERE.parents[2]
SMW = ROOT / "docs/research/station_model_weights_20261001"
ARM = os.environ["ARM"]
os.environ["SMW_ALL"] = "1"
src = (SMW / "capture_long.py").read_text()
# the dump patch inside capture_long already writes per-system rows; additionally pass the decision instant to the shift signal
inner_old = '            signals = oos._holdout_conditioning_signals('
assert src.count("R.PERIODS[PERIOD]") == 1
FIRST_DAY = "2026-07-15" if ARM == "CUR" else "2026-08-15"
src = src.replace('R.PERIODS[PERIOD] = lambda day: "2026-01-01" <= day <= "2026-09-30"',
                  f'R.PERIODS[PERIOD] = lambda day: "{FIRST_DAY}" <= day <= "2026-09-30"')
src = src.replace('PERIOD = "all" if ALL else "long"', f'PERIOD = "sc_{ARM}"')
src = src.replace('R.HERE = HERE', 'R.HERE = Path(os.environ["SC_OUT"])')
# capture_long builds its exec'd text with patch(); add one more patch for the decision instant
src = src.replace('patch("        rows = capture_station(conn, station, zones[station])",',
    '''patch(\'''' + inner_old + '''\',
      "            for _s in pool: day_rows[_s][\\"decision_at_utc\\"] = C._iso(at)\\n''' + inner_old.replace('"', '\\"') + '''")
patch("        rows = capture_station(conn, station, zones[station])",''')
src = src.replace('if __name__ == "__main__":', '''HL = {"CUR": (5.0, 15.0), "B3": (3.0, 15.0), "B8": (8.0, 15.0), "B12": (12.0, 15.0), "L8": (5.0, 8.0), "L30": (5.0, 30.0)}[os.environ["ARM"]]
R.oos.BIAS_HALF_LIFE_DAYS, R.oos.LOGLOSS_REFIT_HALF_LIFE_DAYS = HL
if __name__ == "__main__":
    print("arm", os.environ["ARM"], "bias half-life", R.oos.BIAS_HALF_LIFE_DAYS, "logloss half-life", R.oos.LOGLOSS_REFIT_HALF_LIFE_DAYS, flush=True)''')
os.environ["SC_OUT"] = str(HERE)
sys.argv = [str(SMW / "capture_long.py")] + sys.argv[1:]
exec(compile(src, str(SMW / "capture_long.py"), "exec"), {"__name__": "__main__", "__file__": str(SMW / "capture_long.py")})
