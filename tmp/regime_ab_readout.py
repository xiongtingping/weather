"""Read out one A/B arm: the book, and the miss distribution behind it.

ROI is reported but is not the endpoint.  On roughly two hundred chains it
is dominated by a handful of them -- the standing ruling is that a ledger ROI
difference means nothing until it survives dropping the best one, two and
three chains -- whereas "did the chosen basket cover the truth" is a
proportion over every settled chain and is the question this layer was built
to move.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import selection_market_dashboard as dash  # noqa: E402

label = sys.argv[1]
payload_path = Path(sys.argv[2])

try:
    book = json.loads(payload_path.read_text(encoding="utf-8"))["book"]
except Exception as exc:  # noqa: BLE001
    print(f"     {label}: report unreadable ({exc})")
    raise SystemExit(0)

conn = sqlite3.connect(
    f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro",
    uri=True, timeout=30.0,
)
rows = conn.execute(
    "SELECT s.station_id, s.event_date, s.settled_bucket, s.pnl_usdc,"
    " p.intended_legs_json, p.posted_buckets_json, p.window_buckets_json"
    " FROM paper_trade_settlement s"
    " JOIN paper_trade_position p USING(station_id, event_date)"
    " WHERE s.settled_bucket IS NOT NULL",
).fetchall()

chains: dict[tuple[str, str], dict] = {}
for sid, day, landed, pnl, ij, pj, wj in rows:
    key = (str(sid), str(day))
    chain = chains.setdefault(key, {
        "landed": str(landed), "chosen": set(), "pnl": 0.0,
    })
    chain["pnl"] += float(pnl or 0.0)
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

hits = 0
offsets: dict[int, int] = {}
for chain in chains.values():
    if chain["landed"] in chain["chosen"]:
        hits += 1
        continue
    ords = sorted({
        o for o in (dash._bucket_ordinal(b) for b in chain["chosen"])
        if o is not None
    })
    landed = dash._bucket_ordinal(chain["landed"])
    if landed is None or not ords:
        continue
    step = dash._bucket_step(chain["landed"])
    if landed > ords[-1]:
        offset = max(1, round((landed - ords[-1]) / step))
    elif landed < ords[0]:
        offset = -max(1, round((ords[0] - landed) / step))
    else:
        continue
    offsets[offset] = offsets.get(offset, 0) + 1

total = len(chains)
summary = {
    "label": label,
    "settled_chains": total,
    "covered": hits,
    "coverage_rate": hits / total if total else 0.0,
    "offsets": offsets,
    "roi": book.get("return_on_stake"),
    "equity_usdc": book.get("equity_usdc"),
    "hit_rate": book.get("hit_rate"),
    "staked_usdc": book.get("staked_usdc"),
    "book_settled": book.get("settled_chains"),
    "chain_pnl": {
        f"{sid}|{day}": round(chain["pnl"], 4)
        for (sid, day), chain in chains.items()
    },
}
(ROOT / "tmp" / f"regime_ab_{label}_summary.json").write_text(
    json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n",
    encoding="utf-8",
)

plus_one = offsets.get(1, 0)
print(
    f"     {label:8s} 选桶覆盖 {hits}/{total} = {100 * hits / max(total,1):.1f}%"
    f"  | +1 脱靶 {plus_one}"
    f"  | ROI {100 * (book.get('return_on_stake') or 0):+.1f}%"
    f"  | 权益 {book.get('equity_usdc'):.1f}"
)
print(
    "              脱靶分布 "
    + " ".join(
        f"{k:+d}:{offsets[k]}" for k in sorted(offsets)
    )
)
