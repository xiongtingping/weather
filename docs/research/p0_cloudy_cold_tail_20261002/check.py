#!/usr/bin/env python3
"""PROTOCOL.md: cold-tail mixture on forecast-cloudy updates, scored on the current-arithmetic replay's `cur` vectors."""
from __future__ import annotations
import gzip, json, math, sqlite3, sys
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402
from bayes_basket_target.cross_consensus import clearness_index  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement_if_known  # noqa: E402
REPLAY = ROOT / "docs/research/radiation_pair_clean_replay_20260926/pass1"
HALVES = (("2026-08-16", "2026-09-04"), ("2026-09-05", "2026-09-23"))
KT_CLOUDY, KT_SUNNY, Z_CLOUDY, W_FIX, S_FIX = 0.50, 0.63, -0.5, 0.05, 2
BLOCK, BOOT, SEED = 3, 2000, 20261002

def inst(v): return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)

def shift_cold(p, s):
    q = np.zeros_like(p)
    for i, x in enumerate(p): q[max(0, i - s)] += x
    return q

def mix(p, w, s=S_FIX): return (1 - w) * p + w * shift_cold(p, s)

def score(vec, keys, hit, centers):
    fl, _ = floor_probabilities({k: float(v) for k, v in zip(keys, vec)})
    q = np.array([fl[k] for k in keys]); y = np.zeros_like(q); y[hit] = 1
    crps = float(((np.cumsum(q)[:-1] - np.cumsum(y)[:-1]) ** 2 * np.diff(centers)).sum())
    return np.array([-math.log(max(q[hit], 1e-12)), float(((q - y) ** 2).sum()), crps])

def main():
    ss = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
    zones = dict(ss.execute("SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding"))
    ver = defaultdict(list)
    for s, d, sw, lat, cap in ss.execute(
            "SELECT station_id,local_date,shortwave_radiation_sum,latitude,captured_at_utc FROM calibration_station_forecast_physics_version"
            " WHERE capture_mode IN ('forward','p0_envelope_receipt') AND shortwave_radiation_sum IS NOT NULL AND local_date BETWEEN '2026-08-14' AND '2026-09-25'"):
        ver[(s, d)].append((inst(cap), clearness_index(sw, lat, d)))
    ss.close()
    for k in ver: ver[k].sort(key=lambda x: x[0])
    rows = []
    for path in sorted(REPLAY.glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as h:
            for line in h:
                sd = json.loads(line); c = np.array(sd["centers_c"], float); keys = sd["keys"]; hit = int(sd["hit"])
                vs = ver.get((sd["station"], sd["event_date"]), []); times = [t for t, _ in vs]
                for u in sd["updates"]:
                    if u["rad_state"] != "RESOLVED" or u["z"] is None: continue
                    at = inst(u["at"]); i = bisect_right(times, at)
                    kt = vs[i - 1][1] if i else None
                    p = np.array(u["vecs"]["cur"], float); p = p / p.sum()
                    hours = hours_to_settlement_if_known(sd["event_date"], as_of_utc=u["at"], settlement_timezone=zones.get(sd["station"]))
                    rows.append(dict(station=sd["station"], event_date=sd["event_date"], hours=hours, kt=kt, z=float(u["z"]),
                                     p=p, keys=keys, hit=hit, c=c, base=score(p, keys, hit, c)))
    count = defaultdict(int)
    for r in rows: count[(r["station"], r["event_date"])] += 1
    for r in rows: r["w"] = 1.0 / count[(r["station"], r["event_date"])]
    print("updates", len(rows), "station-days", len(count), "kt resolved", sum(r["kt"] is not None for r in rows),
          "A eligible", sum(1 for r in rows if r["kt"] is not None and r["kt"] <= KT_CLOUDY), flush=True)
    rng = np.random.default_rng(SEED)
    coin = rng.random(len(rows))
    share = np.mean([r["kt"] is not None and r["kt"] <= KT_CLOUDY for r in rows])
    arms = {
        "A 主臂 Kt≤0.50 固定w=0.05": lambda r, i: r["kt"] is not None and r["kt"] <= KT_CLOUDY,
        "B 站内z≤−0.5 固定w=0.05": lambda r, i: r["z"] <= Z_CLOUDY,
        "对照 随机同比例": lambda r, i: coin[i] < share,
        "对照 Kt≥0.63": lambda r, i: r["kt"] is not None and r["kt"] >= KT_SUNNY,
    }
    def boot(sub, d):
        w = np.array([r["w"] for r in sub]); days = sorted({r["event_date"] for r in sub}); by = defaultdict(list)
        for i, r in enumerate(sub): by[r["event_date"]].append(i)
        nb = math.ceil(len(days) / BLOCK); out = []
        for _ in range(BOOT):
            idx = np.concatenate([by[days[(s + j) % len(days)]] for s in rng.integers(0, len(days), nb) for j in range(BLOCK)])
            out.append((w[idx, None] * d[idx]).sum(0) / w[idx].sum())
        out = np.array(out); return (w[:, None] * d).sum(0) / w.sum(), np.percentile(out, 2.5, 0), np.percentile(out, 97.5, 0)
    result = {}
    def report(name, sub, d):
        m, lo, hi = boot(sub, d)
        halves = [boot([r for r in sub if a <= r["event_date"] <= b], d[[a <= r["event_date"] <= b for r in sub]])[0][0] for a, b in HALVES]
        verdict = hi[0] < 0 and lo[1] <= 0 and lo[2] <= 0 and all(h < 0 for h in halves)
        print(f"{name:28} | dLog {m[0]:+.5f} [{lo[0]:+.5f},{hi[0]:+.5f}] dBrier {m[1]:+.5f} [{lo[1]:+.5f},{hi[1]:+.5f}] dCRPS {m[2]:+.5f} [{lo[2]:+.5f},{hi[2]:+.5f}] | 两半 {halves[0]:+.5f}/{halves[1]:+.5f} | {'成立' if verdict else '不成立'}", flush=True)
        result[name] = dict(delta=m.tolist(), lo=lo.tolist(), hi=hi.tolist(), halves=halves, verdict=bool(verdict))
    for pop_name, pop in (("全体", rows), ("24≤h<60", [r for r in rows if r["hours"] is not None and 24 <= r["hours"] < 60])):
        print("==", pop_name, len(pop))
        idx = {id(r): i for i, r in enumerate(rows)}
        for name, cond in arms.items():
            d = np.array([score(mix(r["p"], W_FIX), r["keys"], r["hit"], r["c"]) - r["base"] if cond(r, idx[id(r)]) else np.zeros(3) for r in pop])
            report(name, pop, d)
        # C: walk-forward w on A's condition
        days = sorted({r["event_date"] for r in pop}); grid = np.arange(0.05, 0.55, 0.05)
        el = [r for r in pop if r["kt"] is not None and r["kt"] <= KT_CLOUDY]
        cand = {(id(r), round(w, 2)): score(mix(r["p"], w), r["keys"], r["hit"], r["c"]) - r["base"] for r in el for w in grid}
        pick = {}
        for i, dday in enumerate(days):
            tr = [r for r in el if r["event_date"] < dday]
            if len({r["event_date"] for r in tr}) < 10: continue
            best = min(grid, key=lambda w: sum(r["w"] * cand[(id(r), round(w, 2))][0] for r in tr))
            pick[dday] = round(best, 2) if sum(r["w"] * cand[(id(r), round(best, 2))][0] for r in tr) < 0 else None
        d = np.array([cand[(id(r), pick[r["event_date"]])] if (r in el and pick.get(r["event_date"]) is not None) else np.zeros(3) for r in pop])
        sub_mask = [r["event_date"] in pick for r in pop]
        report("C 主条件 w前推", [r for r, m in zip(pop, sub_mask) if m], d[sub_mask])
        print("   C picks", sorted(set(v for v in pick.values()), key=lambda v: -1 if v is None else v), flush=True)
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
