"""Drop the worst stations from the book and see what is left.

Reads the dashboard's own ``station_stats`` rather than recomputing it, so
"loss" here means exactly what section eleven means by it: credible,
fillable positions, settled chain tails, realised P&L.

The arithmetic the question asks for is the easy half.  The half that decides
whether it means anything is whether a station's losing is a property of the
station or of the fortnight, so the same stations are split at the midpoint
and asked to lose in both halves.  Dropping the worst N after the fact is
selection on the outcome; only persistence makes it a rule you could have
run in advance.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import selection_market_dashboard as dash  # noqa: E402

payload = dash.build_payload(ROOT / "data" / "bayes_basket_target_ssot.db")
rows = payload["station_stats"]
book = payload.get("book") or {}

total_pnl = sum(float(r["realized_usdc"] or 0.0) for r in rows)
total_stake = sum(float(r.get("stake_usdc") or 0.0) for r in rows)
print(f"逐站汇总里共 {len(rows)} 站")
print(f"  合计已实现盈亏 {total_pnl:+.2f} USDC")
if book:
    print(f"  （账本口径：权益 {book.get('equity_usdc')}, "
          f"ROI {book.get('return_on_stake')}）")

ranked = sorted(rows, key=lambda r: float(r["realized_usdc"] or 0.0))
print("\n亏损前十（按已实现盈亏升序）:")
print("  站     仓位  已结算  命中率   声称    已实现USDC   期望USDC")
for r in ranked[:10]:
    hit = r.get("hit_rate")
    claimed = r.get("claimed_mean")
    print(
        f"  {r['station_id']:6s} {r['position_count']:>4d}"
        f" {r['settled_count']:>6d}"
        f" {(f'{100*hit:.1f}%' if hit is not None else '—'):>7s}"
        f" {(f'{claimed:.3f}' if claimed is not None else '—'):>7s}"
        f" {float(r['realized_usdc'] or 0):>11.2f}"
        f" {float(r.get('expected_usdc') or 0):>10.2f}"
    )

print("\n逐个剔除后的账本：")
print("  剔除数   剩余站   剩余已结算   合计盈亏      对比全量")
for drop in (0, 1, 2, 3, 5, 8, 10):
    kept = ranked[drop:]
    pnl = sum(float(r["realized_usdc"] or 0.0) for r in kept)
    settled = sum(int(r["settled_count"] or 0) for r in kept)
    print(
        f"  {drop:>4d}  {len(kept):>7d}  {settled:>10d}"
        f"  {pnl:>+11.2f}  {pnl - total_pnl:>+10.2f}"
    )

# ------------------------------------------------------------------
# Does the losing persist?  Same stations, book split at its midpoint.
# ------------------------------------------------------------------
import json
import sqlite3

conn = sqlite3.connect(
    f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro",
    uri=True, timeout=60.0,
)
per_day: dict[str, dict[str, float]] = defaultdict(dict)
settle_rows = conn.execute(
    "SELECT station_id, event_date, SUM(pnl_usdc)"
    " FROM paper_trade_settlement GROUP BY station_id, event_date"
).fetchall()
days = sorted({str(r[1]) for r in settle_rows})
if len(days) >= 6:
    cut = days[len(days) // 2]
    first: dict[str, float] = defaultdict(float)
    second: dict[str, float] = defaultdict(float)
    n_first: dict[str, int] = defaultdict(int)
    n_second: dict[str, int] = defaultdict(int)
    for station, day, pnl in settle_rows:
        bucket, counter = (
            (first, n_first) if str(day) < cut else (second, n_second)
        )
        bucket[str(station)] += float(pnl or 0.0)
        counter[str(station)] += 1
    worst = [r["station_id"] for r in ranked[:10]]
    print(f"\n亏损前十在前后半段各自的表现（分割点 {cut}）:")
    print("  站       前半段USDC(链)   后半段USDC(链)   后半段仍亏?")
    still = 0
    for station in worst:
        a, b = first.get(station, 0.0), second.get(station, 0.0)
        if n_second.get(station, 0) == 0:
            verdict = "后半段无结算"
        elif b < 0:
            verdict = "是"
            still += 1
        else:
            verdict = "否（转正）"
        print(
            f"  {station:6s} {a:>10.2f}({n_first.get(station,0):>2d})"
            f" {b:>13.2f}({n_second.get(station,0):>2d})   {verdict}"
        )
    scored = [s for s in worst if n_second.get(s, 0) > 0]
    print(f"\n  前十里后半段仍亏的: {still}/{len(scored)}"
          f"（有后半段结算的 {len(scored)} 站）")

    # And the fair test: rank on the first half only, drop those, and see
    # what the second half returns.  That is the rule someone could actually
    # have run.
    ranked_first = sorted(
        (s for s in first if n_first[s] >= 2), key=lambda s: first[s]
    )
    dropped = set(ranked_first[:10])
    kept_second = sum(
        v for s, v in second.items() if s not in dropped
    )
    all_second = sum(second.values())
    print(f"\n  前瞻检验（只用前半段排名，剔掉那十个，看后半段）:")
    print(f"    后半段全量 {all_second:+.2f} USDC")
    print(f"    剔掉前半段最差十站后 {kept_second:+.2f} USDC"
          f"  ({kept_second - all_second:+.2f})")
