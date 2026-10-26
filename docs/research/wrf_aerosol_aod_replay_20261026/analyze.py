"""PROTOCOL.md's comparison: sat_aod against the same-day sat arm (and sat_aer, ctl, market,
belief) over the days with a measured aerosol.  usage: analyze.py"""
import json, math, statistics as st
from pathlib import Path

RUN = Path("/Volumes/weather_data/wrf_apps/run/replay")
base = {r["day"]: r for r in map(json.loads, (RUN / "log.jsonl").read_text().splitlines())}
aod = {r["day"]: r for r in map(json.loads, (RUN / "log_aod.jsonl").read_text().splitlines())}
truth = {d: e["truth_c"] for d, e in json.load(open("data/board_readings/wrf_tmax.json"))["value"]["stations"]["ZUUU"]["days"].items()}
rows = []
for d in sorted(aod):
    b, a = base[d], aod[d]
    rows.append({"day": d, "t": truth[d], "aod550": a["aod550"],
                 "sat_aod": a["wrf_sat_aod_tmax_with_obs_c"], "sat": b["wrf_sat_tmax_with_obs_c"],
                 "sat_aer": b["wrf_sat_aer_tmax_with_obs_c"], "ctl": b["wrf_ctl_tmax_with_obs_c"],
                 "market": (b.get("market") or {}).get("mean_c"), "belief": (b.get("belief") or {}).get("mean_c"),
                 "sat_aod_raw": a["wrf_sat_aod_tmax_c"], "sat_raw": b["wrf_sat_tmax_c"]})

def paired(a, b, sub=rows):
    d = [abs(r[a] - r["t"]) - abs(r[b] - r["t"]) for r in sub if r[a] is not None and r[b] is not None]
    m = st.mean(d); s = st.stdev(d) if len(d) > 1 else float("nan")
    return {"n": len(d), "mean_c": round(m, 3), "sd_c": round(s, 3), "t": round(m / (s / math.sqrt(len(d))), 2) if s else None,
            "better": sum(x < 0 for x in d), "worse": sum(x > 0 for x in d)}

out = {"n_days": len(rows),
       "effect_on_tmax_c": {"mean": round(st.mean(r["sat_aod_raw"] - r["sat_raw"] for r in rows), 3),
                            "min": min(r["sat_aod_raw"] - r["sat_raw"] for r in rows), "max": max(r["sat_aod_raw"] - r["sat_raw"] for r in rows)},
       "mae_c": {k: round(st.mean(abs(r[k] - r["t"]) for r in rows if r[k] is not None), 3) for k in ("sat_aod", "sat", "sat_aer", "ctl", "market", "belief")},
       "bias_c": {k: round(st.mean(r[k] - r["t"] for r in rows if r[k] is not None), 3) for k in ("sat_aod", "sat", "sat_aer", "ctl", "market", "belief")},
       "primary_sat_aod_vs_sat": paired("sat_aod", "sat"),
       "raw_sat_aod_vs_sat": paired("sat_aod_raw", "sat_raw"),
       "sat_aod_vs_sat_aer": paired("sat_aod", "sat_aer"),
       "market_vs_sat_aod": paired("market", "sat_aod"), "ctl_vs_sat_aod": paired("ctl", "sat_aod")}
med = st.median(r["aod550"] for r in rows)
out["by_aod_half"] = {"median_aod550": med,
                      "high": paired("sat_aod", "sat", [r for r in rows if r["aod550"] > med]),
                      "low": paired("sat_aod", "sat", [r for r in rows if r["aod550"] <= med])}
print(json.dumps(out, indent=1, ensure_ascii=False))
for r in rows:
    print(r["day"][5:], r["t"], r["aod550"], r["sat"], r["sat_aod"], round(r["sat_aod_raw"] - r["sat_raw"], 2))
