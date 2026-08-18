"""Does the affine correction form earn its seat out of sample?

Three questions, all judged on a chronological holdout the fit never sees:
  A. When the current rule picks affine, is affine better than constant?
  B. Is the form itself useless, or only the selector?  (always-affine)
  C. Does a stronger selector (longer tail, harder margin) rescue it?

Scores are the module's own ``_metrics`` (brier/nll/crps) plus centre MSE,
so the numbers are in the same currency as the OOS gate.
"""

from __future__ import annotations

import math
import pickle
import random
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, "/Users/xiong/weather")

from bayes_basket_target import native_ensemble_oos as oos

CASES = "/Users/xiong/weather/tmp/affine_cases.pkl"
TRAIN_FRACTION = 0.6
MIN_TRAIN = 12
MIN_HOLDOUT = 5


def as_cases(rows):
    return [
        {"values": values, "actual_tmax_c": actual, "event_date": day}
        for day, values, actual in rows
    ]


AFFINE_SLOPE_MIN = 0.3
AFFINE_SLOPE_MAX = 1.7


def affine_coefficients(pairs):
    """The removed helper, kept local so this study runs against either revision."""
    if len(pairs) < 8:
        return None
    mean_x = sum(x for x, _y in pairs) / len(pairs)
    mean_y = sum(y for _x, y in pairs) / len(pairs)
    var_x = sum((x - mean_x) ** 2 for x, _y in pairs)
    if var_x <= 1e-9:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in pairs) / var_x
    if not AFFINE_SLOPE_MIN <= slope <= AFFINE_SLOPE_MAX:
        return None
    return mean_y - slope * mean_x, slope


def fit_constant(train):
    """The module's fit with the affine branch disabled.

    After the removal this is simply the module's fit; before it, the branch
    is suppressed so the two revisions produce the same constant baseline.
    """
    original = getattr(oos, "_affine_coefficients", None)
    if original is None:
        return oos._fit_parameters(train)
    oos._affine_coefficients = lambda pairs: None
    try:
        return oos._fit_parameters(train)
    finally:
        oos._affine_coefficients = original


def fit_affine_forced(train):
    """Constant fit re-formed as affine, when affine is estimable at all."""
    base = fit_constant(train)
    centers = [sum(c["values"]) / len(c["values"]) for c in train]
    actuals = [float(c["actual_tmax_c"]) for c in train]
    refit = affine_coefficients(list(zip(centers, actuals)))
    if refit is None:
        return None
    params = dict(base)
    params["correction_form"] = "affine"
    params["affine_intercept_c"] = refit[0]
    params["affine_slope"] = refit[1]
    errors = [a - (refit[0] + refit[1] * c) for c, a in zip(centers, actuals)]
    params["train_rmse_c"] = math.sqrt(sum(e * e for e in errors) / len(errors))
    return params


def fit_with_rule(train, tail, margin):
    """The production rule with the tail length and margin as knobs."""
    base = fit_constant(train)
    if len(train) < tail + 10:
        return base
    centers = [sum(c["values"]) / len(c["values"]) for c in train]
    actuals = [float(c["actual_tmax_c"]) for c in train]
    fit_pairs = list(zip(centers[:-tail], actuals[:-tail]))
    affine = affine_coefficients(fit_pairs)
    if affine is None:
        return base
    head_bias = sum(a - c for c, a in fit_pairs) / len(fit_pairs)
    intercept, slope = affine
    constant_err = sum(
        (a - (c + head_bias)) ** 2
        for c, a in zip(centers[-tail:], actuals[-tail:])
    )
    affine_err = sum(
        (a - (intercept + slope * c)) ** 2
        for c, a in zip(centers[-tail:], actuals[-tail:])
    )
    if affine_err < margin * constant_err:
        forced = fit_affine_forced(train)
        if forced is not None:
            return forced
    return base


def corrected_center(mean, params):
    """Form-aware centre, local so it survives the module losing the form.

    ``oos._corrected_center`` is constant-only after the removal; routing
    through it would silently compare affine against itself.
    """
    if str(params.get("correction_form") or "constant") == "affine":
        return (
            float(params["affine_intercept_c"])
            + float(params["affine_slope"]) * mean
        )
    return mean + float(params["bias_c"])


def apply_parameters(values, params):
    """``oos._apply_parameters`` with the form-aware centre reinstated."""
    current = [float(v) for v in values]
    mean = sum(current) / len(current)
    center = corrected_center(mean, params)
    spread_factor = float(params["spread_factor"])
    corrected = [center + spread_factor * (v - mean) for v in current]
    member_bandwidth = (
        max(oos.KERNEL_BANDWIDTH_C, float(params.get("train_rmse_c") or 0.0))
        if len(current) == 1
        else oos.KERNEL_BANDWIDTH_C
    )
    member = oos._probabilities(corrected, bandwidth_c=member_bandwidth)
    climate = oos._probabilities(
        [float(v) for v in params["climate_samples_c"]]
    )
    reliability = float(params["reliability_weight"])
    keys = set(member) | set(climate)
    result = {
        key: reliability * member.get(key, 0.0)
        + (1.0 - reliability) * climate.get(key, 0.0)
        for key in keys
    }
    total = sum(result.values())
    return {key: value / total for key, value in result.items()}


def score(params, holdout):
    """Per-day scores under one parameter set."""
    out = []
    for case in holdout:
        probability = apply_parameters(case["values"], params)
        metrics = oos._metrics(probability, float(case["actual_tmax_c"]))
        mean = sum(case["values"]) / len(case["values"])
        center = corrected_center(mean, params)
        out.append({
            "se": (float(case["actual_tmax_c"]) - center) ** 2,
            "ae": abs(float(case["actual_tmax_c"]) - center),
            **metrics,
        })
    return out


def paired_bootstrap(deltas, draws=20000, seed=11):
    """95% interval on the mean paired delta (affine minus constant)."""
    if not deltas:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    n = len(deltas)
    means = []
    for _ in range(draws):
        means.append(
            sum(deltas[rng.randrange(n)] for _ in range(n)) / n
        )
    means.sort()
    return (means[int(0.025 * draws)], means[int(0.975 * draws)])


def summarize(label, rows_a, rows_b, keys=("se", "crps", "nll", "brier")):
    """rows_a = affine-side day scores, rows_b = constant-side, paired."""
    print(f"\n{label}  (paired days n={len(rows_a)})")
    if not rows_a:
        print("  (empty)")
        return
    for key in keys:
        a = [row[key] for row in rows_a]
        b = [row[key] for row in rows_b]
        deltas = [x - y for x, y in zip(a, b)]
        mean_b = sum(b) / len(b)
        mean_d = sum(deltas) / len(deltas)
        low, high = paired_bootstrap(deltas)
        relative = 100.0 * mean_d / mean_b if mean_b else float("nan")
        verdict = "affine better" if high < 0 else (
            "constant better" if low > 0 else "no difference"
        )
        print(
            f"  {key:6s} constant={mean_b:8.4f}  delta={mean_d:+8.4f} "
            f"({relative:+6.2f}%)  95% CI [{low:+.4f}, {high:+.4f}]  {verdict}"
        )


def main() -> None:
    with open(CASES, "rb") as handle:
        data = pickle.load(handle)

    split_scopes = []
    for key, rows in sorted(data.items()):
        cases = as_cases(rows)
        cut = int(len(cases) * TRAIN_FRACTION)
        train, holdout = cases[:cut], cases[cut:]
        if len(train) < MIN_TRAIN or len(holdout) < MIN_HOLDOUT:
            continue
        split_scopes.append((key, train, holdout))

    print(f"scopes usable at {TRAIN_FRACTION:.0%} split: {len(split_scopes)}")
    print(f"train days median="
          f"{statistics.median(len(t) for _k, t, _h in split_scopes):.0f}"
          f"  holdout median="
          f"{statistics.median(len(h) for _k, _t, h in split_scopes):.0f}")

    # ---- A. the production rule, exactly as shipped -------------------
    picked_affine = []
    for key, train, holdout in split_scopes:
        params = oos._fit_parameters(train)
        if str(params.get("correction_form")) == "affine":
            picked_affine.append((key, train, holdout, params))
    print(
        f"\n[A] production rule (tail={oos.FIT_VALIDATION_TAIL_DAYS}, "
        f"margin=0.8) picks affine in {len(picked_affine)}/"
        f"{len(split_scopes)} scopes ({100.0 * len(picked_affine) / len(split_scopes):.0f}%)"
    )
    rows_a, rows_b = [], []
    for _key, train, holdout, params in picked_affine:
        rows_a.extend(score(params, holdout))
        rows_b.extend(score(fit_constant(train), holdout))
    summarize("[A] affine-picked scopes: affine minus constant", rows_a, rows_b)

    # ---- B. the form itself, selector removed -------------------------
    rows_a, rows_b, estimable = [], [], 0
    for _key, train, holdout in split_scopes:
        forced = fit_affine_forced(train)
        if forced is None:
            continue
        estimable += 1
        rows_a.extend(score(forced, holdout))
        rows_b.extend(score(fit_constant(train), holdout))
    print(f"\n[B] affine estimable in {estimable}/{len(split_scopes)} scopes")
    summarize("[B] ALL estimable scopes: always-affine minus constant",
              rows_a, rows_b)

    # ---- C. can a stronger selector rescue it? ------------------------
    print("\n[C] selector sweep (tail days x margin), affine-picked scopes only")
    print(f"  {'tail':>4} {'margin':>7} {'picked':>7} {'days':>6} "
          f"{'dMSE%':>8} {'dCRPS%':>8} {'dNLL%':>8}")
    for tail in (6, 10, 14, 20):
        for margin in (0.8, 0.6, 0.5):
            a_rows, b_rows, picked = [], [], 0
            for _key, train, holdout in split_scopes:
                params = fit_with_rule(train, tail, margin)
                if str(params.get("correction_form")) != "affine":
                    continue
                picked += 1
                a_rows.extend(score(params, holdout))
                b_rows.extend(score(fit_constant(train), holdout))
            if not a_rows:
                print(f"  {tail:>4} {margin:>7.2f} {picked:>7} {'0':>6}")
                continue
            cells = []
            for key in ("se", "crps", "nll"):
                a = sum(row[key] for row in a_rows) / len(a_rows)
                b = sum(row[key] for row in b_rows) / len(b_rows)
                cells.append(100.0 * (a - b) / b if b else float("nan"))
            print(
                f"  {tail:>4} {margin:>7.2f} {picked:>7} {len(a_rows):>6} "
                f"{cells[0]:>+8.2f} {cells[1]:>+8.2f} {cells[2]:>+8.2f}"
            )

    # ---- D. per-scope win/loss, to rule out a hidden winning subset ----
    wins = losses = 0
    per_scope = []
    for _key, train, holdout in split_scopes:
        forced = fit_affine_forced(train)
        if forced is None:
            continue
        a = score(forced, holdout)
        b = score(fit_constant(train), holdout)
        delta = (
            sum(row["crps"] for row in a) - sum(row["crps"] for row in b)
        ) / len(a)
        per_scope.append(delta)
        if delta < 0:
            wins += 1
        else:
            losses += 1
    print(
        f"\n[D] per-scope CRPS: always-affine wins {wins}, loses {losses} "
        f"of {wins + losses}; median delta={statistics.median(per_scope):+.4f}"
    )


if __name__ == "__main__":
    main()
