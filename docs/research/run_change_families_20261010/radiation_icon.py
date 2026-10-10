#!/usr/bin/env python3
"""Addendum 2 (cont.): the other session's ICON / ECMWF hourly sunshine, on every station-day."""
from __future__ import annotations
import json, math, statistics, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
HERE = Path(__file__).resolve().parent
src = (HERE / "radiation_condition.py").read_text()
head = src[: src.index("ssot = sqlite3.connect")]
ns = {"__file__": str(HERE / "radiation_condition.py"), "__name__": "rc"}
exec(compile(head, "radiation_condition.py", "exec"), ns)
tail_src = src[src.index("def fresh("): src.index("rows = []")] + src[src.index("def score("): src.index("B0 = lambda")]
exec(compile(tail_src, "radiation_condition.py", "exec"), ns)
by_sd, changes, at, floored, argmax_low, logit, fresh = (ns[k] for k in ("by_sd", "changes", "at", "floored", "argmax_low", "logit", "fresh"))
SW = json.load(open("/private/tmp/claude-501/-Users-xiong-weather/8e8e0e0f-2636-45a0-a0af-fe34e79a733b/scratchpad/bench/prev_sw.json"))
day_sum = {}
for key, v in SW.items():
    if "time" not in v:
        continue
    st, model = key.split("|")
    acc = defaultdict(lambda: [0.0, 0.0, 0, 0])
    for t, a, b in zip(v["time"], v["d1"], v["d2"]):
        if 9 <= int(t[11:13]) <= 16:
            if a is not None: acc[t[:10]][0] += a; acc[t[:10]][2] += 1
            if b is not None: acc[t[:10]][1] += b; acc[t[:10]][3] += 1
    day_sum[(st, model)] = {d: (x[0] if x[2] == 8 else None, x[1] if x[3] == 8 else None) for d, x in acc.items()}


def z_of(st, day, when, model):
    dd = day_sum.get((st, model), {})
    if day not in dd:
        return None
    D = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    d1, d2 = dd[day]
    if when >= D - timedelta(days=1) + timedelta(hours=8) and d1 is not None:
        val, idx = d1, 0
    elif when >= D - timedelta(days=2) + timedelta(hours=8) and d2 is not None:
        val, idx = d2, 1
    else:
        return None
    others = [x[idx] for dk, x in dd.items() if dk != day and x[idx] is not None]
    if len(others) < 8:
        return None
    sd = statistics.pstdev(others)
    return (val - statistics.fmean(others)) / sd if sd > 0 else None


out = {}
for model in ("icon_global", "ecmwf_ifs025"):
    rows = []
    for (st, day) in sorted(by_sd):
        for arrived, delta in changes(st, day, "ECMWF_AIFS_ENS_GLOBAL"):
            t = arrived + timedelta(hours=1)
            r = at((st, day), t, not_before=arrived)
            if r is None or r["y"] not in r["names"]:
                continue
            q = floored(r["q"]); M = argmax_low(q); yk = r["names"].index(r["y"])
            U, D = float(sum(q[M + 1:])), float(sum(q[:M]))
            if U <= 0 or D <= 0 or yk == M:
                continue
            z = z_of(st, day, t, model)
            if z is None:
                continue
            rung = float(r["rung"] or 1.0)
            mom = sum(fresh(st, day, s, t) for s in ("ECMWF_AIFS_ENS_GLOBAL", "ECMWF_AIFS_SINGLE_POINT", "NOAA_GEFS_05"))
            rows.append({"st": st, "d": day, "Lq": U / (U + D), "y": 1.0 if yk > M else 0.0, "z": z, "mom": mom,
                         "x": [fresh(st, day, s, t) / rung for s in ("ECMWF_AIFS_ENS_GLOBAL", "ECMWF_AIFS_SINGLE_POINT", "NOAA_GEFS_05")]})
    ns["rows"] = rows
    score, increment = ns["score"], ns["increment"]
    B0 = lambda r: [logit(r["Lq"])]
    B2 = lambda r: [logit(r["Lq"])] + r["x"]
    out[model] = {
        "rows": len(rows), "days": len({r["d"] for r in rows}),
        "z_alone_score_|z|>=1": score(lambda r: abs(r["z"]) >= 1, lambda r: math.copysign(1, r["z"])),
        "z_alone_increment": increment(B0, lambda r: B0(r) + [r["z"]]),
        "z_over_momentum_increment": increment(B2, lambda r: B2(r) + [r["z"]]),
        "M&z_agree": score(lambda r: abs(r["mom"]) >= 0.3 and abs(r["z"]) >= 0.5 and r["mom"] * r["z"] > 0, lambda r: math.copysign(1, r["mom"])),
        "M&z_oppose": score(lambda r: abs(r["mom"]) >= 0.3 and abs(r["z"]) >= 0.5 and r["mom"] * r["z"] < 0, lambda r: math.copysign(1, r["mom"])),
        "M_quiet_z": score(lambda r: abs(r["mom"]) < 0.3 and abs(r["z"]) >= 1, lambda r: math.copysign(1, r["z"])),
    }
(HERE / "results_radiation_icon.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
for m, r in out.items():
    print("==", m, r["rows"], "实例", r["days"], "日")
    for k, v in r.items():
        if isinstance(v, dict):
            if "score" in v:
                print(f"   {k:28s} n={v['n']:4d} 方向分数 {v['score']:+.3f} [{v['ci'][0]:+.3f},{v['ci'][1]:+.3f}] 两半 {v['halves'][0]:+.3f}/{v['halves'][1]:+.3f}")
            elif "delta" in v:
                print(f"   {k:28s} n={v['n']:4d} 增量 {v['delta']:+.4f} [{v['ci'][0]:+.4f},{v['ci'][1]:+.4f}] 两半 {v['halves'][0]:+.4f}/{v['halves'][1]:+.4f}")
            else:
                print(f"   {k:28s} {v}")
