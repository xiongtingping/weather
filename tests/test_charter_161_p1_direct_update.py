"""Charter 161: P1's observation update fitted directly on the final P1
(2026-09-20, 「上线b2」).

P1(k) ~ root(k) M(k) exp f(k), while the artifact's ``direct_update`` block
serves; otherwise charter 150's g**alpha exactly as before.  The fitter's
numpy x is the served ``state_vector`` row by row.
"""
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import numpy as np
import pytest

from bayes_basket_target import metar_trajectory as MT
from bayes_basket_target import p1_direct_update as DU
from bayes_basket_target import p1_observation_likelihood as OL
from bayes_basket_target.materialization import PROBABILITY_MODEL_ZERO_FLOOR
from tests.test_charter_150_p1_joint_likelihood import _artifact, _compile

ROOT = Path(__file__).resolve().parents[1]


def _fitter():
    spec = importlib.util.spec_from_file_location(
        "_fit_p1_charter161", ROOT / "scripts" / "fit_p1_observation_likelihood.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _block(state: str = DU.GATE_SERVE, seed: int = 11, scale: float = 0.3) -> dict:
    rng = np.random.default_rng(seed)
    width = len(DU.x_layout())
    return {
        "schema": DU.SCHEMA, "rule": DU.RULE, "state": state, "lambda": 1.0,
        "x_layout": DU.x_layout(),
        "theta_rise": [float(v) for v in rng.normal(0.0, scale, width)],
        "theta_slope": [float(v) for v in rng.normal(0.0, scale, width)],
        "theta_slope_sq": -0.2, "theta_open_top": 0.1,
        "fallbacks": {name: 0.1 for name in DU.FEATURES},
        "mean": [0.0] * len(DU.STATE_COLUMNS),
        "scale": [1.0] * len(DU.STATE_COLUMNS),
        "trained_through": "2026-07-20",
    }


def _with_block(block: dict | None) -> dict:
    body = _artifact()
    if block is not None:
        body[DU.BLOCK_KEY] = block
    return body


def test_the_fitters_x_is_the_served_state_vector() -> None:
    fitter = _fitter()
    rng = np.random.default_rng(3)
    rows = 200
    raw = rng.normal(0.0, 2.0, (rows, len(DU.STATE_COLUMNS)))
    raw[rng.random((rows, len(DU.STATE_COLUMNS))) < 0.1] = np.nan
    raw[:, len(DU.FEATURES):] = rng.normal(0.0, 1.0, (rows, len(DU.ROOT_FEATURES)))
    hours = np.concatenate([rng.uniform(-1.0, 30.0, rows - 4), [0.0, 6.0, 10.0, 26.0]])
    design = {"raw": raw, "hours": hours}
    scaling = {
        "fallbacks": {name: float(i) / 10.0 for i, name in enumerate(DU.FEATURES)},
        "mean": [float(v) for v in rng.normal(0.0, 1.0, len(DU.STATE_COLUMNS))],
        "scale": [float(v) for v in rng.uniform(0.5, 2.0, len(DU.STATE_COLUMNS))],
    }
    x = fitter.direct_x(design, np.arange(rows), scaling)
    assert x.shape[1] == len(DU.x_layout())
    for i in range(rows):
        features = {
            name: (None if np.isnan(raw[i, k]) else float(raw[i, k]))
            for k, name in enumerate(DU.FEATURES)
        }
        root = {name: float(raw[i, len(DU.FEATURES) + k]) for k, name in enumerate(DU.ROOT_FEATURES)}
        served = DU.state_vector(features, root, float(hours[i]), scaling)
        assert np.allclose(x[i], served, atol=1e-12), i


def test_the_served_p1_is_root_times_mask_times_exp_f() -> None:
    root, packets = _compile(_with_block(_block()))
    envelope = packets[-1]["expected_envelope"]
    receipt = json.loads(envelope["lineage"][OL.LINEAGE_KEY])
    assert envelope["posterior_version"] == "p1_joint_likelihood_v22"
    assert receipt["state"] == "APPLIED" and receipt["update"] == DU.UPDATE
    assert receipt["schema"] == OL.RECEIPT_SCHEMA and receipt["rule"] == DU.RULE
    excluded = set(packets[-1]["hard_excluded_buckets"])
    # Rebuild with the module's own functions from the receipt's features.
    likelihood, _ = DU.compile_direct_update(
        receipt["features"], _with_block(_block()),
        root_probabilities=root.probabilities, bucket_ordinals=root.bucket_ordinals,
        bucket_centers_c=packets[-1]["bucket_centers_c"],
        bucket_upper_bounds_c=packets[-1]["bucket_upper_bounds_c"],
        running_high_c=receipt["running_high_c"],
        running_high_bucket=receipt["running_high_bucket"],
        hard_excluded_buckets=sorted(excluded),
    )
    weighted = {
        b: 0.0 if b in excluded else p * likelihood[b] for b, p in root.probabilities.items()
    }
    total = sum(weighted.values())
    expected = {b: v / total for b, v in weighted.items()}
    if any(v < PROBABILITY_MODEL_ZERO_FLOOR for b, v in expected.items() if b not in excluded):
        pytest.skip("fixture landed under the floor")
    for bucket, value in expected.items():
        assert envelope["probabilities"][bucket] == pytest.approx(value, abs=1e-9)
    # f for the served buckets is in the receipt, and exp(f - max) is the multiplier.
    f = receipt["log_update_by_bucket"]
    top = max(f.values())
    for bucket, value in f.items():
        assert receipt["likelihood_by_bucket"][bucket] == pytest.approx(math.exp(value - top), abs=1e-6)
    # The board's 「再升」 is the updated belief above the running bucket.
    above = sum(
        v for b, v in expected.items() if receipt["rung_by_bucket"][b] >= 1
    )
    assert receipt["rise_probability"] == pytest.approx(above, abs=1e-9)


def test_the_order_gate_trades_a_direct_update() -> None:
    _, packets = _compile(_with_block(_block()))
    MT.assert_p1_execution_evidence(packets[-1]["expected_envelope"]["lineage"])


def test_an_identity_block_serves_charter_150_unchanged() -> None:
    _, plain = _compile(_artifact())
    _, held = _compile(_with_block(_block(state="IDENTITY")))
    a, b = plain[-1]["expected_envelope"], held[-1]["expected_envelope"]
    assert a["probabilities"] == b["probabilities"]
    receipt = json.loads(b["lineage"][OL.LINEAGE_KEY])
    assert "update" not in receipt and receipt["alpha"] is not None


def test_a_malformed_serving_block_is_refused_and_p1_still_compiles() -> None:
    broken = _block()
    broken["theta_rise"] = broken["theta_rise"][:-1]
    _, packets = _compile(_with_block(broken))
    receipt = json.loads(packets[-1]["expected_envelope"]["lineage"][OL.LINEAGE_KEY])
    assert receipt["state"] == "REFUSED"
    assert "p1_direct_update_block_invalid" in receipt["reason"]


def test_the_bases_read_the_ladder_the_way_g_served_it() -> None:
    ordinals = {"36": 0, "37": 1, "38": 2, "39": 3, "40+": 4}
    centers = {"36": 36.0, "37": 37.0, "38": 38.0, "39": 39.0, "40+": 40.0}
    uppers = {"36": 36.5, "37": 37.5, "38": 38.5, "39": 39.5, "40+": None}
    bases = DU.bucket_bases(
        bucket_ordinals=ordinals, bucket_centers_c=centers, bucket_upper_bounds_c=uppers,
        running_high_c=37.0, running_high_bucket="37", hard_excluded_buckets=["36"],
    )
    assert bases == {
        "36": (-1, 0.0, 0.0, 0.0),
        "37": (0, 0.0, 0.0, 0.0),
        "38": (1, 1.0, 0.0, 0.0),
        "39": (2, 1.0, 1.0, 0.0),
        "40+": (3, 1.0, 2.0, 1.0),
    }


def test_a_zero_update_scores_as_the_root_under_the_floor() -> None:
    fitter = _fitter()
    rng = np.random.default_rng(9)
    rows, width = 50, 6
    prior = rng.dirichlet(np.full(width, 0.5), rows)
    live = np.ones((rows, width), dtype=bool); live[:, 0] = False
    design = {
        "prior": prior, "live": live, "truth": rng.integers(1, width, rows),
        "rise": np.zeros((rows, width)), "slope": np.zeros((rows, width)),
        "top": np.zeros((rows, width)),
        "raw": rng.normal(0.0, 1.0, (rows, len(DU.STATE_COLUMNS))),
        "hours": rng.uniform(0.0, 20.0, rows),
    }
    scaling = {"fallbacks": {n: 0.0 for n in DU.FEATURES},
               "mean": [0.0] * len(DU.STATE_COLUMNS), "scale": [1.0] * len(DU.STATE_COLUMNS)}
    theta = np.zeros(2 * len(DU.x_layout()) + 2)
    scores = fitter.direct_scores(design, np.arange(rows), scaling, theta)
    from bayes_basket_target.materialization import floor_probabilities

    for i in range(rows):
        masked = {str(j): (prior[i, j] if live[i, j] else 0.0) for j in range(width)}
        total = sum(masked.values())
        served, _ = floor_probabilities({k: v / total for k, v in masked.items()}, {"0"})
        assert scores[i] == pytest.approx(math.log(served[str(design["truth"][i])]), abs=1e-9)
