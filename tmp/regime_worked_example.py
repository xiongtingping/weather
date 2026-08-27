"""Work three real station-days through the shift, number by number.

For each: the posterior that was served, the same posterior after the shift
that day would have carried, where the truth landed, what the basket claimed
about itself before and after, and what the ledger paid.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from bayes_basket_target.center_width_calibration import (
    apply_center_width_calibration,
)
from bayes_basket_target.center_width_store import _truth, serving_cells_by_day

CASES = [
    ("RKSI", "2026-08-22"),
    ("ZGGG", "2026-08-23"),
    ("SAEZ", "2026-08-21"),
    ("ZGSZ", "2026-08-22"),
    ("ZUUU", "2026-08-26"),
]

conn = sqlite3.connect(
    f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro",
    uri=True, timeout=60.0,
)
table = json.loads(
    (ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
     / "fleet_regime_replay_table.json").read_text(encoding="utf-8")
)
base = json.loads((ROOT / "tmp" / "regime_chains_baseline.json").read_text())
arm = json.loads((ROOT / "tmp" / "regime_chains_regime.json").read_text())

truth = _truth(conn)
days = sorted({day for _station, day in CASES})
cells: dict[tuple[str, str], dict] = {}
for day in days:
    by_day = serving_cells_by_day(
        conn, truth, stage="P0",
        min_decision_at_utc=f"{day}T00:00:00Z", pre_band=False,
    )
    for cell in by_day.get(day, []):
        cells[(str(cell["station_id"]), str(cell["event_date"]))] = cell

for station, day in CASES:
    key = f"{station}|{day}"
    cell = cells.get((station, day))
    if cell is None or key not in base:
        print(f"\n=== {station} {day}: no P0 cell or chain ===")
        continue
    shift = float(
        ((table["stages"].get("P0") or {}).get(day) or {}).get(
            "regime_delta_c", 0.0
        )
    )
    keys = list(cell["ordered_keys"])
    centers = list(cell["key_centers_c"])
    before = dict(cell["probabilities"])
    total = sum(before.values())
    before = {k: v / total for k, v in before.items()}
    after, _ = apply_center_width_calibration(
        before, delta_c=0.0, width_lambda=1.0,
        ordered_keys=keys, key_centers_c=centers,
        excluded_buckets=tuple(cell.get("excluded_buckets") or ()),
        regime_delta_c=shift,
    )
    landed = cell["settled_bucket"]
    actual_c = truth.get((station, day))
    chosen_b = set(base[key]["chosen"])
    chosen_a = set(arm[key]["chosen"]) if key in arm else set()

    mean_b = sum(centers[i] * before[k] for i, k in enumerate(keys))
    mean_a = sum(centers[i] * after[k] for i, k in enumerate(keys))

    print(f"\n=== {station} {day} ===")
    print(f"  当日舰队偏移 {shift:+.3f}°C   真值 {actual_c:.1f}°C"
          f"（落在 {landed}）")
    print(f"  后验中心 {mean_b:.2f}°C -> {mean_a:.2f}°C")
    print("  逐桶概率：")
    for i, k in enumerate(keys):
        if before[k] < 0.02 and after[k] < 0.02:
            continue
        mark = "  <-- 真值桶" if k == landed else ""
        inb = "买" if k in chosen_b else " "
        print(f"    {k:>16s} {inb}  {before[k]:.3f} -> {after[k]:.3f}"
              f"  ({after[k]-before[k]:+.3f}){mark}")
    claim_b = sum(before.get(k, 0.0) for k in chosen_b)
    claim_a = sum(after.get(k, 0.0) for k in chosen_b)
    print(f"  基线选的桶 {sorted(chosen_b)}")
    print(f"    这一篮的声称胜率 {claim_b:.3f} -> {claim_a:.3f}"
          f"  ({claim_a-claim_b:+.3f})")
    if chosen_a and chosen_a != chosen_b:
        print(f"  偏移臂改选了 {sorted(chosen_a)}")
    print(f"  账本：投注 {base[key]['stake']:.2f} -> "
          f"{arm.get(key,{}).get('stake', float('nan')):.2f}"
          f"   盈亏 {base[key]['pnl']:+.2f} -> "
          f"{arm.get(key,{}).get('pnl', float('nan')):+.2f}")
