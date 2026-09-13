"""Bucket-level: when the TAF TX bucket sits outside the P0 core, does it win more often than P0 prices it?"""
import json, sqlite3, time, collections, pathlib, random, statistics as st
from datetime import datetime
from zoneinfo import ZoneInfo
D = pathlib.Path(__file__).resolve().parent
tafs = json.load(open(D / "tafs.json")); truth = json.load(open(D / "truth.json"))
ts = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
def bucket_of(v, intervals):
    for b, (lo, hi) in intervals.items():
        if (lo is None or v >= float(lo)) and (hi is None or v < float(hi)): return b
    return None
latest = {}
for t in tafs:
    if t["source"] != "forward" or f"{t['station']}|{t['day']}" not in truth: continue
    h = (ts(t["received"]) - datetime.fromisoformat(t["day"]).replace(tzinfo=ZoneInfo(t["tz"]))).total_seconds() / 3600
    if h >= 12: continue  # P1 owns the afternoon; keep D-1 and same-day morning
    k = (t["station"], t["day"], "D-1" if h < 0 else "AM")
    if k not in latest or ts(t["received"]) > ts(latest[k]["received"]): latest[k] = t
c = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=5)
c.execute("pragma query_only=on")
deadline = time.monotonic() + 540
c.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
rows = []
cache = {}
for (stn, day, part), t in latest.items():
    if (stn, day) not in cache:
        cache[(stn, day)] = c.execute(
            "select decision_at_utc, fresh_until_utc, probabilities_json, bucket_ordinals_json,"
            " json_extract(lineage_json,'$.bucket_intervals_c_json'), json_extract(lineage_json,'$.model_center_bucket'),"
            " json_extract(lineage_json,'$.provisional_parameter_sources_json') from basket_probability_envelope"
            " where station_id=? and event_date=? and stage='P0' and state='COMMITTED' order by decision_at_utc", (stn, day)).fetchall()
    at = ts(t["received"]); hit = None
    for r in cache[(stn, day)]:
        if ts(r[0]) <= at and (r[1] is None or at < ts(r[1])) and r[4]: hit = r
    if hit is None: continue
    probs = json.loads(hit[2]); ords = json.loads(hit[3]); intervals = json.loads(hit[4])
    y = truth[f"{stn}|{day}"][0]
    tb, xb = bucket_of(y, intervals), bucket_of(t["tx"], intervals)
    if tb is None or xb is None or hit[5] not in ords: continue
    # P0's own served mode is the centre the seat is built on
    mode = max(probs, key=lambda b: probs[b])
    rows.append(dict(stn=stn, day=day, part=part, refused="bias_refused" in (hit[6] or ""),
                     dist=ords[xb] - ords[mode], p_tx=probs.get(xb, 0.0), hit_tx=tb == xb,
                     p_core=sum(probs.get(b, 0.0) for b in probs if abs(ords[b] - ords[mode]) <= 1),
                     hit_core=abs(ords[tb] - ords[mode]) <= 1,
                     p_span=sum(probs.get(b, 0.0) for b in probs if min(0, ords[xb] - ords[mode]) <= ords[b] - ords[mode] <= max(0, ords[xb] - ords[mode])),
                     hit_span=min(0, ords[xb] - ords[mode]) <= ords[tb] - ords[mode] <= max(0, ords[xb] - ords[mode]),
                     p_mode=probs[mode], hit_mode=tb == mode))
print("rows", len(rows), "refused", sum(r["refused"] for r in rows))
(D / "bucket_rows.json").write_text(json.dumps(rows))
def boot_ratio(rs, hk, pk, n=2000):
    days = sorted({r["day"] for r in rs}); by = collections.defaultdict(list)
    for r in rs: by[r["day"]].append(r)
    rng = random.Random(3); v = []
    for _ in range(n):
        s = [r for d in (rng.choice(days) for _ in days) for r in by[d]]
        p = sum(r[pk] for r in s)
        if p > 0: v.append(sum(r[hk] for r in s) / p)
    v.sort(); return [round(v[int(.05 * len(v))], 2), round(v[int(.95 * len(v)) - 1], 2)]
out = {}
for label, sel in [("偏差正常", lambda r: not r["refused"]), ("全部", lambda r: True)]:
    for grp, g in [("TX=众数桶", lambda r: r["dist"] == 0), ("TX 在众数±1", lambda r: abs(r["dist"]) == 1), ("TX 在±1 外", lambda r: abs(r["dist"]) >= 2), ("TX 暖 +1", lambda r: r["dist"] == 1), ("TX 冷 −1", lambda r: r["dist"] == -1), ("TX 暖 ≥+2", lambda r: r["dist"] >= 2), ("TX 冷 ≤−2", lambda r: r["dist"] <= -2)]:
        rs = [r for r in rows if sel(r) and g(r)]
        if not rs: continue
        out[f"{label}|{grp}"] = dict(
            n=len(rs), days=len({r["day"] for r in rs}),
            tx_bucket_p0=round(st.mean(r["p_tx"] for r in rs), 3), tx_bucket_hit=round(st.mean(r["hit_tx"] for r in rs), 3),
            tx_ratio_90=boot_ratio(rs, "hit_tx", "p_tx"),
            core3_p0=round(st.mean(r["p_core"] for r in rs), 3), core3_hit=round(st.mean(r["hit_core"] for r in rs), 3),
            span_p0=round(st.mean(r["p_span"] for r in rs), 3), span_hit=round(st.mean(r["hit_span"] for r in rs), 3),
            mode_p0=round(st.mean(r["p_mode"] for r in rs), 3), mode_hit=round(st.mean(r["hit_mode"] for r in rs), 3))
(D / "bucket_summary.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
for k, v in out.items(): print(k, json.dumps(v, ensure_ascii=False))
