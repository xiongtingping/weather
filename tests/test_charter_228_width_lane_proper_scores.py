"""Charter 228: the width lane is judged by charters 211 and 224.

Every verdict the lane reaches changes what P0 serves, so each needs the bucket
Brier and the LogScore both significantly lower (station-day bootstrap), both
scored on the vector serving serves -- after the 1e-3 floor.  The tier search
is fitted on the LogScore alone; the old single-score hurdle, its 1e-6 floor
and the window term are gone.
"""

from __future__ import annotations

import math
import random
from pathlib import Path

import pytest

from bayes_basket_target import center_width_store as cws
from bayes_basket_target.center_width_calibration import apply_center_width_calibration
from bayes_basket_target.materialization import (
    PROBABILITY_MODEL_ZERO_FLOOR,
    floor_probabilities,
)

ROOT = Path(__file__).resolve().parents[1]


def _cell(rng: random.Random, *, zeros: bool, excluded: bool) -> dict:
    keys = [f"{20 + index}°C" for index in range(11)]
    centres = [20.0 + index for index in range(11)]
    weights = [rng.random() ** 3 for _ in keys]
    if zeros:
        weights[0] = weights[-1] = 0.0
    total = sum(weights)
    return {
        "station_id": "XXXX",
        "probabilities": {key: value / total for key, value in zip(keys, weights)},
        "ordered_keys": keys,
        "key_centers_c": centres,
        "open_edge_bounds_c": (centres[0] + 0.5, centres[-1] - 0.5),
        "excluded_buckets": (keys[-1],) if excluded else (),
        "settled_bucket": rng.choice(keys[1:9]),
    }


def test_a_zero_rung_hit_is_charged_what_serving_charges() -> None:
    cell = _cell(random.Random(1), zeros=True, excluded=False)
    cell["settled_bucket"] = cell["ordered_keys"][0]            # the zero rung
    logscore, brier = cws._cell_proper_scores(cell, delta_c=0.0, width_lambda=1.0)
    served, lifted = floor_probabilities(dict(cell["probabilities"]))
    assert lifted                                              # the floor acted
    assert logscore == -math.log(served[cell["ordered_keys"][0]])
    assert logscore < -math.log(1e-6)                          # not 13.8 nats
    assert logscore == pytest.approx(-math.log(PROBABILITY_MODEL_ZERO_FLOOR), abs=0.01)
    assert brier == sum(
        (served[key] - (1.0 if key == cell["settled_bucket"] else 0.0)) ** 2
        for key in cell["ordered_keys"]
    )


def test_a_candidate_is_scored_through_the_transform_and_then_the_floor() -> None:
    cell = _cell(random.Random(2), zeros=True, excluded=True)
    adjusted, _ = apply_center_width_calibration(
        cell["probabilities"], delta_c=0.2, width_lambda=0.75,
        ordered_keys=cell["ordered_keys"], key_centers_c=cell["key_centers_c"],
        excluded_buckets=cell["excluded_buckets"],
        open_edge_bounds_c=cell["open_edge_bounds_c"],
    )
    total = sum(adjusted[key] for key in cell["ordered_keys"])
    served, _ = floor_probabilities(
        {key: adjusted[key] / total for key in cell["ordered_keys"]},
        cell["excluded_buckets"],
    )
    assert served[cell["excluded_buckets"][0]] == 0.0          # excluded stays out
    assert cws._cell_nll(cell, delta_c=0.2, width_lambda=0.75) == (
        -math.log(served[cell["settled_bucket"]])
    )


@pytest.mark.parametrize("seed", range(6))
def test_every_grid_entry_is_the_scalar_logscore_exactly(seed: int) -> None:
    rng = random.Random(seed)
    cell = _cell(rng, zeros=seed % 2 == 0, excluded=seed % 3 == 0)
    floor = rng.choice([0.0, cws.SIGMA_FLOOR_RUNGS])
    with cws._round_memo():
        row = cws._grid_rows(cell, floor)
        for position in range(0, len(cws._GRID_PAIRS), 7):
            delta, lam = cws._GRID_PAIRS[position]
            assert row[position] == cws._cell_nll(
                cell, delta_c=delta, width_lambda=lam, sigma_floor_rungs=floor
            ), (delta, lam)


def test_the_gate_needs_both_scores() -> None:
    clusters = [(f"S{index:02d}", "2026-09-20") for index in range(40)]
    base = [(1.50, 0.70)] * 40
    both = [(1.40, 0.68)] * 40
    log_only = [(1.40, 0.72)] * 40
    assert cws._proper_score_gate(base, both, clusters)["lower"] is True
    split = cws._proper_score_gate(base, log_only, clusters)
    assert split["ci_logscore"][1] < 0 < split["ci_brier"][0]
    assert split["lower"] is False
    assert cws._proper_score_gate(base, base, clusters)["lower"] is False


def test_the_interval_resamples_station_days() -> None:
    """Forty cells from one station-day are one weather: a gain that lives
    only there is not significant however many cells carry it."""

    base = [(1.5, 0.7)] * 60
    candidate = [(1.2, 0.6)] * 40 + [(1.52, 0.705)] * 20
    one_day = [("AAAA", "2026-09-20")] * 40 + [
        (f"S{index:02d}", "2026-09-21") for index in range(20)
    ]
    spread = [(f"S{index:02d}", "2026-09-20") for index in range(60)]
    assert cws._proper_score_gate(base, candidate, spread)["lower"] is True
    lumped = cws._proper_score_gate(base, candidate, one_day)
    assert lumped["station_days"] == 21
    assert lumped["lower"] is False


def test_the_gate_is_reproducible() -> None:
    rng = random.Random(7)
    base = [(rng.random(), rng.random()) for _ in range(50)]
    candidate = [(b[0] - 0.01 * rng.random(), b[1]) for b in base]
    clusters = [(f"S{index % 17}", "d") for index in range(50)]
    assert cws._proper_score_gate(base, candidate, clusters) == (
        cws._proper_score_gate(base, candidate, clusters)
    )


def test_the_old_single_score_machinery_is_gone() -> None:
    for name in ("MIN_FORWARD_NLL_GAIN", "MAX_BUCKET_NLL_REGRESSION", "WINDOW_TERM_WEIGHT"):
        assert not hasattr(cws, name), name
    assert cws.GATE_RULE == "brier_and_logscore_both_lower_station_day_bootstrap_v1"
    assert (cws.GATE_BOOTSTRAP_DRAWS, cws.GATE_CONFIDENCE) == (2000, 0.95)
    # Its only other reader wrote a table nobody read, and went with it.
    assert not (ROOT / "scripts" / "fit_center_width_replay_table.py").exists()


def test_charter_228_records_the_rule() -> None:
    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    clause = charter.split("\n228. ", 1)[1].split("\n", 1)[0]
    for needle in ("第 211", "第 224", "1e-3", "站日", "窗口", "0.75", "裁决人：用户"):
        assert needle in clause, needle
