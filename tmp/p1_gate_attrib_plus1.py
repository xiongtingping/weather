"""Why did P1 never join the 33 P0-only chains that missed by +1?

P0 opened on every one of these, so P1's refusal cannot honestly be
``chain_never_opened``.  For each chain read the P1 refusal row, the P1
bridge-history posterior that existed anyway, and how much mass that
posterior put on the bucket the day actually settled in.
"""

import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import selection_market_dashboard as dash

conn = sqlite3.connect(
    f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro",
    uri=True, timeout=60.0,
)

rows = conn.execute(
    "SELECT s.station_id, s.event_date, s.settled_bucket, p.stage,"
    " p.intended_legs_json, p.posted_buckets_json, p.window_buckets_json"
    " FROM paper_trade_settlement s"
    " JOIN paper_trade_position p USING(station_id, event_date)"
    " WHERE s.settled_bucket IS NOT NULL",
).fetchall()

chains = {}
for sid, d, landed, stage, ij, pj, wj in rows:
    key = (str(sid), str(d))
    c = chains.setdefault(key, {
        "landed": str(landed), "chosen": set(), "stages": set(),
    })
    c["stages"].add(str(stage))
    got = []
    try:
        got = [str(b) for b in (json.loads(ij) if ij else {}).keys()]
    except (TypeError, ValueError, AttributeError):
        got = []
    if not got:
        for fb in (pj, wj):
            try:
                got = [str(b) for b in json.loads(fb or "[]")]
            except (TypeError, ValueError):
                got = []
            if got:
                break
    c["chosen"].update(got)


def offset(chain):
    if chain["landed"] in chain["chosen"]:
        return 0
    ords = sorted({
        o for o in (dash._bucket_ordinal(b) for b in chain["chosen"])
        if o is not None
    })
    lo = dash._bucket_ordinal(chain["landed"])
    if lo is None or not ords:
        return None
    step = dash._bucket_step(chain["landed"])
    if lo > ords[-1]:
        return max(1, round((lo - ords[-1]) / step))
    if lo < ords[0]:
        return -max(1, round((ords[0] - lo) / step))
    return None


targets = [
    (sid, d, c) for (sid, d), c in chains.items()
    if offset(c) == 1 and c["stages"] == {"P0"}
]
print(f"P0-only +1 chains: {len(targets)}")

reasons = Counter()
detail = []
for sid, d, c in sorted(targets, key=lambda t: (t[1], t[0])):
    ref = conn.execute(
        "SELECT reason, detail, cycles_examined, last_decision_at_utc"
        " FROM paper_trade_refusal"
        " WHERE station_id=? AND event_date=? AND stage='P1'",
        (sid, d),
    ).fetchone()
    reason = ref[0] if ref else "(no refusal row)"
    reasons[reason] += 1
    detail.append((d, sid, reason, (ref[1] if ref else ""),
                   (ref[2] if ref else None), c["landed"]))

print("\n== P1 refusal reason on those chains ==")
for r, n in reasons.most_common():
    print(f"  {r}: {n}")

print("\n== per chain ==")
for d, sid, reason, det, cyc, landed in detail:
    print(f"  {d} {sid:6s} landed={landed:14s} reason={reason:22s}"
          f" cycles={cyc} detail={str(det)[:70]}")

# Did a P1 posterior exist anyway?  Bounded query, one row per chain.
print("\n== P1 posterior presence and mass on the truth bucket ==")
masses, gaps = [], []
missing = 0
for d, sid, reason, _det, _cyc, landed in detail:
    row = conn.execute(
        "SELECT envelope_json FROM basket_probability_bridge_history"
        " WHERE station_id=? AND event_date=? AND stage='P1'"
        " ORDER BY decision_at_utc DESC LIMIT 1",
        (sid, d),
    ).fetchone()
    if not row:
        missing += 1
        continue
    try:
        env = json.loads(row[0])
    except (TypeError, ValueError):
        missing += 1
        continue
    probs = None
    for key in ("bucket_probabilities", "probabilities",
                "bucket_probability_map"):
        candidate = env.get(key)
        if isinstance(candidate, dict) and candidate:
            probs = candidate
            break
    if probs is None:
        for value in env.values():
            if isinstance(value, dict) and value and all(
                isinstance(v, (int, float)) for v in value.values()
            ):
                probs = value
                break
    if not probs:
        missing += 1
        continue
    lo = dash._bucket_ordinal(landed)
    step = dash._bucket_step(landed)
    pts = [(dash._bucket_ordinal(str(k)), float(v))
           for k, v in probs.items()]
    pts = [(o, p) for o, p in pts if o is not None]
    total = sum(p for _, p in pts)
    if not pts or total <= 0 or lo is None:
        missing += 1
        continue
    m = sum(o * p for o, p in pts) / total
    mass = sum(p for o, p in pts if o == lo) / total
    masses.append(mass)
    gaps.append((lo - m) / step)
    print(f"  {d} {sid:6s} P1_mass_on_truth={mass:.3f}"
          f" P1_mean_gap={(lo - m) / step:+.2f} reason={reason}")

if masses:
    print(f"\n  n={len(masses)} (unparsed/missing {missing})")
    print(f"  P1 mass on truth bucket: median={median(masses):.3f}"
          f" mean={mean(masses):.3f}")
    print(f"  P1 mean gap: median={median(gaps):+.2f}"
          f" mean={mean(gaps):+.2f}")
    print(f"  mass>=0.10: {sum(1 for x in masses if x >= 0.10)}"
          f" | mass<0.03: {sum(1 for x in masses if x < 0.03)}")
