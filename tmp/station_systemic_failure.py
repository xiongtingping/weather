"""Which stations are structurally bad, as opposed to unlucky?

Ranking by P&L alone cannot answer that: on five to ten settled chains a
station can lose badly by chance, and the worst-ten list will always contain
some of those.  What separates a broken station from an unlucky one is
failing on dimensions that are not the same measurement wearing different
clothes.

Five are used here, each answering a different question and each pointing at
a different layer:

  1. 实现/期望   realised P&L against what the model expected.  Below zero
                 means it lost money on days the model wanted to be there --
                 an execution- or claim-layer fault, not weather.
  2. 命中/声称   hit rate against mean claimed probability.  Below one means
                 the claim is overstated, which is the calibration layer.
  3. 中心残差    truth minus the served posterior mean, in Celsius.  Large
                 means the centre is simply wrong at this station -- the
                 bias layer, upstream of everything else.
  4. 覆盖        did the chosen basket contain the truth.  Separates "we
                 picked the wrong buckets" from "we picked right and still
                 lost", which are different faults.
  5. 持续性      does it lose in both halves of the book.  A station that
                 only loses in one half is a fortnight, not a station.

Sample size is reported beside every dimension rather than used as a filter,
because a thin station is not a good station -- it is an unjudged one, and
saying so is the honest output.
"""

from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import selection_market_dashboard as dash  # noqa: E402
from bayes_basket_target.center_width_store import (  # noqa: E402
    _truth,
    serving_cells_by_day,
)
from bayes_basket_target.fleet_regime_shift import (  # noqa: E402
    cell_residual_c,
)

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
payload = dash.build_payload(DB)
stats = {r["station_id"]: r for r in payload["station_stats"]}

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=120.0)

# --- 3. centre residual, from the served P0 posteriors ---------------------
truth = _truth(conn)
by_day = serving_cells_by_day(
    conn, truth, stage="P0",
    min_decision_at_utc="2026-08-14T00:00:00Z", pre_band=False,
)
residual: dict[str, list[float]] = defaultdict(list)
for day, cells in by_day.items():
    per: dict[str, list[float]] = defaultdict(list)
    for cell in cells:
        actual = truth.get((cell["station_id"], cell["event_date"]))
        if actual is None:
            continue
        value = cell_residual_c(cell, actual)
        if value is not None:
            per[str(cell["station_id"])].append(value)
    for station, values in per.items():
        residual[station].append(sum(values) / len(values))

# --- 4. coverage, and 5. persistence, from the settled chains -------------
rows = conn.execute(
    "SELECT s.station_id, s.event_date, s.settled_bucket, s.pnl_usdc,"
    " p.intended_legs_json, p.posted_buckets_json, p.window_buckets_json"
    " FROM paper_trade_settlement s"
    " JOIN paper_trade_position p USING(station_id, event_date)"
    " WHERE s.settled_bucket IS NOT NULL",
).fetchall()
import json

chains: dict[tuple[str, str], dict] = {}
for station, day, landed, pnl, ij, pj, wj in rows:
    key = (str(station), str(day))
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

days = sorted({day for _s, day in chains})
cut = days[len(days) // 2] if days else ""
covered: dict[str, list[int]] = defaultdict(list)
half_pnl: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
half_n: dict[str, list[int]] = defaultdict(lambda: [0, 0])
for (station, day), chain in chains.items():
    covered[station].append(int(chain["landed"] in chain["chosen"]))
    index = 0 if day < cut else 1
    half_pnl[station][index] += chain["pnl"]
    half_n[station][index] += 1


def flag(value, bad) -> str:
    return "✗" if value is not None and bad(value) else "·"


print(f"分割点 {cut}；中心残差取 P0 服务后验，08-14 起\n")
header = (
    "站     结算 覆盖率  实现/期望   命中/声称  中心残差  前半段   后半段  "
    "不行的维度"
)
print(header)
print("-" * len(header))

verdicts = []
for station in sorted(stats):
    row = stats[station]
    settled = int(row["settled_count"] or 0)
    realized = float(row["realized_usdc"] or 0.0)
    expected = float(row.get("expected_usdc") or 0.0)
    ratio = realized / expected if abs(expected) > 1e-9 else None
    # The dashboard's own hit-rate-over-claim, not a second copy of it.
    calib = row.get("calibration_ratio")
    res = mean(residual[station]) if residual.get(station) else None
    cov = mean(covered[station]) if covered.get(station) else None
    a, b = half_pnl[station]
    na, nb = half_n[station]

    marks = {
        "实现": flag(ratio, lambda v: v < 0),
        "声称": flag(calib, lambda v: v < 0.75),
        "中心": flag(res, lambda v: abs(v) > 0.8),
        "覆盖": flag(cov, lambda v: v < 0.45),
        "持续": "✗" if (na and nb and a < 0 and b < 0) else "·",
    }
    bad = sum(1 for v in marks.values() if v == "✗")
    verdicts.append((bad, realized, station, settled, marks, {
        "ratio": ratio, "calib": calib, "res": res, "cov": cov,
        "a": a, "b": b, "na": na, "nb": nb,
    }))

for bad, realized, station, settled, marks, d in sorted(
    verdicts, key=lambda t: (-t[0], t[1])
):
    if bad == 0:
        continue
    fmt = lambda v, s="{:.2f}": ("—" if v is None else s.format(v))
    print(
        f"{station:6s} {settled:>4d}"
        f" {fmt(d['cov'], '{:.0%}'):>6s}"
        f" {fmt(d['ratio']):>10s}"
        f" {fmt(d['calib']):>10s}"
        f" {fmt(d['res'], '{:+.2f}'):>9s}"
        f" {d['a']:>+7.1f} {d['b']:>+7.1f}  "
        + " ".join(f"{k}{v}" for k, v in marks.items() if v == "✗")
        + f"   [{bad}/5]"
    )

print("\n按不行的维度数分组:")
groups = defaultdict(list)
for bad, realized, station, settled, _m, _d in verdicts:
    groups[bad].append((station, settled, realized))
for bad in sorted(groups, reverse=True):
    names = ", ".join(
        f"{s}({n}链,{p:+.0f})" for s, n, p in
        sorted(groups[bad], key=lambda t: t[2])
    )
    print(f"  {bad}/5 项不行 ({len(groups[bad])} 站): {names}")

worst = [s for b, _r, s, _n, _m, _d in verdicts if b >= 3]
if worst:
    total = sum(float(stats[s]["realized_usdc"] or 0.0) for s in stats)
    kept = sum(
        float(stats[s]["realized_usdc"] or 0.0)
        for s in stats if s not in set(worst)
    )
    print(f"\n剔掉 ≥3 项不行的 {len(worst)} 站: "
          f"{total:+.2f} -> {kept:+.2f} USDC ({kept-total:+.2f})")
    print(f"  它们是: {', '.join(sorted(worst))}")
