"""Robustness checks before removing the affine form.

1. The one selector cell that looked favourable (tail=14, margin=0.6) -- is it
   real or one outlier scope?
2. Does the verdict survive a different chronological split point?
3. Blast radius: under the production split (holdout = last min_holdout_days),
   how many scopes would change ``train_rmse_c`` if affine were removed?
"""

from __future__ import annotations

import pickle
import statistics
import sys

sys.path.insert(0, "/Users/xiong/weather")
sys.path.insert(0, "/Users/xiong/weather/tmp")

from affine_study import (
    as_cases,
    fit_affine_forced,
    fit_constant,
    fit_with_rule,
    paired_bootstrap,
    score,
)

from bayes_basket_target import native_ensemble_oos as oos

CASES = "/Users/xiong/weather/tmp/affine_cases.pkl"


def load():
    with open(CASES, "rb") as handle:
        return pickle.load(handle)


def splits(data, fraction, min_train=12, min_holdout=5):
    out = []
    for key, rows in sorted(data.items()):
        cases = as_cases(rows)
        cut = int(len(cases) * fraction)
        train, holdout = cases[:cut], cases[cut:]
        if len(train) < min_train or len(holdout) < min_holdout:
            continue
        out.append((key, train, holdout))
    return out


def main() -> None:
    data = load()

    # ---- 1. the favourable cell, scope by scope ----------------------
    print("=" * 72)
    print("1. tail=14 margin=0.6 -- the only cell that favoured affine")
    scoped = splits(data, 0.6)
    per_scope = []
    a_all, b_all = [], []
    for key, train, holdout in scoped:
        params = fit_with_rule(train, 14, 0.6)
        if str(params.get("correction_form")) != "affine":
            continue
        a = score(params, holdout)
        b = score(fit_constant(train), holdout)
        a_all.extend(a)
        b_all.extend(b)
        per_scope.append((
            sum(r["se"] for r in a) / len(a) - sum(r["se"] for r in b) / len(b),
            sum(r["crps"] for r in a) / len(a)
            - sum(r["crps"] for r in b) / len(b),
            key,
        ))
    per_scope.sort()
    print(f"   scopes picked: {len(per_scope)}")
    for dse, dcrps, key in per_scope:
        print(f"     dMSE={dse:+9.3f}  dCRPS={dcrps:+7.4f}  "
              f"{key[0]} {key[1][:26]} {key[3]}")
    for metric in ("se", "crps", "nll"):
        deltas = [
            x[metric] - y[metric] for x, y in zip(a_all, b_all)
        ]
        low, high = paired_bootstrap(deltas)
        mean = sum(deltas) / len(deltas)
        print(f"   {metric:5s} mean delta={mean:+.4f} "
              f"95% CI [{low:+.4f}, {high:+.4f}]")
    if per_scope:
        without_best = per_scope[1:]
        if without_best:
            print(f"   drop the single best scope -> mean dMSE="
                  f"{statistics.mean(d for d, _c, _k in without_best):+.3f}, "
                  f"mean dCRPS="
                  f"{statistics.mean(c for _d, c, _k in without_best):+.4f}")

    # ---- 2. split sensitivity ----------------------------------------
    print("=" * 72)
    print("2. does the verdict hold at other split points?")
    print(f"   {'split':>6} {'scopes':>7} {'picked':>7} {'days':>6} "
          f"{'dMSE%':>8} {'dCRPS%':>8} {'dNLL%':>8}")
    for fraction in (0.5, 0.6, 0.7):
        scoped = splits(data, fraction)
        a_rows, b_rows, picked = [], [], 0
        for _key, train, holdout in scoped:
            params = oos._fit_parameters(train)
            if str(params.get("correction_form")) != "affine":
                continue
            picked += 1
            a_rows.extend(score(params, holdout))
            b_rows.extend(score(fit_constant(train), holdout))
        cells = []
        for metric in ("se", "crps", "nll"):
            a = sum(r[metric] for r in a_rows) / len(a_rows)
            b = sum(r[metric] for r in b_rows) / len(b_rows)
            cells.append(100.0 * (a - b) / b)
        print(f"   {fraction:>6.1f} {len(scoped):>7} {picked:>7} "
              f"{len(a_rows):>6} {cells[0]:>+8.2f} {cells[1]:>+8.2f} "
              f"{cells[2]:>+8.2f}")

    # ---- 3. blast radius under the production split -------------------
    print("=" * 72)
    print("3. blast radius: production split (train = all but last H days)")
    for min_holdout in (3, 5, 7):
        affine_scopes = 0
        total = 0
        rmse_shift = []
        for _key, rows in sorted(data.items()):
            cases = as_cases(rows)
            if len(cases) < min_holdout + 5:
                continue
            train = cases[:-min_holdout]
            if len(train) < 5:
                continue
            total += 1
            params = oos._fit_parameters(train)
            if str(params.get("correction_form")) == "affine":
                affine_scopes += 1
                constant = fit_constant(train)
                rmse_shift.append(
                    params["train_rmse_c"] / constant["train_rmse_c"]
                )
        share = 100.0 * affine_scopes / total if total else 0.0
        median_shift = statistics.median(rmse_shift) if rmse_shift else 0.0
        print(f"   min_holdout={min_holdout}: {affine_scopes}/{total} scopes "
              f"({share:.0f}%) pick affine; median train_rmse_c ratio "
              f"affine/constant = {median_shift:.4f}")


if __name__ == "__main__":
    main()
