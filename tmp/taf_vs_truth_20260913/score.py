"""TAF TX against settled truth, paired with the P0 centre standing when the TAF became knowable."""
import json, collections, statistics as st, pathlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
D = pathlib.Path(__file__).resolve().parent
tafs = json.load(open(D / "tafs.json")); truth = json.load(open(D / "truth.json")); cen = json.load(open(D / "centres.json"))
ts = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))

# Day alignment check on the historical lane (it carries the TX valid time).
hist = [t for t in tafs if t["source"] == "historical"]
aligned = collections.Counter(ts(t["tx_at"]).astimezone(ZoneInfo(t["tz"])).date().isoformat() == t["day"] for t in hist)
print("historical TX valid-at falls on the event local day:", dict(aligned))

def lead_bucket(t):
    tz = ZoneInfo(t["tz"]); day_start = datetime.fromisoformat(t["day"]).replace(tzinfo=tz)
    h = (ts(t["received"]) - day_start).total_seconds() / 3600
    if h < -24: return "a:D-2及更早"
    if h < -12: return "b:D-1 白天前"
    if h < 0: return "c:D-1 夜间(0-12h前)"
    if h < 12: return "d:当日上午"
    if h < 24: return "e:当日下午后"
    return "f:日后"

rows = {}
for t in tafs:
    key = (t["station"], t["day"])
    if f"{t['station']}|{t['day']}" not in truth: continue
    t = dict(t, bucket=lead_bucket(t), truth=truth[f"{t['station']}|{t['day']}"][0])
    # one reading per station-day x lead bucket: the latest knowable TAF in that bucket
    k = (t["station"], t["day"], t["bucket"], t["source"])
    if k not in rows or ts(t["received"]) > ts(rows[k]["received"]): rows[k] = t
rows = list(rows.values())

def summarise(errs):
    n = len(errs)
    if not n: return None
    return dict(n=n, bias=round(st.mean(errs), 2), mae=round(st.mean(abs(e) for e in errs), 2),
                hit_1c=round(sum(abs(e) < 0.5 for e in errs) / n, 3), within_1c=round(sum(abs(e) <= 1.0 for e in errs) / n, 3))

out = {"by_bucket": {}, "by_station": {}, "paired_with_p0": {}, "station_debias_past_only": {}}
for src in ("forward", "historical"):
    for b in sorted({r["bucket"] for r in rows}):
        s = summarise([r["tx"] - r["truth"] for r in rows if r["source"] == src and r["bucket"] == b])
        if s: out["by_bucket"][f"{src}|{b}"] = s
pre = [r for r in rows if r["bucket"] < "e"]
for stn in sorted({r["station"] for r in pre}):
    out["by_station"][stn] = summarise([r["tx"] - r["truth"] for r in pre if r["station"] == stn])

# Pair with the P0 centre in force at TAF receipt (decided <= received < fresh_until).
paired = collections.defaultdict(list)
for r in rows:
    if r["source"] != "forward": continue
    at = ts(r["received"]); best = None
    for dec, c, fu in cen.get(f"{r['station']}|{r['day']}", []):
        if ts(dec) <= at and (fu is None or at < ts(fu)): best = float(c)
    if best is None: continue
    paired[r["bucket"]].append((r["tx"] - r["truth"], best - r["truth"], r))
for b, v in sorted(paired.items()):
    taf = summarise([x[0] for x in v]); p0 = summarise([x[1] for x in v])
    closer = sum(abs(x[0]) < abs(x[1]) for x in v) / len(v)
    blend = summarise([(x[0] + x[1]) / 2 for x in v])
    out["paired_with_p0"][b] = dict(taf=taf, p0=p0, blend_half=blend, taf_closer_share=round(closer, 3))

# Past-only per-station bias correction (only TAF days strictly before this day feed the correction).
for b in sorted({r["bucket"] for r in pre}):
    errs_raw, errs_fix = [], []
    for r in sorted((r for r in pre if r["bucket"] == b), key=lambda r: r["day"]):
        past = [p["tx"] - p["truth"] for p in pre if p["station"] == r["station"] and p["bucket"] == b and p["day"] < r["day"]]
        if len(past) < 7: continue
        errs_raw.append(r["tx"] - r["truth"]); errs_fix.append(r["tx"] - st.mean(past) - r["truth"])
    out["station_debias_past_only"][b] = dict(raw=summarise(errs_raw), debiased=summarise(errs_fix))
(D / "summary.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
print(json.dumps(out, ensure_ascii=False, indent=1))
