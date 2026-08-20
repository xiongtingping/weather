"""Per-stage resting fill account for one ledger configuration.

Run right after a ledger rebuild, before anything re-derives the rows.
Prints one JSON object; the sweep collects them side by side.
"""

import json
import sqlite3
import sys

FEE_RATE = 0.0  # the ledger's stake already carries the fee

conn = sqlite3.connect(
    "file:data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60
)
conn.execute("PRAGMA busy_timeout=60000")

label = conn.execute(
    "SELECT selection_rule_label, COUNT(*) FROM paper_trade_position"
    " GROUP BY 1 ORDER BY 2 DESC LIMIT 1"
).fetchone()

stages = {}
for stage in ("P0", "P0-M", "P1"):
    stages[stage] = {
        "positions": 0, "zero_fill_baskets": 0,
        "posted_rungs": 0, "filled_rungs": 0,
        "intended_shares": 0.0, "filled_shares": 0.0,
        "intended_stake": 0.0, "stake": 0.0,
        "taker_equiv_cost": 0.0, "edge_sum": 0.0,
    }

for (stage, rf, stake, intended_stake, legs_js, intended_js,
     prices_js, edge) in conn.execute(
    "SELECT stage, resting_fill_json, stake_usdc, intended_stake_usdc,"
    "       leg_shares_json, intended_legs_json, prices_json, edge"
    " FROM paper_trade_position"
):
    s = stages[stage]
    s["positions"] += 1
    s["stake"] += stake or 0.0
    s["intended_stake"] += intended_stake or 0.0
    s["edge_sum"] += edge or 0.0
    legs = json.loads(legs_js) if legs_js else {}
    intended = json.loads(intended_js) if intended_js else {}
    prices = json.loads(prices_js) if prices_js else {}
    for bucket, want in intended.items():
        s["intended_shares"] += float(want)
        got = float(legs.get(bucket, 0.0))
        s["filled_shares"] += got
        ask = float(prices.get(bucket) or 0.0)
        s["taker_equiv_cost"] += got * ask * 1.0
    if rf:
        for value in json.loads(rf).values():
            frac = str(value).split("@")[0]
            got_n, posted_n = (int(p) for p in frac.split("/"))
            s["posted_rungs"] += posted_n
            s["filled_rungs"] += got_n

for count, stage in conn.execute(
    "SELECT COUNT(*), stage FROM paper_trade_refusal"
    " WHERE reason='resting_unfilled' GROUP BY stage"
):
    stages[str(stage)]["zero_fill_baskets"] = int(count)

settled = {}
for stage, n, hits, stk, pnl in conn.execute(
    "SELECT stage, COUNT(*), SUM(hit), SUM(stake_usdc), SUM(pnl_usdc)"
    " FROM paper_trade_settlement GROUP BY stage"
):
    settled[stage] = {
        "n": n, "hits": hits, "stake": stk, "pnl": pnl,
        "roi": pnl / stk if stk else None,
    }
total = conn.execute(
    "SELECT COUNT(*), SUM(hit), SUM(stake_usdc), SUM(pnl_usdc)"
    " FROM paper_trade_settlement"
).fetchone()

out = {"rule_label": label[0] if label else None, "stages": {}}
for stage, s in stages.items():
    out["stages"][stage] = {
        "positions": s["positions"],
        "zero_fill_baskets": s["zero_fill_baskets"],
        "share_fill_rate": (
            s["filled_shares"] / s["intended_shares"]
            if s["intended_shares"] else None
        ),
        "rung_fill_rate": (
            s["filled_rungs"] / s["posted_rungs"]
            if s["posted_rungs"] else None
        ),
        "stake_fill_rate": (
            s["stake"] / s["intended_stake"] if s["intended_stake"] else None
        ),
        # What the same realised share vector would have cost crossing at
        # the entry ask.  Positive means resting still saves money.
        "maker_saving_vs_taker": (
            1.0 - s["stake"] / (s["taker_equiv_cost"] * 1.0)
            if s["taker_equiv_cost"] else None
        ),
        "mean_entry_edge": (
            s["edge_sum"] / s["positions"] if s["positions"] else None
        ),
        "settled": settled.get(stage),
    }
out["total_settled"] = {
    "n": total[0], "hits": total[1], "stake": total[2], "pnl": total[3],
    "roi": (total[3] / total[2]) if total[2] else None,
}
json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
print()
