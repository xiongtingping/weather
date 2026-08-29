"""Does the seated window lean toward the heavier side of the centre?

For every P0 intended basket: anchor at the production mean centre bucket
m, read the FULL served distribution's mass two rungs out on each side
(hot2 = p(m+1)+p(m+2), cold2 = p(m-1)+p(m-2)), classify the intended
window's shape by its offsets from m, and check whether an asymmetric
window leans toward the heavier side.  Cut by the charter-55 skew state,
symmetric days being the question asked (2026-08-26): there the skew gate
is silent and the curve score alone picks the lean.
"""
import json
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.delta_v2_runtime import _skew_extension_sign

conn = sqlite3.connect(
    "file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro",
    uri=True,
)
rows = conn.execute(
    "SELECT p.event_date, p.station_id, p.ladder_center_bucket,"
    " p.probabilities_json, p.intended_legs_json,"
    " s.settled_bucket"
    " FROM paper_trade_position p"
    " LEFT JOIN paper_trade_settlement s"
    " ON s.station_id = p.station_id AND s.event_date = p.event_date"
    " WHERE p.stage = 'P0'"
).fetchall()

SKEW_NAME = {1: "right", 0: "sym", -1: "left"}
by_cell = defaultdict(list)
seen = set()
for event_date, station, center, prob_json, intended_json, settled in rows:
    if (event_date, station) in seen:
        continue
    seen.add((event_date, station))
    probabilities = {
        str(k): float(v) for k, v in (json.loads(prob_json) or {}).items()
    }
    ordinals = {b: i for i, b in enumerate(probabilities)}
    if center not in ordinals:
        continue
    m = ordinals[center]
    window = sorted(
        (b for b in (json.loads(intended_json) or {}) if b in ordinals),
        key=lambda b: ordinals[b],
    )
    if not window:
        continue
    offsets = [ordinals[b] - m for b in window]
    lo, hi = min(offsets), max(offsets)
    width = len(window)
    by_ord = {i: probabilities[b] for b, i in ordinals.items()}
    hot2 = by_ord.get(m + 1, 0.0) + by_ord.get(m + 2, 0.0)
    cold2 = by_ord.get(m - 1, 0.0) + by_ord.get(m - 2, 0.0)
    skew = _skew_extension_sign(probabilities, ordinals)
    # window lean: which side holds more rungs around the anchor
    lean = "hot" if hi > -lo else ("cold" if -lo > hi else "even")
    truth_ord = ordinals.get(str(settled or "")) if settled else None
    by_cell[(SKEW_NAME[skew], width, f"[{lo:+d}..{hi:+d}]")].append({
        "lean": lean,
        "hot2": hot2,
        "cold2": cold2,
        "mass_hot": hot2 > cold2,
        "settled": truth_ord is not None,
        "bias": None if truth_ord is None else m - truth_ord,
        "cover": None if truth_ord is None else (lo <= truth_ord - m <= hi),
        "tag": f"{event_date} {station}",
    })

print(f"total P0 rows: {sum(len(v) for v in by_cell.values())}")
print()
for skew_state in ("sym", "right", "left"):
    cells = {k: v for k, v in by_cell.items() if k[0] == skew_state}
    total = sum(len(v) for v in cells.values())
    print(f"=== skew={skew_state}  ({total} station-days) ===")
    for key in sorted(cells, key=lambda k: (k[1], k[2])):
        members = cells[key]
        _, width, shape = key
        n = len(members)
        lean = members[0]["lean"]
        agree = sum(
            1 for e in members
            if (e["lean"] == "hot") == e["mass_hot"]
        ) if lean != "even" else None
        mean_gap = sum(e["hot2"] - e["cold2"] for e in members) / n
        settled = [e for e in members if e["settled"]]
        hits = sum(1 for e in settled if e["cover"])
        line = (
            f"  {width}桶 {shape:<10} n={n:<3} lean={lean:<4}"
            f" mass(hot2-cold2)均值={mean_gap:+.3f}"
        )
        if lean != "even":
            line += f"  方向与质量一致 {agree}/{n}"
        if settled:
            line += f"  盖住 {hits}/{len(settled)}"
        print(line)
    # the asymmetric-window question, pooled within this skew state
    asym = [
        e for v in cells.values() for e in v if e["lean"] != "even"
    ]
    if asym:
        agree = sum(
            1 for e in asym if (e["lean"] == "hot") == e["mass_hot"]
        )
        print(
            f"  -- 不对称窗合计 {len(asym)}：扩边方向与两侧质量一致"
            f" {agree}/{len(asym)} = {agree / len(asym):.0%}"
        )
        bad = [e for e in asym if (e["lean"] == "hot") != e["mass_hot"]]
        for e in bad[:8]:
            side = "热倾" if e["lean"] == "hot" else "冷倾"
            print(
                f"     不一致例: {e['tag']}  {side}窗但质量在另一侧"
                f" (hot2={e['hot2']:.3f} cold2={e['cold2']:.3f})"
                + ("" if e["bias"] is None else f"  偏差={e['bias']:+d}")
            )
    print()
