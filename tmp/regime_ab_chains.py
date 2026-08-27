"""Snapshot per-chain coverage and P&L from whichever arm is in the book now."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import selection_market_dashboard as dash  # noqa: E402

label = sys.argv[1]
conn = sqlite3.connect(
    f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro",
    uri=True, timeout=30.0,
)
rows = conn.execute(
    "SELECT s.station_id, s.event_date, s.settled_bucket, s.pnl_usdc,"
    " s.stake_usdc, p.intended_legs_json, p.posted_buckets_json,"
    " p.window_buckets_json"
    " FROM paper_trade_settlement s"
    " JOIN paper_trade_position p USING(station_id, event_date)"
    " WHERE s.settled_bucket IS NOT NULL",
).fetchall()

chains: dict[str, dict] = {}
for sid, day, landed, pnl, stake, ij, pj, wj in rows:
    key = f"{sid}|{day}"
    chain = chains.setdefault(key, {
        "landed": str(landed), "chosen": set(),
        "pnl": 0.0, "stake": 0.0,
    })
    chain["pnl"] += float(pnl or 0.0)
    chain["stake"] += float(stake or 0.0)
    got = []
    try:
        got = [str(b) for b in (json.loads(ij) if ij else {}).keys()]
    except (TypeError, ValueError, AttributeError):
        got = []
    if not got:
        for fallback in (pj, wj):
            try:
                got = [str(b) for b in json.loads(fallback or "[]")]
            except (TypeError, ValueError):
                got = []
            if got:
                break
    chain["chosen"].update(got)

out: dict[str, dict] = {}
for key, chain in chains.items():
    covered = chain["landed"] in chain["chosen"]
    offset = 0
    if not covered:
        ords = sorted({
            o for o in (dash._bucket_ordinal(b) for b in chain["chosen"])
            if o is not None
        })
        landed = dash._bucket_ordinal(chain["landed"])
        step = dash._bucket_step(chain["landed"])
        if landed is not None and ords:
            if landed > ords[-1]:
                offset = max(1, round((landed - ords[-1]) / step))
            elif landed < ords[0]:
                offset = -max(1, round((ords[0] - landed) / step))
    out[key] = {
        "covered": covered,
        "offset": offset,
        "pnl": round(chain["pnl"], 6),
        "stake": round(chain["stake"], 6),
        "landed": chain["landed"],
        "chosen": sorted(chain["chosen"]),
    }

path = ROOT / "tmp" / f"regime_chains_{label}.json"
path.write_text(
    json.dumps(out, ensure_ascii=False, sort_keys=True, indent=0) + "\n",
    encoding="utf-8",
)
covered = sum(1 for v in out.values() if v["covered"])
print(f"{label}: {len(out)} chains, covered {covered}"
      f" ({100*covered/max(len(out),1):.1f}%), wrote {path.name}")
