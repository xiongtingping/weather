"""Full window enumeration for one station-day, showing why nothing seated."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import paper_trading_ledger as L  # noqa: E402
from bayes_basket_target.basket_kernel.delta_v2 import probability_core  # noqa: E402
from bayes_basket_target.delta_v2_runtime import _support_windows  # noqa: E402

STATION, DAY = sys.argv[1], sys.argv[2]
STAGE = sys.argv[3] if len(sys.argv) > 3 else "P0"

conn = L._connect(L.DEFAULT_DB, writable=False)
stages = L._stage_distributions(conn, STATION, DAY)
payload = stages[STAGE]
quotes, observed_at = L._quotes_at(conn, STATION, DAY, payload["decision_at_utc"])
snapshot, probabilities, prices, depth = L._build_snapshot(
    station_id=STATION, event_date=DAY, observed_at=observed_at,
    buckets=payload["buckets"], quotes=quotes,
)
center = payload["center_bucket"] or max(probabilities, key=probabilities.get)
if center not in probabilities:
    center = max(probabilities, key=probabilities.get)
ordinals = {r.bucket: int(r.ordinal) for r in snapshot.buckets}
print(f"{STATION} {DAY} {STAGE}  decided {payload['decision_at_utc']}  book {observed_at}")
print(f"lineage centre = {payload['center_bucket']!r} -> used {center!r}")
print(f"{'bucket':>16} {'p':>7} {'ask':>7} {'all-in':>7} {'p-ask':>7}")
for r in snapshot.buckets:
    p = probabilities[r.bucket]
    ask = prices[r.bucket]
    allin = ask * (1.0 + L.FEE_RATE) if r.ask_available else None
    print(f"{r.bucket:>16} {p:7.3f} {ask:7.3f} "
          f"{'—' if allin is None else format(allin, '7.3f')} "
          f"{'—' if allin is None else format(p - allin, '7.3f')}")
core = probability_core(probabilities, ordinals=ordinals, center_bucket=center)
if len(core) > L.MAXIMUM_SUPPORT_BUCKETS:
    co = ordinals.get(center, ordinals[max(probabilities, key=probabilities.get)])
    core = frozenset(sorted(
        core, key=lambda b: (abs(ordinals[b] - co), -probabilities.get(b, 0.0))
    )[:L.MAXIMUM_SUPPORT_BUCKETS])
print("core (incumbent) =", sorted(core, key=lambda b: ordinals[b]))
windows, audit = _support_windows(
    snapshot=snapshot, desired=set(core) or {center},
    probabilities=probabilities, fee_rate=L.FEE_RATE,
    maximum_support_buckets=L.MAXIMUM_SUPPORT_BUCKETS,
    minimum_support_buckets=L.MINIMUM_SUPPORT_BUCKETS,
    center_bucket=center,
)
print("audit:", {k: v for k, v in audit.items() if k != "dispersion_support_size"})
print("dispersion:", audit["dispersion_support_size"])
print(f"admitted windows: {len(windows)}")
for w in windows[:8]:
    print("  ", w["buckets"], "p=%.3f" % w["win_probability"],
          "cost=%s" % (None if w["all_in_price"] is None else round(w["all_in_price"], 3)),
          "edge=%s" % (None if w["edge"] is None else round(w["edge"], 4)),
          "incumbent" if w["is_incumbent"] else "")
