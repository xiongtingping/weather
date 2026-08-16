"""Per-scope replay of the paper ledger's P0 decision, with the reason."""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import paper_trading_ledger as L  # noqa: E402
from bayes_basket_target.basket_kernel.delta_v2 import probability_core  # noqa: E402
from bayes_basket_target.delta_v2_runtime import _support_windows  # noqa: E402

DAY = sys.argv[1] if len(sys.argv) > 1 else "2026-08-17"
STAGE = sys.argv[2] if len(sys.argv) > 2 else "P0"

conn = L._connect(L.DEFAULT_DB, writable=False)
scopes = [
    str(r[0]) for r in conn.execute(
        "SELECT DISTINCT station_id FROM basket_probability_bridge_history"
        " WHERE event_date=? ORDER BY station_id",
        (DAY,),
    )
]
print(f"{DAY} {STAGE}: bridge scopes = {len(scopes)}")
reasons: dict[str, list[str]] = defaultdict(list)
for station_id in scopes:
    stages = L._stage_distributions(conn, station_id, DAY)
    if STAGE not in stages:
        # distinguish "no bridge row for the stage" from "no book at any row"
        n = conn.execute(
            "SELECT count(*) FROM basket_probability_bridge_history"
            " WHERE station_id=? AND event_date=? AND stage=?",
            (station_id, DAY, STAGE),
        ).fetchone()[0]
        reasons["no_bridge_row" if not n else "no_book_at_any_decision"].append(
            f"{station_id}({n})"
        )
        continue
    payload = stages[STAGE]
    priced = L._quotes_at(conn, station_id, DAY, payload["decision_at_utc"])
    if priced is None:
        reasons["no_book_at_decision"].append(station_id)
        continue
    quotes, observed_at = priced
    built = L._build_snapshot(
        station_id=station_id, event_date=DAY, observed_at=observed_at,
        buckets=payload["buckets"], quotes=quotes,
    )
    if built is None:
        missing = [
            str(b[0]) for b in payload["buckets"] if str(b[0]) not in quotes
        ]
        reasons["incomplete_book"].append(f"{station_id}(missing={missing[:3]})")
        continue
    snapshot, probabilities, prices, depth = built
    center = payload["center_bucket"] or max(probabilities, key=probabilities.get)
    if center not in probabilities:
        center = max(probabilities, key=probabilities.get)
    ordinals = {row.bucket: int(row.ordinal) for row in snapshot.buckets}
    core = probability_core(probabilities, ordinals=ordinals, center_bucket=center)
    if len(core) > L.MAXIMUM_SUPPORT_BUCKETS:
        co = ordinals.get(center, ordinals[max(probabilities, key=probabilities.get)])
        core = frozenset(sorted(
            core, key=lambda b: (abs(ordinals[b] - co), -probabilities.get(b, 0.0))
        )[:L.MAXIMUM_SUPPORT_BUCKETS])
    try:
        windows, audit = _support_windows(
            snapshot=snapshot, desired=set(core) or {center},
            probabilities=probabilities, fee_rate=L.FEE_RATE,
            maximum_support_buckets=L.MAXIMUM_SUPPORT_BUCKETS,
            minimum_support_buckets=L.MINIMUM_SUPPORT_BUCKETS,
            center_bucket=center,
        )
    except Exception as exc:  # noqa: BLE001
        reasons["selection_failed"].append(f"{station_id}({exc})")
        continue
    pw = [
        r for r in windows
        if r.get("all_in_price") is not None and r.get("edge") is not None
    ]
    if not pw:
        reasons["no_priced_window"].append(
            f"{station_id}(enum={len(windows)},center_ref={audit['center_refused_window_count']},"
            f"short_ref={audit['short_refused_window_count']},min={audit['minimum_support_buckets']})"
        )
        continue
    seat = pw[0]
    fraction, stake = L._kelly_stake(
        win_probability=float(seat["win_probability"]),
        cost=float(seat["all_in_price"]), equity=1000.0,
    )
    if stake <= 0.0:
        reasons["no_positive_kelly"].append(
            f"{station_id}(p={seat['win_probability']:.3f},c={seat['all_in_price']:.3f},"
            f"edge={seat['edge']:.4f},n={len(seat['buckets'])})"
        )
        continue
    legs = [depth.get(b) or [] for b in seat["buckets"]]
    capacity = min((sum(l["shares"] for l in leg) for leg in legs), default=0.0)
    filled = L._fillable_size(
        legs=legs, win_probability=float(seat["win_probability"]),
        budget=stake, fee_rate=L.FEE_RATE, capacity=capacity,
    )
    if filled is None:
        reasons["no_depth"].append(
            f"{station_id}(cap={capacity:.0f},budget={stake:.2f},n={len(seat['buckets'])})"
        )
        continue
    reasons[f"SEATED_{len(seat['buckets'])}"].append(station_id)

for key in sorted(reasons):
    print(f"\n{key}: {len(reasons[key])}")
    print("   " + ", ".join(reasons[key]))
