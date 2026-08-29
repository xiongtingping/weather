"""Does the next-day shift actually move what selection would see?

Before building a replay harness, ask whether the shift is large enough to
bite at all.  For every settled chain, take the posterior that was really
served, apply the shift that would really have been derived as of that
decision, and ask what moved:

  * the distribution's own mean, in buckets
  * the probability the truth bucket carries
  * the truth bucket's rank by mass -- the thing a mass-ranked window
    selector is actually reading

The transform is production's own ``apply_center_width_calibration`` and the
shift is production's own ``derive_offset`` over the real daily series.
Nothing here reimplements either; an audit that hand-copies the rule it is
auditing manufactures its own findings.
"""

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from bayes_basket_target.center_width_calibration import (
    apply_center_width_calibration,
    bucket_centers_from_intervals,
)
from bayes_basket_target.center_width_store import EDGE_WIDTH_C
from bayes_basket_target.fleet_regime_shift import (
    derive_offset,
    visible_daily_series,
)
from bayes_basket_target.probability_bridge import history_envelope_payload
import selection_market_dashboard as dash

conn = sqlite3.connect(
    f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro",
    uri=True, timeout=60.0,
)

# The chains the ledger settled, and what each chose, exactly as the
# dashboard's own miss-offset section reads them.
rows = conn.execute(
    "SELECT s.station_id, s.event_date, s.settled_bucket, p.stage,"
    " p.intended_legs_json, p.posted_buckets_json, p.window_buckets_json"
    " FROM paper_trade_settlement s"
    " JOIN paper_trade_position p USING(station_id, event_date)"
    " WHERE s.settled_bucket IS NOT NULL",
).fetchall()
chains: dict[tuple[str, str], dict] = {}
for sid, day, landed, stage, ij, pj, wj in rows:
    key = (str(sid), str(day))
    chain = chains.setdefault(key, {
        "landed": str(landed), "chosen": set(), "stages": set(),
    })
    chain["stages"].add(str(stage))
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


def offset_of(chain) -> int | None:
    ords = sorted({
        o for o in (dash._bucket_ordinal(b) for b in chain["chosen"])
        if o is not None
    })
    landed = dash._bucket_ordinal(chain["landed"])
    if landed is None or not ords:
        return None
    step = dash._bucket_step(chain["landed"])
    if chain["landed"] in chain["chosen"]:
        return 0
    if landed > ords[-1]:
        return max(1, round((landed - ords[-1]) / step))
    if landed < ords[0]:
        return -max(1, round((ords[0] - landed) / step))
    return None


shift_cache: dict[tuple[str, str], float] = {}


def shift_for(stage: str, event_date: str, as_of: str) -> float:
    key = (stage, event_date)
    if key not in shift_cache:
        series = visible_daily_series(conn, stage=stage, as_of_utc=as_of)
        receipt = derive_offset(series, event_date=event_date)
        # The served value, so a stage that is dark reads as zero here too.
        from bayes_basket_target.fleet_regime_shift import (
            FLEET_REGIME_SERVED_STAGES,
        )
        shift_cache[key] = (
            float(receipt["regime_delta_c"])
            if stage in FLEET_REGIME_SERVED_STAGES else 0.0
        )
    return shift_cache[key]


moves = {"mean_buckets": [], "truth_p_delta": [], "rank_delta": []}
rank_improved = rank_worsened = rank_same = 0
by_offset: dict[int, list[float]] = {}
crossings = Counter()
examined = 0
skipped = 0

for (sid, day), chain in sorted(chains.items()):
    # The P0 decision that opened this chain, read the way production reads
    # it: the stored column can be a stub whose lineage resolves elsewhere.
    row = conn.execute(
        "SELECT decision_at_utc, envelope_json, checkpoint_id,"
        " envelope_content_sha256"
        " FROM basket_probability_bridge_history"
        " WHERE station_id=? AND event_date=? AND stage='P0'"
        " ORDER BY decision_at_utc LIMIT 1",
        (sid, day),
    ).fetchone()
    if row is None:
        skipped += 1
        continue
    decided, stored, checkpoint, sha = row
    try:
        envelope = history_envelope_payload(
            conn, checkpoint_id=str(checkpoint),
            envelope_content_sha256=str(sha),
            stored=str(stored) if stored else None,
        )
    except Exception:
        skipped += 1
        continue
    probabilities = envelope.get("probabilities") or {}
    ordinals = envelope.get("bucket_ordinals") or {}
    lineage = envelope.get("lineage") or {}
    try:
        intervals = json.loads(
            str(lineage.get("bucket_intervals_c_json") or "{}")
        )
    except (TypeError, ValueError):
        skipped += 1
        continue
    if not probabilities or set(intervals) != set(probabilities):
        skipped += 1
        continue
    keys = sorted(probabilities, key=lambda k: ordinals.get(k, 0))
    try:
        centers = bucket_centers_from_intervals(
            intervals, ordered_keys=keys, edge_width_c=EDGE_WIDTH_C
        )
    except ValueError:
        skipped += 1
        continue

    delta = shift_for("P0", str(day), str(decided))
    if delta == 0.0:
        skipped += 1
        continue
    shifted, _receipt = apply_center_width_calibration(
        probabilities, delta_c=0.0, width_lambda=1.0,
        ordered_keys=keys, key_centers_c=centers, regime_delta_c=delta,
    )
    examined += 1

    step = dash._bucket_step(chain["landed"])
    before_mean = sum(
        centers[i] * probabilities[k] for i, k in enumerate(keys)
    ) / sum(probabilities.values())
    after_mean = sum(
        centers[i] * shifted[k] for i, k in enumerate(keys)
    ) / sum(shifted.values())
    moves["mean_buckets"].append((after_mean - before_mean) / step)

    truth = chain["landed"]
    if truth in probabilities:
        before_p = probabilities[truth]
        after_p = shifted[truth]
        moves["truth_p_delta"].append(after_p - before_p)
        order_before = sorted(
            keys, key=lambda k: -probabilities[k]
        ).index(truth)
        order_after = sorted(keys, key=lambda k: -shifted[k]).index(truth)
        moves["rank_delta"].append(order_after - order_before)
        if order_after < order_before:
            rank_improved += 1
        elif order_after > order_before:
            rank_worsened += 1
        else:
            rank_same += 1
        off = offset_of(chain)
        if off is not None:
            by_offset.setdefault(off, []).append(after_p - before_p)
            crossings[(off, "improved" if order_after < order_before
                       else "worse" if order_after > order_before
                       else "same")] += 1

print(f"chains examined {examined}, skipped {skipped}")
print(f"\nmean displacement: median "
      f"{median(moves['mean_buckets']):+.3f} buckets"
      f"  (mean {mean(moves['mean_buckets']):+.3f},"
      f" max |{max(abs(x) for x in moves['mean_buckets']):.3f}|)")
print(f"truth-bucket probability change: median "
      f"{median(moves['truth_p_delta']):+.4f}"
      f"  mean {mean(moves['truth_p_delta']):+.4f}")
print(f"truth-bucket rank: improved {rank_improved},"
      f" worsened {rank_worsened}, unchanged {rank_same}")

print("\nby what the chain actually did (offset 0 = hit):")
for off in sorted(by_offset):
    values = by_offset[off]
    label = "hit" if off == 0 else f"missed by {off:+d}"
    print(f"  {label:16s} n={len(values):3d}"
          f"  mean truth-p change {mean(values):+.4f}"
          f"  improved/same/worse "
          f"{crossings[(off,'improved')]}/{crossings[(off,'same')]}"
          f"/{crossings[(off,'worse')]}")
