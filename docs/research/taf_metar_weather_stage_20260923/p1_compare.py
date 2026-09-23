import json, statistics, random, sys
# usage: p1_compare.py <workdir> <variant>   (variant = taf | surprise)
S, VARIANT = sys.argv[1], sys.argv[2]
b = json.load(open(f"{S}/base/artifact.json.walk.json")); t = json.load(open(f"{S}/{VARIANT}/artifact.json.walk.json"))
days = sorted(set(b["direct_nat_by_day"]) & set(t["direct_nat_by_day"]))
diff = [t["direct_nat_by_day"][d] - b["direct_nat_by_day"][d] for d in days]
rng = random.Random(20260924)
boot = sorted(statistics.mean(rng.choice(diff) for _ in diff) for _ in range(4000))
print("days", days[0], "..", days[-1], len(days))
for d, x in zip(days, diff): print(f"  {d}: base {b['direct_nat_by_day'][d]:+.4f}  taf {t['direct_nat_by_day'][d]:+.4f}  diff {x:+.4f}")
print(f"paired mean diff (taf - base) {statistics.mean(diff):+.5f} nat/sample  CI [{boot[100]:+.5f}, {boot[3899]:+.5f}]  days better {sum(1 for x in diff if x > 0)}/{len(diff)}")
print("gain vs root: base", round(b["gain_vs_root_nat"], 4), "taf", round(t["gain_vs_root_nat"], 4), "| vs alpha: base", round(b["gain_vs_alpha_nat"], 4), "taf", round(t["gain_vs_alpha_nat"], 4))
a = json.load(open(f"{S}/{VARIANT}/artifact.json"))["direct_update"]
lay = a["x_layout"]; tr = a["theta_rise"]; ts = a["theta_slope"]
for name in ("cloud_cover_fraction", "precipitation_present", "wind_speed_mps", "taf_cloud_cover_fraction", "taf_precipitation_probability", "taf_wind_speed_mps", "cloud_surprise", "precip_surprise"):
    if name not in lay: continue
    row = []
    for pre in ("", "mid_x_", "late_x_"):
        k = lay.index(pre + name); row.append(f"{pre or 'all_'}rise {tr[k]:+.3f} slope {ts[k]:+.3f}")
    print(f"  {name:32s} " + " | ".join(row))
print("taf fallbacks", {k: round(v, 3) for k, v in a["fallbacks"].items() if k.startswith("taf_") or k.endswith("_surprise")})
