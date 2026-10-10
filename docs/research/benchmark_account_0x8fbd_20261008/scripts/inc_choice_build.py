"""Round 26: the choice rows of the cluster's first-taker episodes AFTER the
research window, in round 14's schema (choice_rows.jsonl.gz), so the
replication can be tested forward.

Fills: the research window's (bench cluster.json + window.json) and the
increment (bench cluster_inc.json), merged and deduplicated; episodes per
(market, outcome) with the same chained 3,600 s gap as cluster_events.py, so
an episode spanning the junction is one episode.  An event is an episode whose
first fill is a taker fill and lies after the research window's last fill.
At the fill: the newest committed WS snapshot at most 30 minutes old (hot SSOT),
the one 60-90 minutes earlier for the hour's move, our newest served vector,
yesterday's settled truth, today's running high (archive reports).  Today's
truth is filled in when settled, None otherwise -- the replication needs none.

Writes <bench>/choice_rows_inc.jsonl.gz and <bench>/cluster_fills_all.json.
"""
import collections
import gzip
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

B = Path(sys.argv[1])
sys.path.insert(0, str(B))
sys.path.insert(0, "/Users/xiong/weather")
from lib import CITY2ST, parse_bucket, parse_event  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402

GAP = 3600


def norm(t, wallet, taker):
    city, ed = parse_event(t.get("eventSlug") or "")
    label, lo, hi, unit, kind = parse_bucket(t.get("title") or "")
    return dict(w=wallet, ts=int(t["timestamp"]), cid=t["conditionId"], outcome=t["outcome"], side=t["side"],
                price=float(t["price"]), size=float(t["size"]), taker=taker, station=CITY2ST.get(city),
                event_date=ed, bucket=label, unit=unit, tx=t["transactionHash"], asset=t["asset"])


def with_taker(fills, taker_rows, wallet):
    keys = collections.Counter((t["transactionHash"], t["asset"], t["side"], str(t["price"]), str(t["size"]), t["timestamp"])
                               for t in taker_rows)
    out = []
    for t in fills:
        k = (t["transactionHash"], t["asset"], t["side"], str(t["price"]), str(t["size"]), t["timestamp"])
        is_t = keys.get(k, 0) > 0
        if is_t:
            keys[k] -= 1
        out.append(norm(t, wallet, is_t))
    return out


allf = []
win = json.load(open(B / "window.json"))
allf += with_taker(win["fills"], win["taker"], "0x8fbd7cf5f806f563080864694415829f7229a959")
for wallet, d in json.load(open(B / "cluster.json")).items():
    allf += with_taker(d["fills"], d["taker"], wallet.lower())
cut = max(f["ts"] for f in allf)
for wallet, d in json.load(open(B / "cluster_inc.json")).items():
    allf += with_taker(d["fills"], d["taker"], wallet.lower())
seen, merged = set(), []
for f in sorted(allf, key=lambda f: f["ts"]):
    k = (f["tx"], f["asset"], f["side"], f["price"], f["size"], f["ts"], f["w"])
    if k in seen:
        continue
    seen.add(k)
    merged.append(f)
buys = [f for f in merged if f["side"] == "BUY" and f["station"] and f["event_date"]]
json.dump([{k: f[k] for k in ("w", "ts", "cid", "outcome", "price", "size", "taker", "station", "event_date", "bucket")}
           for f in buys], open(B / "cluster_fills_all.json", "w"))
print("fills merged", len(merged), "buys", len(buys), "research cut", cut, file=sys.stderr)

by = collections.defaultdict(list)
for f in buys:
    by[(f["cid"], f["outcome"])].append(f)
episodes = []
for lst in by.values():
    lst.sort(key=lambda f: f["ts"])
    ep = [lst[0]]
    for f in lst[1:]:
        if f["ts"] - ep[-1]["ts"] > GAP:
            episodes.append(ep)
            ep = [f]
        else:
            ep.append(f)
    episodes.append(ep)
events = [ep for ep in episodes if ep[0]["ts"] > cut and ep[0]["taker"]]
print("episodes after the cut", sum(1 for ep in episodes if ep[0]["ts"] > cut), "first-taker", len(events), file=sys.stderr)

hot = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
hot.execute("PRAGMA query_only=1")
arch = sqlite3.connect("file:/Users/xiong/weather/data/archive.db?mode=ro", uri=True, timeout=30)
arch.execute("PRAGMA query_only=1")
tz = {s: z for s, z in hot.execute("SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding")}
labels = {}
for st, ed, b, t in hot.execute(
        "SELECT station_id, event_date, settled_bucket, settled_tmax_c FROM basket_station_day_settlement_label l"
        " WHERE event_date >= '2026-10-05' AND truth_revision_no=(SELECT max(truth_revision_no) FROM basket_station_day_settlement_label x"
        " WHERE x.station_id=l.station_id AND x.event_date=l.event_date)"):
    labels[(st, ed)] = (b, float(t))


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def snap(st, ed, ts, lo_s, hi_s):
    row = hot.execute(
        "SELECT buckets_json, observed_at FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=?"
        " AND state='COMMITTED' AND observed_at<=? AND observed_at>=? ORDER BY observed_at DESC LIMIT 1",
        (st, ed, iso(ts - lo_s), iso(ts - hi_s))).fetchone()
    return (json.loads(row[0]), row[1]) if row else (None, None)


def served(st, ed, ts):
    h = hot.execute(
        "SELECT history_id, stage FROM basket_probability_bridge_history WHERE station_id=? AND event_date=?"
        " AND state='COMMITTED' AND decision_at_utc<=? ORDER BY decision_at_utc DESC LIMIT 1", (st, ed, iso(ts))).fetchone()
    if not h:
        return None, None
    P = {b: p for b, p in hot.execute("SELECT bucket, probability FROM basket_probability_bridge_bucket_probability WHERE history_id=?", (h[0],))}
    s = sum(P.values())
    if s <= 0:
        return None, h[1]
    f, _ = floor_probabilities({b: v / s for b, v in P.items()})
    z = sum(f.values())
    return {b: v / z for b, v in f.items()}, h[1]


def levels(raw):
    out = []
    for lv in raw or []:
        try:
            out.append((float(lv["price"]), float(lv.get("shares") or 0)))
        except (TypeError, ValueError, KeyError):
            pass
    return out


out = gzip.open(B / "choice_rows_inc.jsonl.gz", "wt")
miss = collections.Counter()
n = 0
for ep in events:
    f0 = ep[0]
    st, ed, ts = f0["station"], f0["event_date"], f0["ts"]
    Bk, obs_at = snap(st, ed, ts, 0, 1800)
    if not Bk:
        miss["no_book"] += 1
        continue
    P, stage = served(st, ed, ts)
    if not P:
        miss["no_p"] += 1
        continue
    B60, _ = snap(st, ed, ts, 3600, 5400)
    two = {}
    for b in Bk:
        bids, asks = levels(b.get("bid_levels")), levels(b.get("ask_levels"))
        bid = max((p for p, _s in bids), default=None) if bids else (b.get("bid_price") or None)
        ask = min((p for p, _s in asks), default=None) if asks and b.get("ask_available", True) else None
        if bid is not None and ask is not None and 0 < ask - bid < 0.2:
            two[b["bucket"]] = (bid, ask, (asks or [(None, None)])[0][1], (bids or [(None, None)])[0][1])
    if len(two) < 3:
        miss["thin_book"] += 1
        continue
    mids = {b: (v[0] + v[1]) / 2 for b, v in two.items()}
    s = sum(mids.values())
    if not 0.85 <= s <= 1.2:
        miss["unhealthy"] += 1
        continue
    mid60 = {}
    for b in (B60 or []):
        bids, asks = levels(b.get("bid_levels")), levels(b.get("ask_levels"))
        if bids and asks:
            mid60[b["bucket"]] = (max(p for p, _s in bids) + min(p for p, _s in asks)) / 2
    q = {b: m / s for b, m in mids.items()}
    ords = {b["bucket"]: b["ordinal"] for b in Bk}
    mode = max(q, key=lambda b: (q[b], -ords[b]))
    cand = [b for b in P if b in ords]
    if not cand:
        miss["no_ladder_match"] += 1
        continue
    our_mode = max(cand, key=P.get)
    if f0["bucket"] not in two:
        miss["chosen_not_quoted"] += 1
        continue
    zone = ZoneInfo(tz.get(st, "UTC"))
    local = datetime.fromtimestamp(ts, zone)
    day0 = datetime(local.year, local.month, local.day, tzinfo=zone).astimezone(timezone.utc)
    temps = [t for (t,) in arch.execute(
        "SELECT reported_temperature_c FROM reported_trajectory_observations WHERE station_id=? AND valid_at>=? AND valid_at<=?",
        (st, day0.strftime("%Y-%m-%dT%H:%M"), datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")))]
    yday = (datetime.fromisoformat(ed) - timedelta(days=1)).date().isoformat()
    truth = labels.get((st, ed))
    alts = []
    for b, (bid, ask, ash, bsh) in two.items():
        o = ords[b]
        mom = (mids[b] - mid60[b]) if b in mid60 else None
        for side in ("Yes", "No"):
            alts.append(dict(bucket=b, ord=o, side=side, price=ask if side == "Yes" else 1 - bid, q=q[b], spread=ask - bid,
                             depth=ash if side == "Yes" else bsh, d_mode=o - ords[mode], d_our=o - ords[our_mode],
                             p=P.get(b, 0.0), mom=mom, chosen=int(b == f0["bucket"] and side == f0["outcome"])))
    lead = (datetime.fromisoformat(ed).replace(tzinfo=timezone.utc) + timedelta(hours=12)
            - datetime.fromtimestamp(ts, timezone.utc)).total_seconds() / 3600
    out.write(json.dumps(dict(
        station=st, event_date=ed, ts=ts, lead_h=lead, unit=f0["unit"] or ("F" if "°F" in f0["bucket"] else "C"),
        stage=stage, usdc=sum(f["size"] * f["price"] for f in ep), wallet=f0["w"][:10],
        truth_ord=ords.get(truth[0]) if truth else None, truth_bucket=truth[0] if truth else None,
        truth_c=truth[1] if truth else None, cur_obs=temps[-1] if temps else None,
        tmax_today=max(temps) if temps else None, ytruth_c=(labels.get((st, yday)) or (None, None))[1],
        is_d0=(local.date().isoformat() == ed), mode_ord=ords[mode], our_mode_ord=ords[our_mode], book_at=obs_at,
        alts=alts), ensure_ascii=False) + "\n")
    n += 1
out.close()
print("rows", n, dict(miss), file=sys.stderr)
