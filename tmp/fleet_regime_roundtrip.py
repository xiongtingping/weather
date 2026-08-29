"""Round-trip the fleet regime store on a synthetic database.

Exercises the write path, the as-of read, and the escalation ladder against
a hand-built series whose right answers are known by construction.
"""

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import fleet_regime_shift as frs

conn = sqlite3.connect(":memory:")
frs.ensure_fleet_regime_schema(conn)

series = [
    ("2026-08-20", 0.55, 40, "2026-08-21T02:00:00Z"),
    ("2026-08-21", 0.62, 41, "2026-08-22T02:00:00Z"),
    ("2026-08-22", 0.48, 39, "2026-08-23T02:00:00Z"),
    ("2026-08-23", 0.71, 42, "2026-08-24T02:00:00Z"),
]
for day, residual, n, seen in series:
    frs.upsert_daily_residual(
        conn, stage="P0", event_date=day, residual_c_mean=residual,
        station_count=n, cell_count=n * 3, truth_visible_at_utc=seen,
        now_utc="2026-08-24T03:00:00Z",
    )
conn.commit()

print("== as-of read walks the visibility timeline ==")
for as_of in ("2026-08-21T01:00:00Z", "2026-08-21T03:00:00Z",
              "2026-08-23T03:00:00Z", "2026-08-24T03:00:00Z"):
    visible = frs.visible_daily_series(conn, stage="P0", as_of_utc=as_of)
    print(f"  as_of={as_of} -> {[row['event_date'] for row in visible]}")

print("\n== offset as evidence accumulates ==")
for as_of, event_date in (
    ("2026-08-21T03:00:00Z", "2026-08-22"),
    ("2026-08-22T03:00:00Z", "2026-08-23"),
    ("2026-08-23T03:00:00Z", "2026-08-24"),
    ("2026-08-24T03:00:00Z", "2026-08-25"),
):
    receipt = frs.load_regime_shift(
        conn, stage="P0", event_date=event_date, as_of_utc=as_of
    )
    print(f"  serving {event_date} (as_of {as_of[:10]}):"
          f" tier={receipt['tier']:5s} run={receipt['run_length']}"
          f" shift={receipt['regime_delta_c']:+.3f}"
          f" alert={receipt['alert']} state={receipt['state']}")

print("\n== a revision restates its own day, never appends ==")
frs.upsert_daily_residual(
    conn, stage="P0", event_date="2026-08-23", residual_c_mean=0.10,
    station_count=42, cell_count=126,
    truth_visible_at_utc="2026-08-25T09:00:00Z",
    now_utc="2026-08-25T09:00:00Z",
)
conn.commit()
rows = conn.execute(
    "SELECT COUNT(*) FROM calibration_fleet_regime_daily WHERE stage='P0'"
).fetchone()[0]
after = frs.load_regime_shift(
    conn, stage="P0", event_date="2026-08-26",
    as_of_utc="2026-08-25T10:00:00Z",
)
print(f"  rows still {rows} (no duplicate day)")
print(f"  restated signal={after['signal_c']:+.2f}"
      f" run={after['run_length']} tier={after['tier']}"
      f" shift={after['regime_delta_c']:+.3f}")

print("\n== stages are independent books ==")
frs.upsert_daily_residual(
    conn, stage="P1", event_date="2026-08-23", residual_c_mean=-0.44,
    station_count=38, cell_count=114,
    truth_visible_at_utc="2026-08-24T02:00:00Z",
    now_utc="2026-08-24T03:00:00Z",
)
conn.commit()
p1 = frs.load_regime_shift(
    conn, stage="P1", event_date="2026-08-24",
    as_of_utc="2026-08-24T03:00:00Z",
)
print(f"  P1 signal={p1['signal_c']:+.2f} shift={p1['regime_delta_c']:+.3f}"
      f" (opposite sign to P0, as its own book)")

print("\n== composition with the fitted layer, and the entitlement wall ==")
from bayes_basket_target.center_width_calibration import (
    apply_center_width_calibration,
)

probability = {"28C": 0.15, "29C": 0.30, "30C": 0.35, "31C": 0.20}
keys = ["28C", "29C", "30C", "31C"]
centers = [28.0, 29.0, 30.0, 31.0]


def mean_of(dist):
    return sum(dist[k] * c for k, c in zip(keys, centers))


base = mean_of(probability)
plain, receipt_plain = apply_center_width_calibration(
    probability, delta_c=0.0, width_lambda=1.0,
    ordered_keys=keys, key_centers_c=centers,
)
shifted, receipt_shift = apply_center_width_calibration(
    probability, delta_c=0.0, width_lambda=1.0,
    ordered_keys=keys, key_centers_c=centers, regime_delta_c=0.4,
)
both, receipt_both = apply_center_width_calibration(
    probability, delta_c=-0.2, width_lambda=1.0,
    ordered_keys=keys, key_centers_c=centers, regime_delta_c=0.4,
)
print(f"  input mean            {base:.3f}")
print(f"  identity              {mean_of(plain):.3f}")
print(f"  regime +0.4 only      {mean_of(shifted):.3f}"
      f"  receipt regime={receipt_shift['regime_delta_c']:+.2f}"
      f" fitted={receipt_shift['fitted_delta_c']:+.2f}")
print(f"  fitted -0.2 + regime  {mean_of(both):.3f}"
      f"  receipt delta={receipt_both['delta_c']:+.2f}"
      f" (fitted {receipt_both['fitted_delta_c']:+.2f}"
      f" + regime {receipt_both['regime_delta_c']:+.2f})")
try:
    apply_center_width_calibration(
        probability, delta_c=0.0, width_lambda=1.0,
        ordered_keys=keys, key_centers_c=centers, regime_delta_c=1.6,
    )
    print("  ENTITLEMENT WALL DID NOT FIRE  <-- defect")
except ValueError as exc:
    print(f"  entitlement wall fires: {exc}")
try:
    apply_center_width_calibration(
        probability, delta_c=1.4, width_lambda=1.0,
        ordered_keys=keys, key_centers_c=centers, regime_delta_c=0.9,
    )
    print("  composed 2.3C accepted (each layer inside its own bound)")
except ValueError as exc:
    print(f"  composed refused: {exc}")
