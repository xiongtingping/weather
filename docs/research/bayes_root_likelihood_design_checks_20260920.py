"""Bounded, synthetic arithmetic checks for the 2026-09-20 design.

This does not import production code, connect to a market, train a model, or
claim to verify the deployed system. Run with Python 3; only stdlib is used.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import random
import resource
import shutil
import sys
import threading
import time


HERE = Path(__file__).resolve().parent
REPORT = HERE / "bayes_root_likelihood_design_checks_20260920.json"
MAX_RSS_BYTES = 128 * 1024 * 1024
MIN_FREE_BYTES = 1024 * 1024 * 1024
MAX_SECONDS = 30
MAX_REPORT_BYTES = 128 * 1024
START = time.monotonic()
STOP = threading.Event()
CHECKS: list[dict] = []
ASSERTIONS = 0


def check_resources() -> dict:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        rss *= 1024
    free = shutil.disk_usage(HERE).free
    elapsed = time.monotonic() - START
    if rss <= 0 or free < MIN_FREE_BYTES or rss > MAX_RSS_BYTES or elapsed > MAX_SECONDS:
        raise RuntimeError("resource_guard_failed")
    return {"peak_rss_bytes": rss, "free_disk_bytes": free, "elapsed_seconds": elapsed}


def watchdog() -> None:
    while not STOP.wait(0.1):
        try:
            check_resources()
        except Exception:
            os._exit(124)


def require(ok: bool, label: str) -> None:
    global ASSERTIONS
    ASSERTIONS += 1
    if not ok:
        raise AssertionError(label)


def close(a: float, b: float, label: str) -> None:
    require(math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-10), label)


def vector_close(a: list[float], b: list[float], label: str) -> None:
    require(len(a) == len(b), label + ":length")
    for left, right in zip(a, b):
        close(left, right, label)


def must_reject(fn, label: str) -> None:
    try:
        fn()
    except ValueError:
        require(True, label)
    else:
        require(False, label)


def posterior(root, log_update, mask=None, floor=0.0):
    if mask is None:
        mask = [True] * len(root)
    if not root or len(root) != len(log_update) or len(root) != len(mask):
        raise ValueError("dimension_invalid")
    if not math.isfinite(floor) or not 0 <= floor < 1:
        raise ValueError("floor_invalid")
    if any(not math.isfinite(p) or p < 0 for p in root):
        raise ValueError("root_invalid")
    if not math.isclose(sum(root), 1.0, abs_tol=1e-10):
        raise ValueError("root_not_normalized")
    if any(not math.isfinite(v) for v in log_update):
        raise ValueError("likelihood_invalid")
    logits = [math.log(p) + v if keep and p > 0 else -math.inf
              for p, v, keep in zip(root, log_update, mask)]
    top = max(logits)
    if not math.isfinite(top):
        raise ValueError("empty_support")
    scaled = [math.exp(v - top) for v in logits]
    total = sum(scaled)
    result = [v / total for v in scaled]
    if floor:
        # Example of an explicitly shared served floor, not an activation choice.
        result = [max(v, floor) if keep else 0.0 for v, keep in zip(result, mask)]
        total = sum(result)
        result = [v / total for v in result]
    return result


def evidence_posterior(root, evidence):
    unique = {}
    for identity, values in evidence:
        values = tuple(values)
        if identity in unique and unique[identity] != values:
            raise ValueError("same_identity_conflicting_content")
        unique[identity] = values
    # Only a toy for fixed log factors. Conditional learned heads must be
    # recomputed on a canonical evidence set, not inferred from this test.
    total = [0.0] * len(root)
    for identity in sorted(unique):
        values = unique[identity]
        if len(values) != len(root):
            raise ValueError("evidence_shape")
        total = [a + b for a, b in zip(total, values)]
    return posterior(root, total)


def depth_cost(quantity, levels):
    if quantity < 0:
        raise ValueError("negative_quantity")
    left, cost, last = quantity, 0.0, -math.inf
    for price, size in levels:
        if not 0 < price < 1 or size < 0 or price < last:
            raise ValueError("book_invalid")
        last = price
        take = min(left, size)
        cost += take * price
        left -= take
        if left <= 1e-12:
            return cost
    raise ValueError("insufficient_depth")


def pnl(q, cost):
    return [v - cost for v in q]


def ev(probabilities, profits):
    return sum(p * value for p, value in zip(probabilities, profits))


def run_group(name, fn):
    before = ASSERTIONS
    detail = fn()
    CHECKS.append({"name": name, "assertions": ASSERTIONS - before, "status": "PASS", "detail": detail})
    check_resources()


def identity_and_double_count():
    root = [0.8, 0.2]
    vector_close(posterior(root, [0.0, 0.0]), root, "identity")
    wrong = posterior(root, [math.log(p) for p in root])
    close(wrong[0], 16 / 17, "prediction_is_not_increment")
    evidence = [("single_source", [math.log(4), 0.0])]
    vector_close(evidence_posterior([0.5, 0.5], evidence * 10), root, "duplicate_ten_times")
    must_reject(lambda: evidence_posterior(root, [("x", [1, 0]), ("x", [0, 1])]), "conflicting_identity")
    return {"identity": root, "wrong_double_count": wrong}


def canonical_replay_and_root_rebase():
    source = [("TAF", [math.log(4), 0]), ("REGIONAL", [0, math.log(2)])]
    expected = evidence_posterior([0.5, 0.5], source)
    vector_close(evidence_posterior([0.5, 0.5], list(reversed(source))), expected, "arrival_order")
    for _ in range(50):
        vector_close(evidence_posterior([0.5, 0.5], source * 3), expected, "replay")
    old = evidence_posterior([0.8, 0.2], [("O", [0, math.log(4)])])
    rebased = evidence_posterior([0.2, 0.8], [("O", [0, math.log(4)])])
    vector_close(old, [0.5, 0.5], "old_root")
    vector_close(rebased, [1 / 17, 16 / 17], "new_root")
    require(rebased != old, "root_change_not_old_posterior")
    return {"rebased": rebased}


def extreme_gaussian():
    results = []
    centers = [24.0, 25.0, 26.0]
    for tx in [35.0, 36.0, 35.0, -35.0, 36.0]:
        logs = [-(tx - c) ** 2 / (2 * 0.25**2) for c in centers]
        value = posterior([1 / 3] * 3, logs)
        close(sum(value), 1, "extreme_sum")
        target = 0 if tx < 0 else 2
        require(value[target] > 0.999999, "extreme_no_identity_fallback")
        results.append({"tx": tx, "posterior": value})
    return results


def support_and_numerical_invariants():
    rng = random.Random(20260920)
    for iteration in range(300):
        count = rng.randint(2, 15)
        weights = [rng.random() + 1e-6 for _ in range(count)]
        total = sum(weights)
        root = [w / total for w in weights]
        logs = [rng.uniform(-800, 800) for _ in range(count)]
        mask = [rng.choice([False, True]) for _ in range(count)]
        mask[iteration % count] = True
        result = posterior(root, logs, mask)
        shifted = posterior(root, [v + 12345.0 for v in logs], mask)
        vector_close(result, shifted, "log_offset_invariance")
        close(sum(result), 1, "normalization")
        for value, keep in zip(posterior(root, logs, mask, 0.001), mask):
            require(value >= 0 and math.isfinite(value), "finite_probability")
            if not keep:
                close(value, 0, "floor_never_resurrects_mask")
    must_reject(lambda: posterior([0.5, 0.5], [0, 0], [False, False]), "empty_support")
    must_reject(lambda: posterior([1.0, 0.0], [0, 0], [False, True]), "zero_root_on_support")
    must_reject(lambda: posterior([0.5, 0.5], [float("nan"), 0]), "nan")
    must_reject(lambda: posterior([0.5, 0.5], [float("inf"), 0]), "infinite_log_evidence")
    return {"random_cases": 300, "seed": 20260920}


def geometry_roundtrips():
    def c_to_f(v):
        return v * 9 / 5 + 32

    def f_to_c(v):
        return (v - 32) * 5 / 9

    def survives(upper, upper_closed, lower_bound):
        return upper > lower_bound or (upper_closed and upper == lower_bound)

    rng = random.Random(773)
    for _ in range(300):
        lo = rng.uniform(-80, 60)
        width = rng.uniform(0.01, 5)
        hi = lo + width
        for _ in range(4):
            lo, hi = f_to_c(c_to_f(lo)), f_to_c(c_to_f(hi))
        close(hi - lo, width, "geometry_roundtrip")
    close(f_to_c(82) - f_to_c(80), 10 / 9, "fahrenheit_width_from_boundaries")
    require(survives(math.inf, False, 34.0), "33_or_above_survives_34")
    require(not survives(33.5, False, 34), "finite_bucket_excluded")
    require(not survives(32.5, False, 32.5), "exclusive_boundary")
    require(survives(32.5, True, 32.5), "inclusive_boundary")
    require(survives(32.5, False, 32.49), "intersecting_bucket_retained")
    require(c_to_f(math.inf) == math.inf and f_to_c(-math.inf) == -math.inf, "infinite_tail_roundtrip")
    return {"random_intervals": 300, "roundtrips_each": 4}


def unequal_basket_and_pending():
    probabilities = [0.1, 0.5, 0.3, 0.1]
    q = [0, 20, 12, 0]
    cost = 20 * 0.37 + 12 * 0.25
    profits = pnl(q, cost)
    vector_close(profits, [-10.4, 9.6, 1.6, -10.4], "full_pnl")
    close(ev(probabilities, profits), 3.2, "full_ev")
    require(0.37 + 0.25 < 1 and min(profits) < 0, "sum_prices_not_guarantee")
    first = pnl([0, 20, 0, 0], 7.4)
    close(first[2], -7.4, "missing_winning_leg")
    actual_none = pnl([0, 0, 0, 0], 0)
    close(min(actual_none), 0, "unfilled_not_actual_loss")
    require(cost > 0, "pending_reserves_cash")
    close(ev(probabilities, [b - a for a, b in zip(first, profits)]), 0.6, "remaining_delta_ev")
    return {"cost": cost, "pnl": profits, "ev": ev(probabilities, profits), "first_leg_pnl": first}


def depth_and_minimums():
    levels = [(0.20, 5), (0.24, 7), (0.31, 4)]
    close(depth_cost(10, levels), 2.2, "walk_two_levels")
    close(depth_cost(16, levels), 3.92, "walk_full_depth")
    must_reject(lambda: depth_cost(17, levels), "insufficient_depth")
    costs = [depth_cost(q, levels) for q in range(17)]
    require(all(a <= b for a, b in zip(costs, costs[1:])), "monotone_cost")
    # At the venue minimum size, actual walked cost can exceed the budget.
    budget, venue_min_size = 2.0, 10
    require(depth_cost(venue_min_size, levels) > budget, "minimum_size_cannot_bypass_budget")
    conservative_reserve = 10 * 0.24
    require(conservative_reserve >= depth_cost(10, levels), "fok_limit_reserve")
    return {"cost_10": costs[10], "limit_reserve_10": conservative_reserve}


def negative_leg_execution():
    probs = [0.6, 0.3, 0.1]
    a = pnl([10, 0, 0], 4.0)
    b = pnl([0, 10, 0], 3.5)
    both = pnl([10, 10, 0], 7.5)
    close(ev(probs, a), 2.0, "positive_leg")
    close(ev(probs, b), -0.5, "negative_leg")
    close(ev(probs, both), 1.5, "positive_full_basket")
    path_evs = {str(chance): ev(probs, b) + chance * ev(probs, a) for chance in [0, 0.2, 0.25, 0.8, 1]}
    close(path_evs["0.2"], -0.1, "failure_probability_can_reverse_ev")
    close(path_evs["0.8"], 1.1, "execution_ev_not_full_basket_ev")
    close(path_evs["0.25"], 0, "break_even_completion")
    vector_close([total - paid for total, paid in zip(both, a)], b, "past_positive_ev_not_new_edge")
    return {"full_ev": 1.5, "B_then_A_execution_ev": path_evs, "B_only_worst": min(b)}


def maker_adverse_selection():
    probability, price, fill_win, fill_lose = 0.6, 0.45, 0.1, 0.9
    fill_rate = probability * fill_win + (1 - probability) * fill_lose
    real_ev = probability * fill_win * (1 - price) - (1 - probability) * fill_lose * price
    wrong = fill_rate * (probability - price)
    close(real_ev, -0.129, "outcome_conditional_fill")
    close(wrong, 0.063, "independence_mispricing")
    require(real_ev < 0 < wrong, "adverse_selection_reverses_sign")
    return {"conditional_ev": real_ev, "incorrect_independent_ev": wrong, "fill_rate": fill_rate}


def portfolio_wealth_and_fill_replay():
    # One ledger, no simulated orders sent. Actual cash already paid for q.
    cash, marked_position, payout = 90.0, 8.0, 20.0
    equity = cash + marked_position
    close(cash + payout, equity - marked_position + payout, "no_double_mark")
    require(equity + payout != cash + payout, "marked_plus_payout_is_wrong")
    reserves = [40.0, 35.0]
    require(sum(reserves) + 20 > cash, "shared_cash_rejects_third_scope")
    historical_spend, new_reserved_total = 10.0, 85.0
    require(new_reserved_total <= cash, "current_cash_has_already_paid_historical_spend")
    require(historical_spend + new_reserved_total > cash, "subtracting_historical_spend_again_wrongly_refuses")
    fills = {}
    for fill_id, size, cost in [("fill-a", 2, 0.8), ("fill-a", 2, 0.8), ("fill-b", 3, 1.2)]:
        if fill_id in fills:
            require(fills[fill_id] == (size, cost), "duplicate_fill_identical")
        fills[fill_id] = (size, cost)
    close(sum(v[0] for v in fills.values()), 5, "fill_count_once")
    close(sum(v[1] for v in fills.values()), 2, "cost_count_once")
    target, filled, confirmed_pending = 20, 5, 8
    close(max(0, target - filled - confirmed_pending), 7, "pending_prevents_duplicate_order")
    close(pnl([filled, 0], 2)[0], 3, "pending_does_not_increase_actual_pnl")
    for _ in range(100):
        require(max(0, target - filled - confirmed_pending) == 7, "fixed_target_replay_no_budget_reset")
    return {"actual_wealth": cash + payout, "available_after_reserve": cash - sum(reserves)}


def total_inventory_sizing():
    # Analytic CRRA optimum for one binary YES with linear cost, for this
    # mathematical example only. Existing inventory remains in both outcomes.
    wealth, probability, price, gamma = 100.0, 0.6, 0.4, 4.0
    ratio = (probability * (1 - price) / ((1 - probability) * price)) ** (1 / gamma)
    target = wealth * (ratio - 1) / ((1 - price) + ratio * price)
    after_cash = wealth - price * target
    marginal = (probability * (1 - price) * (after_cash + target) ** (-gamma)
                - (1 - probability) * price * after_cash ** (-gamma))
    close(marginal, 0, "target_has_zero_marginal_utility")
    log_ratio = probability * (1 - price) / ((1 - probability) * price)
    log_target = wealth * (log_ratio - 1) / ((1 - price) + log_ratio * price)
    require(not math.isclose(target, log_target / 4, rel_tol=1e-4), "gamma_four_not_exact_quarter_kelly")
    def utility(extra):
        cash = after_cash - price * extra
        return (probability * (cash + target + extra) ** (1 - gamma)
                + (1 - probability) * cash ** (1 - gamma)) / (1 - gamma)
    require(utility(1) < utility(0), "do_not_buy_another_fraction_after_fill")
    require(utility(-1) < utility(0), "return_from_other_side_to_same_total_optimum")
    return {"crra_gamma_4_target_shares": target, "quarter_log_optimal_shares": log_target / 4}


def main() -> None:
    check_resources()
    worker = threading.Thread(target=watchdog, daemon=True)
    worker.start()
    groups = [
        identity_and_double_count,
        canonical_replay_and_root_rebase,
        extreme_gaussian,
        support_and_numerical_invariants,
        geometry_roundtrips,
        unequal_basket_and_pending,
        depth_and_minimums,
        negative_leg_execution,
        maker_adverse_selection,
        portfolio_wealth_and_fill_replay,
        total_inventory_sizing,
    ]
    try:
        for group in groups:
            run_group(group.__name__, group)
        report = {
            "schema": "bayes_root_likelihood_design_arithmetic_v1",
            "status": "PASS",
            "scope": "synthetic mathematical checks only; no production modules, training, network, SSOT, orders, or profitability validation",
            "groups": len(CHECKS),
            "assertions": ASSERTIONS,
            "checks": CHECKS,
            "resource_limits": {"rss_bytes": MAX_RSS_BYTES, "min_free_disk_bytes": MIN_FREE_BYTES, "seconds": MAX_SECONDS, "max_report_bytes": MAX_REPORT_BYTES},
            "resources": check_resources(),
        }
        encoded = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
        if len(encoded) > MAX_REPORT_BYTES:
            raise RuntimeError("report_too_large")
        REPORT.write_bytes(encoded)
        print(json.dumps({"status": "PASS", "groups": len(CHECKS), "assertions": ASSERTIONS, "report": str(REPORT)}, ensure_ascii=False))
    finally:
        STOP.set()
        worker.join(timeout=1)


if __name__ == "__main__":
    main()
