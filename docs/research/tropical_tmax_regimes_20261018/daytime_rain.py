"""The rain-timing rule counted from daytime only (user 2026-10-18 「热带规律卡：
只统计白天开始的降雨，夜里的残雨不算"10 点前下雨"，先用研究数据重算比例再改」).

The day's first rain / thunder report is taken at or after a cutoff hour; a
report before it (the night's leftover shower) does not count.  Rain still
falling at the cutoff counts from the cutoff.  Same days, same classes and the
same year-block bootstrap as analyze.py; cutoff 0 reproduces its table.

    uv run python docs/research/tropical_tmax_regimes_20261018/daytime_rain.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import build  # noqa: E402
from analyze import line, rate  # noqa: E402

CUTOFFS = (0, 5, 6, 7)
CATS = (("10 点前", lambda h: h is not None and h < 10), ("10–13 点", lambda h: h is not None and 10 <= h < 13),
        ("13–16 点", lambda h: h is not None and 13 <= h < 16), ("16 点后", lambda h: h is not None and h >= 16),
        ("全天无", lambda h: h is None))


def main() -> None:
    days = json.loads((HERE / "days.json").read_text())
    for st in ("WSSS", "WMKK"):
        by = defaultdict(list)
        for o in build.load(st):
            if o["rain"] or o["ts"]:
                by[o["at"].date()].append(o["at"].hour + o["at"].minute / 60)
        R = [r for r in days if r["station"] == st]
        base = (rate(R, "cool"), rate(R, "warm"))
        print(f"\n# {st}  {len(R)} 天  降温日 {100 * base[0]:.0f}%  突破日 {100 * base[1]:.0f}%")
        for cut in CUTOFFS:
            for r in R:
                r["h"] = min((h for h in by.get(date.fromisoformat(r["date"]), []) if h >= cut), default=None)
            night_only = sum(1 for r in R if r["first_rain_h"] is not None and r["first_rain_h"] < cut and
                             (r["h"] is None or r["h"] >= 10))
            print(f"\n## 自 {cut:02d}:00 起算（夜雨不算而改档的 10 点前天数 {night_only}）")
            for name, f in CATS:
                print(line(f"降水 {name}", [r for r in R if f(r["h"])], base))


if __name__ == "__main__":
    main()
