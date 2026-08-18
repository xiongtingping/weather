"""Does removing the affine form put any scope's OOS gate pass at risk?

A scope keeps its P0 pointer as long as the builder writes a new evaluation
AND a matching activation in the same pass -- which it does whenever the new
evaluation PASSes.  So the rebind is only dangerous where dropping affine
makes the gate metrics worse.  The gate reads brier and crps (STRICT) and
nll (the tolerance channel), so this reports all three, per scope, on the
production split, for exactly the scopes that currently pick affine.
"""

from __future__ import annotations

import pickle
import sys

sys.path.insert(0, "/Users/xiong/weather")
sys.path.insert(0, "/Users/xiong/weather/tmp")

from affine_study import as_cases, fit_affine_forced, fit_constant, fit_with_rule, score

CASES = "/Users/xiong/weather/tmp/affine_cases.pkl"
MIN_HOLDOUT = 5


def main() -> None:
    with open(CASES, "rb") as handle:
        data = pickle.load(handle)

    improved = {"brier": 0, "crps": 0, "nll": 0}
    worsened = {"brier": 0, "crps": 0, "nll": 0}
    totals = {"brier": [0.0, 0.0], "crps": [0.0, 0.0], "nll": [0.0, 0.0]}
    both_worse = []
    scopes = 0

    for key, rows in sorted(data.items()):
        cases = as_cases(rows)
        if len(cases) < MIN_HOLDOUT + 12:
            continue
        train, holdout = cases[:-MIN_HOLDOUT], cases[-MIN_HOLDOUT:]
        shipped = fit_with_rule(train, 6, 0.8)
        if str(shipped.get("correction_form")) != "affine":
            continue
        scopes += 1
        after = score(fit_constant(train), holdout)   # what we will serve+score
        before = score(shipped, holdout)              # what the gate scored
        deltas = {}
        for metric in ("brier", "crps", "nll"):
            a = sum(r[metric] for r in after) / len(after)
            b = sum(r[metric] for r in before) / len(before)
            totals[metric][0] += a
            totals[metric][1] += b
            deltas[metric] = a - b
            if a < b:
                improved[metric] += 1
            elif a > b:
                worsened[metric] += 1
        # STRICT needs brier AND crps not worse; both worse is the risk case.
        if deltas["brier"] > 0 and deltas["crps"] > 0:
            both_worse.append((deltas["crps"], key))

    print(f"affine-picking scopes on the production split: {scopes}")
    print()
    print("  gate metric movement, constant (after) vs affine (before)")
    print("  negative delta = the new evaluation scores BETTER than the one")
    print("  the current activation was granted on")
    for metric in ("brier", "crps", "nll"):
        a, b = totals[metric]
        print(
            f"    {metric:6s} mean {b / scopes:8.4f} -> {a / scopes:8.4f}  "
            f"({100.0 * (a - b) / b:+6.2f}%)   "
            f"improves in {improved[metric]:>2}/{scopes} scopes, "
            f"worsens in {worsened[metric]:>2}"
        )
    print()
    print(f"  scopes where BOTH brier and crps worsen (STRICT path at risk): "
          f"{len(both_worse)}/{scopes}")
    for delta, key in sorted(both_worse, reverse=True)[:10]:
        print(f"    dCRPS={delta:+.4f}  {key[0]} {key[1][:28]} {key[3]}")


if __name__ == "__main__":
    main()
