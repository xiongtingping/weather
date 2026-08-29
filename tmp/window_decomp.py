"""Split the out-of-window losses into placement, width and the model itself.

Three things can put the settled bucket outside the posted window, and they
have different owners.  A window of a given width can always be slid onto the
truth after the fact, so width alone is never the binding constraint -- the
question is whether the width was enough to reach the truth from where the
model's own mass sits.  So each station-day is scored against the best window
of the SAME width the model's distribution would have chosen for itself:

  A  the posted window held the truth                     -- nothing lost
  B  it did not, but the model's own best window would    -- placement
  C  neither did                                          -- the model's mass
     is in the wrong place, and no placement of that width rescues it

For C the minimum width whose model-optimal window would have reached the
truth says how much wider the ladder would have had to be.
"""
import json, sqlite3, statistics, sys
from collections import Counter

c = sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro",
                    uri=True, timeout=60)
c.execute("PRAGMA query_only=1")
settled = {(str(r[0]), str(r[1])): str(r[2]) for r in c.execute(
    "SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}

# The opening decision is where placement is decided; later stages top up.
opening = {}
for r in c.execute(
    "SELECT station_id,event_date,decision_at_utc,ladder_center_bucket,"
    "window_buckets_json,probabilities_json FROM paper_trade_position"
    " ORDER BY decision_at_utc"
):
    k = (str(r[0]), str(r[1]))
    if k in settled and k not in opening:
        opening[k] = r
# What the chain ended up posting across every stage, which is the union the
# settlement actually had a chance against.
posted_union = {}
for r in c.execute(
    "SELECT station_id,event_date,window_buckets_json,posted_buckets_json"
    " FROM paper_trade_position"
):
    k = (str(r[0]), str(r[1]))
    if k not in settled:
        continue
    s = posted_union.setdefault(k, set())
    for field in (r[2], r[3]):
        try:
            s.update(str(b) for b in json.loads(field or "[]"))
        except ValueError:
            pass

def best_window(probs, ordinals, width):
    """The contiguous window of this width holding the most model mass."""
    order = sorted(ordinals, key=lambda b: ordinals[b])
    best, mass = None, -1.0
    for i in range(len(order) - width + 1):
        w = order[i:i + width]
        m = sum(float(probs.get(b, 0.0)) for b in w)
        if m > mass:
            best, mass = w, m
    return best, mass

buckets = Counter()
widths_needed = []
mass_actual, mass_ideal = [], []
union_hit = 0
for k, r in opening.items():
    probs = json.loads(r[5] or "{}")
    window = [str(b) for b in json.loads(r[4] or "[]")]
    if not window or not probs:
        continue
    ordinals = {b: i for i, b in enumerate(
        sorted(probs, key=lambda x: list(probs).index(x)))}
    # Ordinals must come from the bridge, not from dict order.
    row = c.execute(
        "SELECT envelope_json FROM basket_probability_bridge_history"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        " ORDER BY decision_at_utc LIMIT 1", k).fetchone()
    if not row:
        continue
    ordinals = json.loads(row[0])["bucket_ordinals"]
    tb = settled[k]
    if tb not in ordinals or any(b not in ordinals for b in window):
        continue
    W = len(window)
    ideal, m_ideal = best_window(probs, ordinals, W)
    m_actual = sum(float(probs.get(b, 0.0)) for b in window)
    mass_actual.append(m_actual); mass_ideal.append(m_ideal)
    if tb in window:
        buckets["A 已挂窗口罩住了真值"] += 1
    elif ideal and tb in ideal:
        buckets["B 模型自己的最优同宽窗能罩住 → 摆放损失"] += 1
    else:
        buckets["C 同宽度下模型最优窗也罩不住 → 模型质量摆错位置"] += 1
        for w in range(W + 1, len(ordinals) + 1):
            cand, _ = best_window(probs, ordinals, w)
            if cand and tb in cand:
                widths_needed.append(w)
                break
        else:
            widths_needed.append(len(ordinals))
    if tb in posted_union.get(k, set()):
        union_hit += 1

tot = sum(buckets.values())
print("已结算且能定位的站日: %d\n" % tot)
for k in sorted(buckets):
    print("  %-44s %3d  %5.1f%%" % (k, buckets[k], 100 * buckets[k] / tot))
print()
print("  已挂窗口平均覆盖模型质量 %.1f%%，同宽度最优窗可覆盖 %.1f%%（差 %.1f 个百分点）"
      % (100 * statistics.mean(mass_actual), 100 * statistics.mean(mass_ideal),
         100 * (statistics.mean(mass_ideal) - statistics.mean(mass_actual))))
print("  全链已挂桶的并集罩住真值: %d / %d = %.1f%%" % (union_hit, tot, 100 * union_hit / tot))
if widths_needed:
    cnt = Counter(widths_needed)
    print("\n  C 类若要罩住真值，模型最优窗需要的宽度:")
    for w in sorted(cnt):
        print("     %2d 桶 : %3d 个站日" % (w, cnt[w]))
    print("     中位需要 %.0f 桶（当前上限 4 桶）" % statistics.median(widths_needed))
