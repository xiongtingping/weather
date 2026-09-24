"""Settled P&L of the system's own basket strategy as the paper ledger replays it
(production judge; P0 opens, P0-M / P1 add), by stage, venue truth applied.

    python3 ledger_readout.py
"""
import collections
import json
import sqlite3
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
db = sqlite3.connect(f"file:{ROOT / 'data' / 'paper_ledger.db'}?mode=ro", uri=True, timeout=30)
ov = json.load(open(Path(__file__).with_name("venue_truth_overrides.json")))["overrides"]
rows = db.execute(
    "SELECT p.station_id, p.event_date, p.stage, p.leg_shares_json, p.window_buckets_json,"
    " s.settled_bucket, s.hit, s.stake_usdc, s.payout_usdc, s.pnl_usdc, p.selection_rule_sha256"
    " FROM paper_trade_position p JOIN paper_trade_settlement s ON s.position_id=p.position_id").fetchall()
recs = []
for st, day, stage, legs_j, win_j, sb, hit, stake, payout, pnl, sha in rows:
    vb = ov.get(f"{st}|{day}")
    if vb and vb != sb:
        legs, win = json.loads(legs_j or "null"), json.loads(win_j or "[]")
        shares = legs if isinstance(legs, dict) else dict(zip(win, legs or []))
        payout = float(shares.get(vb, 0.0))
        pnl, hit = payout - stake, int(payout > 0)
    recs.append((day, stage, stake, pnl, hit))
print("rule digests:", sorted({r[10][:12] for r in rows}))


def summ(rs, label):
    byday = collections.defaultdict(lambda: [0.0, 0.0])
    for d, _, s, p, _ in rs:
        byday[d][0] += p
        byday[d][1] += s
    days = sorted(byday)
    rng = np.random.default_rng(3)
    bs = []
    for _ in range(2000):
        pk = rng.choice(len(days), len(days))
        bs.append(sum(byday[days[i]][0] for i in pk) / sum(byday[days[i]][1] for i in pk))
    h = len(days) // 2
    r1 = sum(byday[d][0] for d in days[:h]) / sum(byday[d][1] for d in days[:h])
    r2 = sum(byday[d][0] for d in days[h:]) / sum(byday[d][1] for d in days[h:])
    st = sum(r[2] for r in rs)
    pn = sum(r[3] for r in rs)
    print(f"{label}: n={len(rs)} days={len(days)} stake={st:.1f} pnl={pn:+.1f} roi={pn/st:+.3f} "
          f"[{np.percentile(bs, 5):+.3f},{np.percentile(bs, 95):+.3f}] halves {r1:+.3f}/{r2:+.3f} hit={np.mean([r[4] for r in rs]):.1%}")


summ(recs, "all")
for s in ("P0", "P0-M", "P1"):
    sel = [r for r in recs if r[1] == s]
    if len({r[0] for r in sel}) >= 2:
        summ(sel, s)
