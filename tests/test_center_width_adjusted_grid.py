"""The width lane's grid path agrees with the scalar path bit for bit.

``center_width_adjusted_grid`` scores many (shift, lambda) pairs at once so
the width lane can refit on the evidence charter 202 stopped throwing away
(ten days took hours one pair at a time; the daily refit is killed at 900 s).
It is only allowed to exist because every row equals the scalar function's
output exactly, and every search built on it returns what the scalar search
returned -- asserted here with ``==``, never with a tolerance.
"""

from __future__ import annotations

import json
import random

import pytest

from bayes_basket_target import center_width_store as cws
from bayes_basket_target.center_width_calibration import (
    apply_center_width_calibration,
    center_width_adjusted_grid,
)


def _ladder(rng: random.Random, count: int, fahrenheit: bool):
    step = 10.0 / 9.0 if fahrenheit else 1.0
    keys = [f"b{index}" for index in range(count)]
    centers = [15.0 + index * step for index in range(count)]
    weights = [rng.random() ** 3 for _ in keys]
    weights[0] = rng.choice([0.0, 1e-9, 0.01, 0.2])
    total = sum(weights)
    return keys, centers, {key: value / total for key, value in zip(keys, weights)}


@pytest.mark.parametrize("seed", range(24))
def test_every_row_is_the_scalar_functions_output(seed: int) -> None:
    rng = random.Random(seed)
    keys, centers, probability = _ladder(rng, rng.choice([5, 8, 11]), seed % 2 == 1)
    open_edges = (
        (centers[0] + 0.5, centers[-1] - 0.5) if seed % 3 else None
    )
    excluded = [keys[-1]] if seed % 5 == 0 else []
    floor = rng.choice([0.0, 1.44])
    pairs = [
        (round(rng.uniform(-1.5, 1.5), 3), round(rng.uniform(0.5, 1.5), 3))
        for _ in range(30)
    ] + [(0.0, 1.0), (0.0, 0.75)]
    grid, refused = center_width_adjusted_grid(
        probability, ordered_keys=keys, key_centers_c=centers,
        shifts=[pair[0] for pair in pairs], lambdas=[pair[1] for pair in pairs],
        open_edge_bounds_c=open_edges, excluded_buckets=excluded,
        sigma_floor_rungs=floor,
    )
    for row, (shift, lam) in enumerate(pairs):
        adjusted, _ = apply_center_width_calibration(
            probability, delta_c=shift, width_lambda=lam, ordered_keys=keys,
            key_centers_c=centers, open_edge_bounds_c=open_edges,
            excluded_buckets=excluded, sigma_floor_rungs=floor,
        )
        if refused[row]:
            assert adjusted == probability
            continue
        assert [adjusted[key] for key in keys] == grid[row].tolist()


def _cells(rng: random.Random, days: int, per_day: int):
    by_day = {}
    for day in range(days):
        cells = []
        for _ in range(per_day):
            keys, centers, probability = _ladder(rng, 11, rng.random() < 0.3)
            cells.append({
                "probabilities": probability,
                "ordered_keys": keys,
                "key_centers_c": centers,
                "open_edge_bounds_c": (centers[0] + 0.5, centers[-1] - 0.5),
                "excluded_buckets": (),
                "settled_bucket": rng.choice(keys[2:9]),
            })
        by_day[f"2026-09-{10 + day:02d}"] = cells
    return by_day


def _slow_tiers(cells, *, sigma_floor_rungs, pooled_lambda):
    """``_search_tiers`` exactly as it read before the grid path."""

    by_tier = {}
    for cell in cells:
        by_tier.setdefault(cws._cell_tier(cell), []).append(cell)
    best = None
    for delta in cws.SHIFT_GRID_C:
        if abs(delta) > cws.MAX_ABS_SHIFT_C:
            continue
        lambdas, total = [], 0.0
        for tier in range(len(cws.WIDTH_TIER_BOUNDARIES_RUNGS) + 1):
            members = by_tier.get(tier, [])
            if not members:
                lambdas.append(float(pooled_lambda))
                continue
            tier_best = None
            for lam in cws.WIDTH_TIER_LAMBDA_GRID:
                if not cws.MIN_WIDTH_LAMBDA <= lam <= cws.MAX_WIDTH_LAMBDA:
                    continue
                value = cws._mean_scores(
                    members, delta_c=delta, width_lambda=lam,
                    sigma_floor_rungs=sigma_floor_rungs,
                )["logscore"] * len(members)
                if tier_best is None or value < tier_best[1]:
                    tier_best = (lam, value)
            lambdas.append(tier_best[0])
            total += tier_best[1]
        objective = total / max(1, len(cells))
        if best is None or objective < best[2]:
            best = (delta, tuple(lambdas), objective)
    return best


def test_the_searches_return_what_the_scalar_searches_returned(monkeypatch) -> None:
    rng = random.Random(202)
    by_day = _cells(rng, days=5, per_day=4)
    days = sorted(by_day)
    train = [cell for day in days for cell in by_day[day]]

    def run(fast: bool):
        if not fast:
            monkeypatch.setattr(cws, "_GRID_INDEX", {})
        out = {}
        with cws._round_memo():
            out["pooled"] = cws._search(train)
            out["floored"] = cws._search(train, sigma_floor_rungs=cws.SIGMA_FLOOR_RUNGS)
            out["tiers"] = (
                cws._search_tiers(train, sigma_floor_rungs=0.0, pooled_lambda=0.9)
                if fast else
                _slow_tiers(train, sigma_floor_rungs=0.0, pooled_lambda=0.9)
            )
            out["rolling"] = json.dumps(
                cws._rolling_origin_admission(by_day, days), sort_keys=True,
                default=str,
            )
        monkeypatch.undo()
        return out

    slow = run(fast=False)
    fast = run(fast=True)
    assert fast == slow
