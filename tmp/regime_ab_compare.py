"""Compare the two arms honestly: common chains, and drop-the-best checks.

Two things have to be separated.  The arms do not settle the same chain set
-- a shifted posterior seats a different window and some station-days stop
seating at all -- so a headline difference partly measures which chains each
arm chose to be in.  The common set removes that.

And a ledger ROI difference over roughly two hundred chains is routinely the
work of two or three of them, so the standing rule is that it means nothing
until it survives dropping the best one, two and three contributors from
each side.
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
base = json.loads((ROOT / "tmp" / "regime_chains_baseline.json").read_text())
arm = json.loads((ROOT / "tmp" / "regime_chains_regime.json").read_text())

common = sorted(set(base) & set(arm))
only_base = sorted(set(base) - set(arm))
only_arm = sorted(set(arm) - set(base))

print(f"chains: baseline {len(base)}, regime {len(arm)},"
      f" common {len(common)}")
print(f"  seated only by baseline: {len(only_base)}")
print(f"  seated only by regime:   {len(only_arm)}")

# ---------------------------------------------------------------- coverage
cb = sum(1 for k in common if base[k]["covered"])
ca = sum(1 for k in common if arm[k]["covered"])
gained = [k for k in common if arm[k]["covered"] and not base[k]["covered"]]
lost = [k for k in common if base[k]["covered"] and not arm[k]["covered"]]
print(f"\ncoverage on the common set: {cb} -> {ca}"
      f"  ({100*cb/len(common):.1f}% -> {100*ca/len(common):.1f}%)")
print(f"  gained {len(gained)}, lost {len(lost)},"
      f" net {len(gained)-len(lost):+d}")

# A proportion difference on paired data: McNemar's discordant counts are the
# whole test, and with this few flips the normal approximation is not safe --
# report the exact two-sided binomial instead.
flips = len(gained) + len(lost)
if flips:
    from math import comb
    k = min(len(gained), len(lost))
    tail = sum(comb(flips, i) for i in range(0, k + 1)) / (2 ** flips)
    print(f"  exact two-sided p = {min(1.0, 2 * tail):.3f}"
          f"  (discordant pairs {flips})")

# ------------------------------------------------------------ miss offsets
def offsets(book, keys):
    out = {}
    for k in keys:
        if book[k]["covered"]:
            continue
        out[book[k]["offset"]] = out.get(book[k]["offset"], 0) + 1
    return out


ob, oa = offsets(base, common), offsets(arm, common)
print("\nmiss distribution on the common set:")
for off in sorted(set(ob) | set(oa)):
    print(f"  {off:+d}: {ob.get(off,0):3d} -> {oa.get(off,0):3d}"
          f"  ({oa.get(off,0)-ob.get(off,0):+d})")

# -------------------------------------------------------------------- P&L
pb = {k: base[k]["pnl"] for k in common}
pa = {k: arm[k]["pnl"] for k in common}
sb = {k: base[k]["stake"] for k in common}
sa = {k: arm[k]["stake"] for k in common}


def roi(pnl, stake, drop=0):
    keys = sorted(pnl, key=lambda k: -pnl[k])[drop:]
    total_stake = sum(stake[k] for k in keys)
    return (
        sum(pnl[k] for k in keys) / total_stake if total_stake else 0.0,
        sum(pnl[k] for k in keys),
    )


print("\nP&L on the common set, dropping the best contributors:")
print("  drop   baseline ROI    regime ROI     delta")
for drop in (0, 1, 2, 3):
    rb, tb = roi(pb, sb, drop)
    ra, ta = roi(pa, sa, drop)
    print(f"  {drop:>3}    {100*rb:+7.2f}%      {100*ra:+7.2f}%"
          f"     {100*(ra-rb):+6.2f} pts")

delta = [pa[k] - pb[k] for k in common]
moved = [k for k in common if abs(pa[k] - pb[k]) > 1e-9]
print(f"\nchains whose P&L moved at all: {len(moved)}/{len(common)}")
if moved:
    worst = sorted(moved, key=lambda k: pa[k] - pb[k])[:5]
    best = sorted(moved, key=lambda k: -(pa[k] - pb[k]))[:5]
    print(f"  total P&L change {sum(delta):+.2f} USDC"
          f"  (mean per moved chain {mean([pa[k]-pb[k] for k in moved]):+.3f})")
    def outcome(book, key):
        return "HIT" if book[key]["covered"] else f"{book[key]['offset']:+d}"

    print("  biggest losses:")
    for k in worst:
        print(f"    {k}  {pb[k]:+8.2f} -> {pa[k]:+8.2f}"
              f"   base={outcome(base, k)} arm={outcome(arm, k)}")
    print("  biggest gains:")
    for k in best:
        print(f"    {k}  {pb[k]:+8.2f} -> {pa[k]:+8.2f}"
              f"   base={outcome(base, k)} arm={outcome(arm, k)}")
