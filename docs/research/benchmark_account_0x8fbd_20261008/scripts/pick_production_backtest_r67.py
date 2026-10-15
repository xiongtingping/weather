"""Round 67: the production keep-F pick (charter 267 补记七, listing_forecast_f.compute) replayed strictly as of
each event day's listing instant (truths cut at listing date - 2), against the truth.  Resumable in 520 s chunks.
Usage: pick_production_backtest_r67.py <bench>
"""
import json
import sqlite3
import statistics
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

ROOT = Path("/Users/xiong/weather")
sys.path.insert(0, str(ROOT))
from bayes_basket_target import listing_forecast_f as F  # noqa: E402
from bayes_basket_target.deterministic_forecast_family import global_roster  # noqa: E402

B = Path(sys.argv[1])
B.mkdir(parents=True, exist_ok=True)
VARIANT = sys.argv[2] if len(sys.argv) > 2 else "production"
OUT = B / f"pick_backtest_r67_{VARIANT}.json"
done = json.load(open(OUT)) if OUT.exists() else {}
conn = sqlite3.connect(f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")
ROSTER = global_roster(conn)
ALL = F.formal_truths(conn, since=date(2026, 6, 1), until=date(2026, 12, 31))
real_truths = F.formal_truths
if VARIANT == "train_from_0912":
    # diagnostic: the research sample's start -- training rows only from event day 09-12 (round 65/66 data)
    _fit = F.fit_pick
    F.fit_pick = lambda rows: _fit([r for r in rows if r[0] >= date(2026, 9, 12)])
D_FROM, D_TO = date(2026, 9, 22), date(2026, 10, 14)
t0 = time.time()
for k in range((D_TO - D_FROM).days + 1):
    d = D_FROM + timedelta(days=k)
    if d.isoformat() in done or time.time() - t0 > 500:
        continue
    now = datetime.fromisoformat(F.listing_instant(d).replace("Z", "+00:00"))
    cut = (now.date() - timedelta(days=2)).isoformat()
    F.formal_truths = lambda c, since, until, cut=cut: {kk: v for kk, v in ALL.items() if since.isoformat() <= kk[1] <= min(until.isoformat(), cut)}
    payload = F.compute(conn, now=now, roster=ROSTER)
    done[d.isoformat()] = {"pick_model": {kk: v for kk, v in (payload.get("pick") or {}).items() if kk not in ("pos", "full")},
                           "rows": {kk.split("|")[0]: {"f_c": v.get("f_c"), "pick": v.get("pick")}
                                    for kk, v in payload["station_days"].items() if kk.endswith(d.isoformat()) and v.get("state") == "READY"}}
    json.dump(done, open(OUT, "w"))
    print(d, f"{time.time() - t0:.0f}s", file=sys.stderr, flush=True)
left = (D_TO - D_FROM).days + 1 - len(done)
if left:
    print(f"{left} event days left", file=sys.stderr)
    sys.exit(3)

truth = {}
for st, ed, b in conn.execute(
        "SELECT station_id, event_date, settled_bucket FROM basket_station_day_settlement_label l"
        " WHERE event_date BETWEEN ? AND ? AND truth_revision_no=(SELECT max(truth_revision_no) FROM basket_station_day_settlement_label x"
        " WHERE x.station_id=l.station_id AND x.event_date=l.event_date)", (D_FROM.isoformat(), D_TO.isoformat())):
    truth[(st, ed)] = b
rows = []
for ed, v in done.items():
    for st, r in v["rows"].items():
        p = r.get("pick") or {}
        if p.get("state") != "READY" or (st, ed) not in truth:
            continue
        rows.append({"ed": ed, "st": st, "off": p["offset"], "pick": p["bucket"], "fb": p["f_bucket"], "y": truth[(st, ed)]})
states = {}
for v in done.values():
    for r in v["rows"].values():
        s = (r.get("pick") or {}).get("state")
        states[s] = states.get(s, 0) + 1
print(f"event days {D_FROM}..{D_TO}; pick states {states}; scored (settled, READY) {len(rows)}")
print("pick model by day: " + ", ".join(f"{ed[5:]} {v['pick_model'].get('state')}/{v['pick_model'].get('days')}d" for ed, v in sorted(done.items())))
rng = np.random.default_rng(67)


def boot(vals, eds, draws=2000):
    vals, eds = np.asarray(vals, float), np.asarray(eds)
    ud = sorted(set(eds))
    sums = np.array([vals[eds == d].sum() for d in ud])
    cnts = np.array([(eds == d).sum() for d in ud])
    idx = rng.integers(0, len(ud), size=(draws, len(ud)))
    bs = sums[idx].sum(1) / cnts[idx].sum(1)
    return float(vals.mean()), float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))


hp = [float(r["pick"] == r["y"]) for r in rows]
hf = [float(r["fb"] == r["y"]) for r in rows]
m, lo, hi = boot([a - b for a, b in zip(hp, hf)], [r["ed"] for r in rows])
moved = [r for r in rows if r["off"] != 0]
print(f"pick hits {statistics.mean(hp):.1%} vs F bucket {statistics.mean(hf):.1%}: {m:+.3f} [{lo:+.3f}, {hi:+.3f}]")
if moved:
    print(f"moved off F {len(moved)}/{len(rows)} ({len(moved) / len(rows):.1%}; F+1 {sum(r['off'] > 0 for r in moved)}, F-1 {sum(r['off'] < 0 for r in moved)}):"
          f" pick hits {statistics.mean(float(r['pick'] == r['y']) for r in moved):.1%} vs F {statistics.mean(float(r['fb'] == r['y']) for r in moved):.1%}")
days = sorted({r["ed"] for r in rows})
mid = days[len(days) // 2]
for half, keep in (("first half", lambda e: e < mid), ("second half", lambda e: e >= mid)):
    g = [r for r in rows if keep(r["ed"])]
    mv = [r for r in g if r["off"] != 0]
    print(f"  {half}: all {statistics.mean(float(r['pick'] == r['y']) for r in g):.1%} vs F {statistics.mean(float(r['fb'] == r['y']) for r in g):.1%};"
          f" moved {len(mv)}: {statistics.mean(float(r['pick'] == r['y']) for r in mv) if mv else float('nan'):.1%}"
          f" vs F {statistics.mean(float(r['fb'] == r['y']) for r in mv) if mv else float('nan'):.1%}")
