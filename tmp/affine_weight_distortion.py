"""train_rmse_c is the cross-ensemble weight (score = 1/rmse).

The affine branch overwrites it with a two-parameter in-sample residual and
compares it against one-parameter residuals from every other system.  This
asks whether that lower number is skill or just the extra free parameter.
"""

from __future__ import annotations

import math
import pickle
import statistics
import sys

sys.path.insert(0, "/Users/xiong/weather")
sys.path.insert(0, "/Users/xiong/weather/tmp")

from affine_study import as_cases, corrected_center, fit_constant, fit_with_rule

CASES = "/Users/xiong/weather/tmp/affine_cases.pkl"
MIN_HOLDOUT = 5


def rmse(params, cases):
    total = 0.0
    for case in cases:
        mean = sum(case["values"]) / len(case["values"])
        center = corrected_center(mean, params)
        total += (float(case["actual_tmax_c"]) - center) ** 2
    return math.sqrt(total / len(cases))


def main() -> None:
    with open(CASES, "rb") as handle:
        data = pickle.load(handle)

    train_ratios = []
    holdout_ratios = []
    for _key, rows in sorted(data.items()):
        cases = as_cases(rows)
        if len(cases) < MIN_HOLDOUT + 12:
            continue
        train, holdout = cases[:-MIN_HOLDOUT], cases[-MIN_HOLDOUT:]
        params = fit_with_rule(train, 6, 0.8)
        if str(params.get("correction_form")) != "affine":
            continue
        constant = fit_constant(train)
        # What the weight formula is handed:
        train_ratios.append(
            params["train_rmse_c"] / constant["train_rmse_c"]
        )
        # What the same two forms actually deliver on unseen days:
        holdout_ratios.append(
            rmse(params, holdout) / rmse(constant, holdout)
        )

    n = len(train_ratios)
    train_median = statistics.median(train_ratios)
    holdout_median = statistics.median(holdout_ratios)
    print(f"affine-picking scopes: {n}")
    print()
    print("  ratio affine/constant RMSE  (below 1.0 = affine looks better)")
    print(f"    on TRAIN  (feeds the weight): median {train_median:.4f}"
          f"   mean {statistics.mean(train_ratios):.4f}")
    print(f"    on HOLDOUT (actual skill)  : median {holdout_median:.4f}"
          f"   mean {statistics.mean(holdout_ratios):.4f}")
    print()
    print(f"  => cross-ensemble score is 1/rmse, so these systems currently"
          f" receive")
    print(f"     {1.0 / train_median:.2f}x the weight-score their constant"
          f" fit would earn,")
    print(f"     while delivering {1.0 / holdout_median:.2f}x on unseen days.")
    print()
    better_on_train = sum(1 for r in train_ratios if r < 1.0)
    better_on_holdout = sum(1 for r in holdout_ratios if r < 1.0)
    print(f"  affine beats constant on train   in {better_on_train}/{n} scopes")
    print(f"  affine beats constant on holdout in {better_on_holdout}/{n} scopes")


if __name__ == "__main__":
    main()
