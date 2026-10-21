"""M7 evaluator (PROTOCOL_M7.md, committed 7f3da223 before any reading):
the composite at local 18..23 h, arm 6 against the station-bias mean, walk-forward
as M3.

    --smoke   counts and finite predictions only
    (no flag) the single judgement run: writes reading_m7_<UTC>.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import evaluate as e  # noqa: E402

e.CHECK_HOURS = tuple(range(18, 24))
e.PAIRS = {"AB_station-mean": ("AB_station", "mean"), "AB_station-AB": ("AB_station", "AB")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    rows = json.loads(e.DAYS.read_text())
    D = e.Data(rows)
    res = e.walk_forward(D)
    if args.smoke:
        print(json.dumps({"predictions": int(sum(res[H]["rows"].size for H in e.CHECK_HOURS)),
                          "finite": bool(all(np.isfinite(res[H][a][i]).all() for H in e.CHECK_HOURS
                                             for a in e.ARMS for i in (0, 1))), "note": "smoke only -- no scores printed"}))
        return 0
    summary = e.summarise(D, res)
    reading = {"schema": "tmax_physics_m7_reading_v1", "protocol": "7f3da223",
               "read_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "metrics": ["LogScore", "CRPS", "MAE"], "summary": summary}
    stamp = reading["read_at_utc"].replace(":", "").replace("-", "")
    (HERE / f"reading_m7_{stamp}.json").write_text(json.dumps(reading, indent=1))
    print("written", f"reading_m7_{stamp}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
